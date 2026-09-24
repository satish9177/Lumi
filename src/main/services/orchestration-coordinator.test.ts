import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentResult, AgentTaskSnapshot } from '../../shared/agent-contracts'
import type { AgentProjectRunView } from '../../shared/project-contracts'
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
}): { coordinator: OrchestrationCoordinator; researchCalls: string[] } {
  const researchCalls: string[] = []
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
    getLatestProjectRun: async () => input.projectRun ?? { ok: true, value: null }
  })
  return { coordinator: coord, researchCalls }
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
    const result = await coord.createAndRun('Research the Lumi repository and summarize it')
    expect(result.ok).toBe(true)
    expect(graph.calls[0]).toBe('create')
  })

  it('refuses a capability with no dispatch handler rather than silently skipping it', async () => {
    // Not reachable through the real Python catalog (it only ever offers composed capabilities), but the
    // coordinator's own closed dispatch must still fail closed if it ever were.
    const graph = new FakeGraph()
    graph.view = orchestration({ availableCapabilities: ['document_read'] })
    const { coordinator: coord } = coordinator({
      graph, planner: new ScriptedPlanner([{ kind: 'step', capability: 'document_read', reason: 'x' }])
    })
    const result = await coord.run(graph.view.orchestrationId)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_refused')
  })
})
