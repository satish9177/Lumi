import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type {
  AgentBrowserProfileView,
  AgentDesktopActionView,
  AgentDesktopReadView,
  AgentDesktopScrollTargetList,
  AgentDesktopSurfaceList,
  AgentRegisteredApp,
  AgentResult,
  AgentTaskSnapshot
} from '../../shared/agent-contracts'
import type { AgentProjectRunView } from '../../shared/project-contracts'
import type { AgentDocumentTaskView, AgentLocalComparisonView } from '../../shared/document-contracts'
import type { AgentOrchestrationStepView, AgentOrchestrationView } from '../../shared/orchestration-contracts'
import { ModelRoutingError, type OrchestrationDecision, type OrchestrationPlanOutcome } from '../agent/orchestration-planner'
import {
  OrchestrationCoordinator,
  type OrchestrationGraphClient,
  type OrchestrationPlannerLike
} from './orchestration-coordinator'

/**
 * Milestone 11 S2: the orchestration loop, against an in-memory durable-graph double.
 *
 * The double reimplements just enough of `OrchestrationService`'s state machine (RUNNING/PAUSED, one
 * step per capability, approval-required pause on a task-backed step, budgets) to prove the *coordinator's*
 * own properties: it never executes a capability outside its closed `dispatch`, it stops at the first
 * pause rather than guessing past it, and a malformed/unavailable planner never silently continues.
 * `test_orchestration_service.py` is the source of truth for the durable engine's own behavior.
 */

const WORKER = '99999999-2222-4333-8444-555555555555'

function orchestration(overrides: Partial<AgentOrchestrationView> = {}): AgentOrchestrationView {
  return {
    orchestrationId: '00000000-0000-4000-8000-000000000001',
    status: 'RUNNING',
    live: true,
    revision: 1,
    objective: 'Research the Lumi repository and summarize it',
    stepCount: 0,
    childTaskCount: 0,
    plannerCalls: 0,
    createdAt: '2026-09-24T10:00:00+00:00',
    expiresAt: '2026-09-24T10:30:00+00:00',
    availableCapabilities: ['public_research', 'project_status'],
    steps: [],
    ...overrides
  }
}

class FakeGraph implements OrchestrationGraphClient {
  view: AgentOrchestrationView = orchestration()
  calls: string[] = []

  async createOrchestration(): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push('create')
    return { ok: true, value: this.view }
  }

  async getOrchestration(): Promise<AgentResult<AgentOrchestrationView>> {
    return { ok: true, value: this.view }
  }

  async getLatestOrchestration(): Promise<AgentResult<AgentOrchestrationView | null>> {
    return { ok: true, value: this.view }
  }

  async recordPlannerCall(): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push('planner-call')
    this.view = { ...this.view, plannerCalls: this.view.plannerCalls + 1, revision: this.view.revision + 1 }
    return { ok: true, value: this.view }
  }

  async advanceOrchestration(
    _id: string, _revision: number, capabilityId: AgentCapabilityId, options: { taskId?: string; resolvedSummary?: string }
  ): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push(`advance:${capabilityId}`)
    const sequence = this.view.steps.length + 1
    const step: AgentOrchestrationStepView = options.taskId
      ? { sequence, capabilityId, status: 'AWAITING_APPROVAL', childTaskId: options.taskId }
      : { sequence, capabilityId, status: 'SUCCEEDED', resultHandle: `${capabilityId}:${sequence}`, resultSummary: options.resolvedSummary }
    this.view = {
      ...this.view,
      revision: this.view.revision + 1,
      stepCount: this.view.stepCount + 1,
      steps: [...this.view.steps, step],
      ...(options.taskId ? { status: 'PAUSED', pauseReason: 'approval_required' } : {})
    }
    return { ok: true, value: this.view }
  }

  async resumeOrchestration(): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push('resume')
    if (this.view.status === 'PAUSED' && this.view.pauseReason === 'approval_required') {
      this.view = { ...this.view, status: 'RUNNING', pauseReason: undefined, revision: this.view.revision + 1 }
    }
    return { ok: true, value: this.view }
  }

  async finishOrchestration(): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push('finish')
    this.view = { ...this.view, status: 'SUCCEEDED', revision: this.view.revision + 1 }
    return { ok: true, value: this.view }
  }

  async stopOrchestration(): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push('stop')
    this.view = { ...this.view, status: 'STOPPED', revision: this.view.revision + 1 }
    return { ok: true, value: this.view }
  }

  async registerResource(
    _id: string, _revision: number,
    options: { kind: string; safeLabel: string; backingId?: string; backingText?: string; documentTaskId?: string }
  ): Promise<AgentResult<AgentOrchestrationView>> {
    this.calls.push(`register:${options.kind}`)
    const sequence = (this.view.resources ?? []).length + 1
    this.view = {
      ...this.view,
      revision: this.view.revision + 1,
      ...(options.documentTaskId !== undefined ? { documentTaskId: options.documentTaskId } : {}),
      resources: [
        ...(this.view.resources ?? []),
        {
          ref: `r${sequence}`, kind: options.kind, privacyClass: 'private', safeLabel: options.safeLabel,
          singleUse: false, ...(options.backingId !== undefined ? { backingId: options.backingId } : {}),
          ...(options.backingText !== undefined ? { backingText: options.backingText } : {})
        }
      ]
    }
    return { ok: true, value: this.view }
  }
}

class ScriptedPlanner implements OrchestrationPlannerLike {
  calls = 0
  constructor(private readonly decisions: OrchestrationDecision[]) {}

  async next(): Promise<OrchestrationPlanOutcome> {
    const decision = this.decisions[Math.min(this.calls, this.decisions.length - 1)]
    this.calls += 1
    return { decision, provider: 'scripted', model: 'scripted-1' }
  }
}

class FailingPlanner implements OrchestrationPlannerLike {
  async next(): Promise<OrchestrationPlanOutcome> {
    throw new ModelRoutingError('orchestration_planning', [])
  }
}

function coordinator(input: {
  graph: FakeGraph
  planner: OrchestrationPlannerLike
  research?: AgentResult<AgentTaskSnapshot>
  projectRun?: AgentResult<AgentProjectRunView | null>
  recipeId?: AgentResult<string | null>
  createdRun?: AgentResult<AgentProjectRunView>
  startedRun?: AgentResult<AgentProjectRunView>
  extracted?: AgentResult<AgentDocumentTaskView>
  compared?: AgentResult<AgentLocalComparisonView>
  accountReadTask?: AgentResult<AgentTaskSnapshot>
  continuedAccountRead?: AgentResult<AgentTaskSnapshot>
  profiles?: AgentResult<AgentBrowserProfileView[]>
  desktopSurfaces?: AgentResult<AgentDesktopSurfaceList>
  desktopObservation?: AgentResult<{ nodeCount: number; truncated: boolean }>
  desktopReasonTask?: AgentResult<AgentDesktopReadView>
  desktopFocusAction?: AgentResult<AgentDesktopActionView>
  desktopScrollTargets?: AgentResult<AgentDesktopScrollTargetList>
  desktopScrollAction?: AgentResult<AgentDesktopActionView>
  desktopApps?: AgentResult<AgentRegisteredApp[]>
  desktopLaunchAction?: AgentResult<AgentDesktopActionView>
  stoppedRun?: AgentResult<AgentProjectRunView>
}): {
  coordinator: OrchestrationCoordinator; researchCalls: string[]; startCalls: string[]
  extractCalls: Array<{ taskId: string; fileId: string }>; compareCalls: Array<{ taskId: string; first: string; second: string }>
  accountReadCalls: Array<{ objective: string; profileId: string }>; continueAccountReadCalls: string[]
  observeCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }>
  desktopReasonCalls: Array<{ objective: string; workerGeneration: string; surfaceRef: string; surfaceEpoch: number }>
  focusCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }>
  scrollTargetCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }>
  scrollCalls: Array<{ workerGeneration: string; observationId: string; controlRef: string; step: string }>
  launchCalls: string[]
  stopProjectCalls: string[]
} {
  const researchCalls: string[] = []
  const startCalls: string[] = []
  const extractCalls: Array<{ taskId: string; fileId: string }> = []
  const compareCalls: Array<{ taskId: string; first: string; second: string }> = []
  const accountReadCalls: Array<{ objective: string; profileId: string }> = []
  const continueAccountReadCalls: string[] = []
  const observeCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }> = []
  const desktopReasonCalls: Array<{ objective: string; workerGeneration: string; surfaceRef: string; surfaceEpoch: number }> = []
  const focusCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }> = []
  const scrollTargetCalls: Array<{ workerGeneration: string; surfaceRef: string; surfaceEpoch: number }> = []
  const scrollCalls: Array<{ workerGeneration: string; observationId: string; controlRef: string; step: string }> = []
  const launchCalls: string[] = []
  const stopProjectCalls: string[] = []
  const coord = new OrchestrationCoordinator({
    orchestrations: input.graph,
    planner: input.planner,
    createResearchTask: async (objective) => {
      researchCalls.push(objective)
      return input.research ?? {
        ok: true,
        value: { task: { taskId: randomUUID(), kind: 'public_research', status: 'WAITING_APPROVAL' } } as unknown as AgentTaskSnapshot
      }
    },
    getLatestProjectRun: async () => input.projectRun ?? { ok: true, value: null },
    getRegisteredRecipeId: async () => input.recipeId ?? { ok: true, value: '00000000-0000-4000-8000-0000000000rc' },
    createProjectRun: async () => input.createdRun ?? {
      ok: true,
      value: { taskId: randomUUID(), phase: 'awaiting_approval' } as unknown as AgentProjectRunView
    },
    startProjectRun: async (taskId) => {
      startCalls.push(taskId)
      return input.startedRun ?? { ok: true, value: { taskId, phase: 'approved' } as unknown as AgentProjectRunView }
    },
    createDocumentTask: async (objective) => ({
      ok: true, value: { taskId: randomUUID(), objective, files: [], documents: [] } as unknown as AgentDocumentTaskView
    }),
    addDocumentFromRoot: async (taskId, rootId, relativePath) => ({
      ok: true,
      value: {
        taskId,
        files: [{ fileId: randomUUID(), source: 'ROOT_FILE', displayName: relativePath, rootId, format: 'PDF', sizeBytes: 1, addedAt: '' }],
        documents: []
      } as unknown as AgentDocumentTaskView
    }),
    extractDocument: async (taskId, fileId) => {
      extractCalls.push({ taskId, fileId })
      return input.extracted ?? {
        ok: true,
        value: { taskId, documents: [{ documentId: randomUUID(), fileId, textChars: 1234 }] } as unknown as AgentDocumentTaskView
      }
    },
    compareDocumentsLocally: async (taskId, first, second) => {
      compareCalls.push({ taskId, first, second })
      return input.compared ?? { ok: true, value: { overlap: 0.5 } as unknown as AgentLocalComparisonView }
    },
    createAccountReadTask: async (objective, profileId) => {
      accountReadCalls.push({ objective, profileId })
      return input.accountReadTask ?? {
        ok: true,
        value: { task: { taskId: randomUUID(), kind: 'authenticated_read', status: 'WAITING_APPROVAL' } } as unknown as AgentTaskSnapshot
      }
    },
    continueAccountRead: async (taskId) => {
      continueAccountReadCalls.push(taskId)
      return input.continuedAccountRead ?? {
        ok: true, value: { task: { taskId, kind: 'authenticated_read', status: 'PAUSED' } } as unknown as AgentTaskSnapshot
      }
    },
    listBrowserProfiles: async () => input.profiles ?? {
      ok: true,
      value: [{
        profileId: '00000000-0000-4000-8000-0000000000aa', label: 'GitHub - Personal', site: 'github.com',
        status: 'AUTHENTICATED', revision: 1
      }] as AgentBrowserProfileView[]
    },
    listDesktopSurfaces: async () => input.desktopSurfaces ?? {
      ok: true,
      value: {
        workerGeneration: WORKER, surfaces: [{ surfaceRef: 's1', surfaceEpoch: 1, applicationLabel: 'Editor', windowTitle: 'notes.txt', visible: true, minimized: false }],
        truncated: false
      }
    },
    observeDesktopTarget: async (workerGeneration, surfaceRef, surfaceEpoch) => {
      observeCalls.push({ workerGeneration, surfaceRef, surfaceEpoch })
      return input.desktopObservation ?? { ok: true, value: { nodeCount: 5, truncated: false } }
    },
    createDesktopReasonTask: async (objective, target) => {
      desktopReasonCalls.push({ objective, ...target })
      return input.desktopReasonTask ?? {
        ok: true, value: { taskId: randomUUID() } as unknown as AgentDesktopReadView
      }
    },
    proposeDesktopFocus: async (workerGeneration, surfaceRef, surfaceEpoch) => {
      focusCalls.push({ workerGeneration, surfaceRef, surfaceEpoch })
      return input.desktopFocusAction ?? {
        ok: true, value: { actionId: randomUUID(), taskId: randomUUID() } as unknown as AgentDesktopActionView
      }
    },
    findDesktopScrollTargets: async (workerGeneration, surfaceRef, surfaceEpoch) => {
      scrollTargetCalls.push({ workerGeneration, surfaceRef, surfaceEpoch })
      return input.desktopScrollTargets ?? {
        ok: true, value: { observationId: randomUUID(), targets: [{ controlRef: 'u2', role: 'list', name: 'Results' }] }
      }
    },
    proposeDesktopScroll: async (workerGeneration, observationId, controlRef, step) => {
      scrollCalls.push({ workerGeneration, observationId, controlRef, step })
      return input.desktopScrollAction ?? {
        ok: true, value: { actionId: randomUUID(), taskId: randomUUID() } as unknown as AgentDesktopActionView
      }
    },
    listDesktopApps: async () => input.desktopApps ?? {
      ok: true, value: [{ appId: 'notepad', label: 'Notepad' }]
    },
    proposeDesktopLaunch: async (appId) => {
      launchCalls.push(appId)
      return input.desktopLaunchAction ?? {
        ok: true, value: { actionId: randomUUID(), taskId: randomUUID() } as unknown as AgentDesktopActionView
      }
    },
    stopProjectRun: async (taskId) => {
      stopProjectCalls.push(taskId)
      return input.stoppedRun ?? { ok: true, value: { taskId, phase: 'stopped' } as unknown as AgentProjectRunView }
    }
  })
  return {
    coordinator: coord, researchCalls, startCalls, extractCalls, compareCalls, accountReadCalls, continueAccountReadCalls,
    observeCalls, desktopReasonCalls, focusCalls, scrollTargetCalls, scrollCalls, launchCalls, stopProjectCalls
  }
}

describe('OrchestrationCoordinator.run', () => {
  it('dispatches public_research through its own boundary and pauses for its approval, never bypassing it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord, researchCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'public_research', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.status).toBe('PAUSED')
    expect(result.value.pauseReason).toBe('approval_required')
    expect(researchCalls).toEqual(['Research the Lumi repository and summarize it'])
    expect(graph.calls).toEqual(['planner-call', 'advance:public_research'])
  })

  it('dispatches project_status synchronously, with no task and no pause, and keeps looping', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph,
      planner: new ScriptedPlanner([
        { kind: 'step', capability: 'project_status', reason: 'x' },
        { kind: 'finish', reason: 'done' }
      ]),
      projectRun: { ok: true, value: { phase: 'running', ready: true } as unknown as AgentProjectRunView }
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.status).toBe('SUCCEEDED')
    expect(result.value.steps).toHaveLength(1)
    expect(result.value.steps[0].status).toBe('SUCCEEDED')
    expect(result.value.steps[0].resultSummary).toContain('running')
    expect(graph.calls).toEqual(['planner-call', 'advance:project_status', 'planner-call', 'finish'])
  })

  it('dispatches project_start through its own boundary and pauses for its warning card, never starting unapproved', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord, startCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'project_start', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.status).toBe('PAUSED')
    expect(result.value.pauseReason).toBe('approval_required')
    expect(graph.calls).toEqual(['planner-call', 'advance:project_start'])
    // Linking the freshly-created run never itself calls start(): nothing was approved yet.
    expect(startCalls).toEqual([])
  })

  it('refuses project_start when no recipe is registered, rather than inventing one', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'project_start', reason: 'x' }]),
      recipeId: { ok: true, value: null }
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_refused')
    expect(graph.calls).toEqual(['planner-call'])
  })

  it('surfaces the SAME refusal ProjectService’s own effect lock/one-live-run guarantee would give a direct request, never bypassing it', async () => {
    // No new authorization path exists for project_start: createProjectRun is ProjectController's own
    // entry point, so a run already active for this project refuses here exactly as it would outside
    // orchestration -- the coordinator adds no parallel "try again a different way" behavior.
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'project_start', reason: 'x' }]),
      createdRun: { ok: false, error: { code: 'project_refused', message: 'A run is already active for this project.' } }
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('project_refused')
    // No step was ever recorded for the refused attempt -- only the planner call that chose it.
    expect(graph.calls).toEqual(['planner-call'])
  })

  it('on resume, nudges an approved project_start run forward with the SAME idempotent start(), never a second approval', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      status: 'PAUSED',
      pauseReason: 'approval_required',
      stepCount: 1,
      steps: [{ sequence: 1, capabilityId: 'project_start', status: 'AWAITING_APPROVAL', childTaskId: '00000000-0000-4000-8000-0000000000aa' }],
      availableCapabilities: []
    })
    const { coordinator: coord, startCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'finish', reason: 'done' }])
    })
    await coord.run(graph.view.orchestrationId)
    expect(startCalls).toEqual(['00000000-0000-4000-8000-0000000000aa'])
  })

  it('never nudges start() for a capability other than project_start', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      status: 'PAUSED',
      pauseReason: 'approval_required',
      stepCount: 1,
      steps: [{ sequence: 1, capabilityId: 'public_research', status: 'AWAITING_APPROVAL', childTaskId: '00000000-0000-4000-8000-0000000000bb' }],
      availableCapabilities: []
    })
    const { coordinator: coord, startCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'finish', reason: 'done' }])
    })
    await coord.run(graph.view.orchestrationId)
    expect(startCalls).toEqual([])
  })

  it('stops when the planner says stop, and touches no capability', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord, researchCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'stop', reason: 'no path forward' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.status).toBe('STOPPED')
    expect(researchCalls).toEqual([])
  })

  it('a failed/unavailable planner leaves the orchestration untouched and reports a refusal, never guesses a step', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord, researchCalls } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_refused')
    expect(researchCalls).toEqual([])
    expect(graph.calls).toEqual(['planner-call'])
  })

  it('resumes a paused-for-approval orchestration by re-checking durable state, not by re-choosing', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      status: 'PAUSED',
      pauseReason: 'approval_required',
      stepCount: 1,
      steps: [{ sequence: 1, capabilityId: 'public_research', status: 'SUCCEEDED', resultHandle: 'research_result:1', resultSummary: 'answered: ok' }],
      availableCapabilities: []
    })
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'finish', reason: 'done' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    // resume() re-checked state before the loop resumed planning; only after that did a fresh planner
    // call and the finish decision run.
    expect(graph.calls[0]).toBe('resume')
    expect(result.value.status).toBe('SUCCEEDED')
  })

  it('stops the loop after a bounded number of iterations rather than looping forever', async () => {
    const graph = new FakeGraph()
    // A planner that always asks for the same already-succeeded capability would loop forever without a
    // client-side bound; the graph double's own advance() keeps returning SUCCEEDED for project_status
    // (it does not itself enforce the server's loop guard), so only the coordinator's bound stops this.
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'project_status', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.steps.length).toBeLessThanOrEqual(20)
  })

  it('createAndRun creates a fresh orchestration and runs it to its first pause', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'public_research', reason: 'x' }])
    })
    const result = await coord.createOrchestration('Research the Lumi repository and summarize it')
    expect(result.ok).toBe(true)
    expect(graph.calls[0]).toBe('create')
  })

  it('refuses a capability with no dispatch handler rather than silently skipping it', async () => {
    // Not reachable through the real Python catalog (it only ever offers composed capabilities), but the
    // coordinator's own closed dispatch must still fail closed if it ever were.
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_observe'] })
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_observe', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_refused')
  })

  it('dispatches account_read by resolving the cited account_context_ref, never trusting the planner’s own text', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['account_read'],
      resources: [{
        ref: 'r1', kind: 'account_context_ref', privacyClass: 'private',
        safeLabel: 'approved signed-in account context for github.com', singleUse: false,
        backingId: '00000000-0000-4000-8000-0000000000aa'
      }]
    })
    const { coordinator: coord, accountReadCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'account_read', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(accountReadCalls).toEqual([{ objective: graph.view.objective, profileId: '00000000-0000-4000-8000-0000000000aa' }])
    expect(graph.calls).toContain('advance:account_read')
  })

  it('refuses account_read when no account_context_ref was cited, before creating any task', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['account_read'], resources: [] })
    const { coordinator: coord, accountReadCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'account_read', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(accountReadCalls).toEqual([])
  })

  it('refuses account_read when the cited ref is the wrong kind, before creating any task', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['account_read'],
      resources: [{ ref: 'r1', kind: 'document_ref', privacyClass: 'private', safeLabel: 'x', singleUse: false, backingId: '00000000-0000-4000-8000-0000000000aa' }]
    })
    const { coordinator: coord, accountReadCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'account_read', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(accountReadCalls).toEqual([])
  })

  // ---- Milestone 12 S4: desktop + app + project-stop composition ---------------------------------

  const DESKTOP_TARGET_RESOURCE = {
    ref: 'r1', kind: 'desktop_target_ref', privacyClass: 'private' as const,
    safeLabel: 'approved desktop window: Editor', singleUse: false, backingText: `${WORKER}|s1|1`
  }
  const APP_RESOURCE = {
    ref: 'r1', kind: 'app_ref', privacyClass: 'none' as const, safeLabel: 'Notepad', singleUse: false, backingText: 'notepad'
  }
  const PROJECT_RESOURCE = {
    ref: 'r1', kind: 'project_ref', privacyClass: 'none' as const, safeLabel: 'registered project run', singleUse: false,
    backingId: '00000000-0000-4000-8000-0000000000pp'
  }

  it('dispatches desktop_observe by resolving the cited desktop_target_ref into (workerGeneration, surfaceRef, surfaceEpoch), never a native identity', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_observe'], resources: [DESKTOP_TARGET_RESOURCE] })
    const { coordinator: coord, observeCalls } = coordinator({
      graph, planner: new ScriptedPlanner([
        { kind: 'step', capability: 'desktop_observe', resources: ['r1'], reason: 'x' },
        { kind: 'finish', reason: 'done' }
      ])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(observeCalls).toEqual([{ workerGeneration: WORKER, surfaceRef: 's1', surfaceEpoch: 1 }])
    expect(graph.calls).toContain('advance:desktop_observe')
  })

  it('refuses desktop_observe when no desktop_target_ref was cited', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_observe'], resources: [] })
    const { coordinator: coord, observeCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_observe', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(observeCalls).toEqual([])
  })

  it('refuses desktop_observe when the cited resource is not shaped like a desktop target', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['desktop_observe'],
      resources: [{ ...DESKTOP_TARGET_RESOURCE, backingText: 'not-shaped-at-all' }]
    })
    const { coordinator: coord, observeCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_observe', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(observeCalls).toEqual([])
  })

  it('dispatches desktop_reason by opening the existing desktop-read card over the resolved window', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_reason'], resources: [DESKTOP_TARGET_RESOURCE] })
    const { coordinator: coord, desktopReasonCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_reason', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(desktopReasonCalls).toEqual([{ objective: graph.view.objective, workerGeneration: WORKER, surfaceRef: 's1', surfaceEpoch: 1 }])
    expect(graph.calls).toContain('advance:desktop_reason')
  })

  it('dispatches desktop_safe_action defaulting to focus when the planner omits "operation"', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_safe_action'], resources: [DESKTOP_TARGET_RESOURCE] })
    const { coordinator: coord, focusCalls, scrollTargetCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_safe_action', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(focusCalls).toEqual([{ workerGeneration: WORKER, surfaceRef: 's1', surfaceEpoch: 1 }])
    expect(scrollTargetCalls).toEqual([])
    expect(graph.calls).toContain('advance:desktop_safe_action')
  })

  it('dispatches desktop_safe_action scroll_down by letting trusted code choose the first scrollable control, never the planner', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_safe_action'], resources: [DESKTOP_TARGET_RESOURCE] })
    const { coordinator: coord, scrollTargetCalls, scrollCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_safe_action', resources: ['r1'], operation: 'scroll_down', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(scrollTargetCalls).toEqual([{ workerGeneration: WORKER, surfaceRef: 's1', surfaceEpoch: 1 }])
    expect(scrollCalls).toHaveLength(1)
    expect(scrollCalls[0]).toMatchObject({ workerGeneration: WORKER, controlRef: 'u2', step: 'small_down' })
    expect(graph.calls).toContain('advance:desktop_safe_action')
  })

  it('dispatches desktop_safe_action scroll_up as the closed opposite step', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_safe_action'], resources: [DESKTOP_TARGET_RESOURCE] })
    const { coordinator: coord, scrollCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_safe_action', resources: ['r1'], operation: 'scroll_up', reason: 'x' }])
    })
    await coord.run(graph.view.orchestrationId)
    expect(scrollCalls[0]).toMatchObject({ step: 'small_up' })
  })

  it('refuses desktop_safe_action when no desktop_target_ref was cited', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['desktop_safe_action'], resources: [] })
    const { coordinator: coord, focusCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'desktop_safe_action', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(focusCalls).toEqual([])
  })

  it('dispatches launch_registered_app by resolving the cited app_ref, never an exe path or argument', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['launch_registered_app'], resources: [APP_RESOURCE] })
    const { coordinator: coord, launchCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'launch_registered_app', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(launchCalls).toEqual(['notepad'])
    expect(graph.calls).toContain('advance:launch_registered_app')
  })

  it('refuses launch_registered_app when no app_ref was cited', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['launch_registered_app'], resources: [] })
    const { coordinator: coord, launchCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'launch_registered_app', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(launchCalls).toEqual([])
  })

  it('dispatches project_stop by resolving the cited project_ref into a task id, never a PID or job handle', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['project_stop'], resources: [PROJECT_RESOURCE] })
    const { coordinator: coord, stopProjectCalls } = coordinator({
      graph, planner: new ScriptedPlanner([
        { kind: 'step', capability: 'project_stop', resources: ['r1'], reason: 'x' },
        { kind: 'finish', reason: 'done' }
      ])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(stopProjectCalls).toEqual(['00000000-0000-4000-8000-0000000000pp'])
    expect(graph.calls).toContain('advance:project_stop')
  })

  it('refuses project_stop when no project_ref was cited', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['project_stop'], resources: [] })
    const { coordinator: coord, stopProjectCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'project_stop', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(stopProjectCalls).toEqual([])
  })

  it('Milestone 12 S3: Continue on a manual_handoff_required pause re-observes through continueAccountRead before resuming, never assumes the human already acted', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      status: 'PAUSED',
      pauseReason: 'manual_handoff_required',
      stepCount: 1,
      availableCapabilities: [],
      steps: [{
        sequence: 1, capabilityId: 'account_read', status: 'AWAITING_APPROVAL',
        childTaskId: '00000000-0000-4000-8000-0000000000cc',
        pendingNote: 'Manual action required: sign in to the account in the Lumi browser. When finished, return here and choose Continue.'
      }]
    })
    const { coordinator: coord, continueAccountReadCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'finish', reason: 'done' }])
    })
    const result = await coord.continueOrchestration(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    // The nudge ran BEFORE resume() re-read state -- Continue itself never settles anything.
    expect(continueAccountReadCalls).toEqual(['00000000-0000-4000-8000-0000000000cc'])
    expect(graph.calls[0]).toBe('resume')
  })

  it('never nudges continueAccountRead for a plain approval_required pause (the ordinary scope card, not a handoff)', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      status: 'PAUSED',
      pauseReason: 'approval_required',
      stepCount: 1,
      availableCapabilities: [],
      steps: [{ sequence: 1, capabilityId: 'account_read', status: 'AWAITING_APPROVAL', childTaskId: '00000000-0000-4000-8000-0000000000cc' }]
    })
    const { coordinator: coord, continueAccountReadCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'finish', reason: 'done' }])
    })
    await coord.continueOrchestration(graph.view.orchestrationId)
    expect(continueAccountReadCalls).toEqual([])
  })

  it('dispatches document_read by resolving the cited resource’s backing id, never trusting the planner’s own text', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['document_read'],
      documentTaskId: '00000000-0000-4000-8000-0000000000dd',
      resources: [{ ref: 'r1', kind: 'document_ref', privacyClass: 'private', safeLabel: 'resume.pdf', singleUse: false, backingId: '00000000-0000-4000-8000-0000000000ff' }]
    })
    const { coordinator: coord, extractCalls } = coordinator({
      graph, planner: new ScriptedPlanner([
        { kind: 'step', capability: 'document_read', resources: ['r1'], reason: 'x' },
        { kind: 'finish', reason: 'done' }
      ])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(extractCalls).toEqual([{ taskId: '00000000-0000-4000-8000-0000000000dd', fileId: '00000000-0000-4000-8000-0000000000ff' }])
    expect(graph.calls).toContain('advance:document_read')
  })

  it('refuses document_read when the cited ref is the wrong kind, before calling DocumentService at all', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['document_read'],
      documentTaskId: '00000000-0000-4000-8000-0000000000dd',
      // A document_result_ref, not the document_ref document_read requires.
      resources: [{ ref: 'r1', kind: 'document_result_ref', privacyClass: 'private', safeLabel: 'x', singleUse: false, backingId: '00000000-0000-4000-8000-0000000000ff' }]
    })
    const { coordinator: coord, extractCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'document_read', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(extractCalls).toEqual([])
  })

  it('refuses document_read when the cited ref is not in the fresh available set', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['document_read'], documentTaskId: '00000000-0000-4000-8000-0000000000dd', resources: []
    })
    const { coordinator: coord, extractCalls } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'document_read', resources: ['r1'], reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(extractCalls).toEqual([])
  })

  it('dispatches document_compare by resolving both cited resources, and reports only the numeric overlap', async () => {
    const graph = new FakeGraph()
    graph.view = orchestration({
      availableCapabilities: ['document_compare'],
      documentTaskId: '00000000-0000-4000-8000-0000000000dd',
      resources: [
        { ref: 'r1', kind: 'document_result_ref', privacyClass: 'private', safeLabel: 'a', singleUse: false, backingId: '00000000-0000-4000-8000-000000000001' },
        { ref: 'r2', kind: 'document_result_ref', privacyClass: 'private', safeLabel: 'b', singleUse: false, backingId: '00000000-0000-4000-8000-000000000002' }
      ]
    })
    const { coordinator: coord, compareCalls } = coordinator({
      graph, planner: new ScriptedPlanner([
        { kind: 'step', capability: 'document_compare', resources: ['r1', 'r2'], reason: 'x' },
        { kind: 'finish', reason: 'done' }
      ])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    expect(compareCalls).toEqual([{
      taskId: '00000000-0000-4000-8000-0000000000dd',
      first: '00000000-0000-4000-8000-000000000001', second: '00000000-0000-4000-8000-000000000002'
    }])
    const step = result.ok ? result.value.steps.find((item) => item.capabilityId === 'document_compare') : undefined
    expect(step?.resultSummary).toContain('%')
  })
})

describe('OrchestrationCoordinator.attachApprovedDocument', () => {
  it('creates the shared document task on first use and registers a document_ref', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedDocument(graph.view.orchestrationId, 'root-1', 'resume.pdf')
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.documentTaskId).toBeDefined()
    expect(result.value.resources?.[0]?.kind).toBe('document_ref')
    expect(result.value.resources?.[0]?.safeLabel).toBe('resume.pdf')
    expect(graph.calls).toContain('register:document_ref')
  })

  it('reuses the same document task on a second attach', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const first = await coord.attachApprovedDocument(graph.view.orchestrationId, 'root-1', 'resume.pdf')
    expect(first.ok).toBe(true)
    const taskId = first.ok ? first.value.documentTaskId : undefined
    const second = await coord.attachApprovedDocument(graph.view.orchestrationId, 'root-1', 'job.txt')
    expect(second.ok).toBe(true)
    expect(second.ok && second.value.documentTaskId).toBe(taskId)
  })

  it('refuses a non-string argument rather than forwarding it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedDocument(42, 'root-1', 'resume.pdf')
    expect(result.ok).toBe(false)
  })
})

describe('OrchestrationCoordinator.attachApprovedAccount', () => {
  it('registers an account_context_ref for a signed-in profile the user picked', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedAccount(graph.view.orchestrationId, '00000000-0000-4000-8000-0000000000aa')
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.resources?.[0]?.kind).toBe('account_context_ref')
    expect(result.value.resources?.[0]?.backingId).toBe('00000000-0000-4000-8000-0000000000aa')
    expect(result.value.resources?.[0]?.safeLabel).toBe('approved signed-in account context for github.com')
    expect(graph.calls).toContain('register:account_context_ref')
  })

  it('refuses a profile that is not signed in, never registering a resource for it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new FailingPlanner(),
      profiles: {
        ok: true,
        value: [{
          profileId: '00000000-0000-4000-8000-0000000000aa', label: 'GitHub - Personal', site: 'github.com',
          status: 'NEEDS_LOGIN', revision: 1
        }] as AgentBrowserProfileView[]
      }
    })
    const result = await coord.attachApprovedAccount(graph.view.orchestrationId, '00000000-0000-4000-8000-0000000000aa')
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:account_context_ref')
  })

  it('refuses an unknown profile id, never registering a resource for it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedAccount(graph.view.orchestrationId, '00000000-0000-4000-8000-000000000000')
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:account_context_ref')
  })

  it('refuses a non-string argument rather than forwarding it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedAccount(42, '00000000-0000-4000-8000-0000000000aa')
    expect(result.ok).toBe(false)
  })
})

describe('OrchestrationCoordinator.attachApprovedDesktopTarget', () => {
  it('registers a desktop_target_ref for a window the fresh listing still shows', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedDesktopTarget(graph.view.orchestrationId, WORKER, 's1', 1)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.resources?.[0]?.kind).toBe('desktop_target_ref')
    expect(result.value.resources?.[0]?.backingText).toBe(`${WORKER}|s1|1`)
    expect(result.value.resources?.[0]?.safeLabel).toBe('approved desktop window: Editor')
    expect(graph.calls).toContain('register:desktop_target_ref')
  })

  it('refuses a window whose epoch no longer matches the fresh listing -- the window closed and reopened', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedDesktopTarget(graph.view.orchestrationId, WORKER, 's1', 2)
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:desktop_target_ref')
  })

  it('refuses a different worker generation -- the desktop worker restarted', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedDesktopTarget(graph.view.orchestrationId, '00000000-0000-4000-8000-000000000000', 's1', 1)
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:desktop_target_ref')
  })

  it('refuses malformed input rather than forwarding it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    expect((await coord.attachApprovedDesktopTarget(42, WORKER, 's1', 1)).ok).toBe(false)
    expect((await coord.attachApprovedDesktopTarget(graph.view.orchestrationId, WORKER, 'hwnd:5', 1)).ok).toBe(false)
    expect((await coord.attachApprovedDesktopTarget(graph.view.orchestrationId, WORKER, 's1', 'one' as unknown as number)).ok).toBe(false)
  })
})

describe('OrchestrationCoordinator.attachApprovedApp', () => {
  it('registers an app_ref for a registered application', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedApp(graph.view.orchestrationId, 'notepad')
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.resources?.[0]?.kind).toBe('app_ref')
    expect(result.value.resources?.[0]?.backingText).toBe('notepad')
    expect(result.value.resources?.[0]?.safeLabel).toBe('Notepad')
    expect(graph.calls).toContain('register:app_ref')
  })

  it('refuses an unregistered app id, never registering a resource for it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedApp(graph.view.orchestrationId, 'not-registered')
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:app_ref')
  })

  it('refuses a non-string argument rather than forwarding it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedApp(42, 'notepad')
    expect(result.ok).toBe(false)
  })
})

describe('OrchestrationCoordinator.attachApprovedProject', () => {
  it('registers a project_ref for the one currently live, Lumi-owned run', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new FailingPlanner(),
      projectRun: { ok: true, value: { taskId: '00000000-0000-4000-8000-0000000000pp', phase: 'running' } as unknown as AgentProjectRunView }
    })
    const result = await coord.attachApprovedProject(graph.view.orchestrationId)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('unreachable')
    expect(result.value.resources?.[0]?.kind).toBe('project_ref')
    expect(result.value.resources?.[0]?.backingId).toBe('00000000-0000-4000-8000-0000000000pp')
    expect(graph.calls).toContain('register:project_ref')
  })

  it('refuses when there is no live run to attach', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner(), projectRun: { ok: true, value: null } })
    const result = await coord.attachApprovedProject(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:project_ref')
  })

  it('refuses a run that has already ended -- only a live run may be attached', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({
      graph, planner: new FailingPlanner(),
      projectRun: { ok: true, value: { taskId: '00000000-0000-4000-8000-0000000000pp', phase: 'stopped' } as unknown as AgentProjectRunView }
    })
    const result = await coord.attachApprovedProject(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    expect(graph.calls).not.toContain('register:project_ref')
  })

  it('refuses a non-string argument rather than forwarding it', async () => {
    const graph = new FakeGraph()
    const { coordinator: coord } = coordinator({ graph, planner: new FailingPlanner() })
    const result = await coord.attachApprovedProject(42)
    expect(result.ok).toBe(false)
  })
})
