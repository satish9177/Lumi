import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentResult, AgentTaskSnapshot } from '../../shared/agent-contracts'
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
}): {
  coordinator: OrchestrationCoordinator; researchCalls: string[]; startCalls: string[]
  extractCalls: Array<{ taskId: string; fileId: string }>; compareCalls: Array<{ taskId: string; first: string; second: string }>
} {
  const researchCalls: string[] = []
  const startCalls: string[] = []
  const extractCalls: Array<{ taskId: string; fileId: string }> = []
  const compareCalls: Array<{ taskId: string; first: string; second: string }> = []
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
    }
  })
  return { coordinator: coord, researchCalls, startCalls, extractCalls, compareCalls }
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
    graph.view = orchestration({ availableCapabilities: ['account_read'] })
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'account_read', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_refused')
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
