import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentResult, AgentTaskSnapshot } from '../../shared/agent-contracts'
import type { AgentProjectRunView } from '../../shared/project-contracts'
import type { AgentOrchestrationView } from '../../shared/orchestration-contracts'
import {
  ModelRoutingError,
  orchestrationResultLines,
  orchestrationStateLines,
  type OrchestrationPlanOutcome
} from '../agent/orchestration-planner'

/**
 * What the coordinator needs from the durable graph client -- an interface, not the concrete
 * `OrchestrationController`, so a test double can satisfy it structurally without a fake HTTP runtime.
 * `OrchestrationController` implements this automatically; it is the only production implementation.
 */
export interface OrchestrationGraphClient {
  createOrchestration: (objective: unknown) => Promise<AgentResult<AgentOrchestrationView>>
  getOrchestration: (orchestrationId: unknown) => Promise<AgentResult<AgentOrchestrationView>>
  recordPlannerCall: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  advanceOrchestration: (
    orchestrationId: string, expectedRevision: number, capabilityId: AgentCapabilityId,
    options: { taskId?: string; resolvedSummary?: string }
  ) => Promise<AgentResult<AgentOrchestrationView>>
  resumeOrchestration: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  finishOrchestration: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  stopOrchestration: (orchestrationId: unknown) => Promise<AgentResult<AgentOrchestrationView>>
}

/**
 * What the coordinator needs from the planner -- an interface, not the concrete `OrchestrationPlanner`, for
 * the same structural-testability reason as `OrchestrationGraphClient`.
 */
export interface OrchestrationPlannerLike {
  next: (input: {
    objective: string
    orchestrationId: string
    facts: readonly string[]
    resultLines: readonly string[]
    available: readonly AgentCapabilityId[]
  }) => Promise<OrchestrationPlanOutcome>
}

/**
 * Milestone 11 S2: the orchestration loop.
 *
 * `OrchestrationController` only records durable state; `OrchestrationPlanner` only chooses a capability
 * id. This class is the "observe -> choose ONE next capability -> controller validates -> capability runs
 * through its normal boundary -> persist result -> re-plan" loop the plan doc describes, and nothing else:
 * it holds no tool of its own and cannot execute a capability directly. `dispatch` is the one closed place
 * a chosen capability id is turned into a request to that capability's own existing entry point -- exactly
 * the same call a direct user request makes, including that capability's own approval/grant/disclosure
 * card. A capability outside this closed dispatch is refused, never silently skipped or half-run.
 *
 * The loop stops at the first pause (approval required, a budget, a detected loop) or terminal state
 * (finished, stopped) and returns. Progressing past a pause happens the normal way -- through that
 * capability's own existing UI -- and calling `run` again resumes from durable state, never from anything
 * held in memory here.
 */

//: Defensive only: the runtime enforces its own step budget and will pause first in the ordinary case.
const MAX_LOOP_ITERATIONS = 20

export interface OrchestrationCoordinatorDependencies {
  orchestrations: OrchestrationGraphClient
  planner: OrchestrationPlannerLike
  /** Milestone 7b's own entry point: creates the task and opens its scope card. Never grants it. */
  createResearchTask: (objective: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Milestone 10 S3's own entry point: a pure read, no side effect. */
  getLatestProjectRun: () => Promise<AgentResult<AgentProjectRunView | null>>
}

export class OrchestrationCoordinator {
  constructor(private readonly deps: OrchestrationCoordinatorDependencies) {}

  /** Creates a new orchestration for this objective and runs it to its first pause or terminal state. */
  async createAndRun(objectiveValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    const created = await this.deps.orchestrations.createOrchestration(objectiveValue)
    if (!created.ok) return created
    return this.run(created.value.orchestrationId)
  }

  /**
   * Resumes and continues an existing orchestration to its next pause or terminal state. Safe to call on
   * an already-paused or already-terminal orchestration: it re-checks durable state first and does nothing
   * further once nothing has changed.
   */
  async run(orchestrationId: string): Promise<AgentResult<AgentOrchestrationView>> {
    const loaded = await this.deps.orchestrations.getOrchestration(orchestrationId)
    if (!loaded.ok) return loaded
    let view = loaded.value

    if (view.status === 'PAUSED' && view.pauseReason === 'approval_required') {
      const resumed = await this.deps.orchestrations.resumeOrchestration(orchestrationId, view.revision)
      if (!resumed.ok) return resumed
      view = resumed.value
    }

    for (let iteration = 0; iteration < MAX_LOOP_ITERATIONS && view.status === 'RUNNING'; iteration += 1) {
      const counted = await this.deps.orchestrations.recordPlannerCall(orchestrationId, view.revision)
      if (!counted.ok) return counted
      view = counted.value
      if (view.status !== 'RUNNING') break

      let decisionKind: 'step' | 'finish' | 'stop'
      let capability: AgentCapabilityId | undefined
      try {
        const outcome = await this.deps.planner.next({
          objective: view.objective,
          orchestrationId,
          facts: orchestrationStateLines({
            orchestrationId,
            status: view.status,
            pauseReason: view.pauseReason ?? null,
            stepCount: view.stepCount,
            maxSteps: 20,
            plannerCalls: view.plannerCalls,
            maxPlannerCalls: 20,
            available: view.availableCapabilities,
            steps: view.steps.map((step) => ({ sequence: step.sequence, capabilityId: step.capabilityId, status: step.status }))
          }),
          resultLines: orchestrationResultLines(
            view.steps.map((step) => ({ sequence: step.sequence, capabilityId: step.capabilityId, resultSummary: step.resultSummary ?? null }))
          ),
          available: view.availableCapabilities
        })
        decisionKind = outcome.decision.kind
        capability = outcome.decision.kind === 'step' ? outcome.decision.capability : undefined
      } catch (error) {
        if (!(error instanceof ModelRoutingError)) throw error
        // No provider could decide the next step. The orchestration stays RUNNING (a transient outage is
        // not a reason to stop permanently); the caller may simply try `run` again later.
        return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not decide the next step right now. Nothing was done.' } }
      }

      if (decisionKind === 'finish') {
        const finished = await this.deps.orchestrations.finishOrchestration(orchestrationId, view.revision)
        if (!finished.ok) return finished
        view = finished.value
        break
      }
      if (decisionKind === 'stop' || capability === undefined) {
        const stopped = await this.deps.orchestrations.stopOrchestration(orchestrationId)
        if (!stopped.ok) return stopped
        view = stopped.value
        break
      }

      const dispatched = await this.dispatch(orchestrationId, view.revision, capability, view.objective)
      if (!dispatched.ok) return dispatched
      view = dispatched.value
    }

    return { ok: true, value: view }
  }

  stop(orchestrationId: string): Promise<AgentResult<AgentOrchestrationView>> {
    return this.deps.orchestrations.stopOrchestration(orchestrationId)
  }

  /**
   * The one closed place a capability id becomes a request to that capability's own boundary. Every branch
   * is a real, already-reviewed entry point; there is no default/dynamic dispatch and no way for a string
   * this module does not explicitly name to reach anything.
   */
  private async dispatch(
    orchestrationId: string, revision: number, capability: AgentCapabilityId, objective: string
  ): Promise<AgentResult<AgentOrchestrationView>> {
    if (capability === 'public_research') {
      const created = await this.deps.createResearchTask(objective)
      if (!created.ok) return created
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, capability, { taskId: created.value.task.taskId })
    }
    if (capability === 'project_status') {
      const latest = await this.deps.getLatestProjectRun()
      if (!latest.ok) return latest
      const summary = latest.value
        ? `Project run phase: ${latest.value.phase}${latest.value.ready ? ', ready' : ', not ready yet'}.`
        : 'No project run has been started yet.'
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, capability, { resolvedSummary: summary })
    }
    // Not reachable in the ordinary case: the runtime only ever offers a planner the capabilities it has
    // itself composed. Fail closed rather than silently doing nothing.
    return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi does not yet know how to use that capability.' } }
  }
}
