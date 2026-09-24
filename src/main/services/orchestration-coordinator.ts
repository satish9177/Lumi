import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentResult, AgentTaskSnapshot } from '../../shared/agent-contracts'
import type { AgentProjectRunView } from '../../shared/project-contracts'
import type { AgentDocumentTaskView, AgentLocalComparisonView } from '../../shared/document-contracts'
import type { AgentOrchestrationResourceView, AgentOrchestrationView } from '../../shared/orchestration-contracts'
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
  getLatestOrchestration: () => Promise<AgentResult<AgentOrchestrationView | null>>
  recordPlannerCall: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  advanceOrchestration: (
    orchestrationId: string, expectedRevision: number, capabilityId: AgentCapabilityId,
    options: { taskId?: string; resolvedSummary?: string; resources?: readonly string[]; resultBackingId?: string }
  ) => Promise<AgentResult<AgentOrchestrationView>>
  resumeOrchestration: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  finishOrchestration: (orchestrationId: string, expectedRevision: number) => Promise<AgentResult<AgentOrchestrationView>>
  stopOrchestration: (orchestrationId: unknown) => Promise<AgentResult<AgentOrchestrationView>>
  /** Milestone 12 S2: makes a trusted input resource available. Never reachable from the planner. */
  registerResource: (
    orchestrationId: string, expectedRevision: number,
    options: { kind: string; safeLabel: string; backingId?: string; backingText?: string; documentTaskId?: string }
  ) => Promise<AgentResult<AgentOrchestrationView>>
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
    availableResources: readonly string[]
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
  /** The registered project's current recipe id, if any -- never chosen or invented by a planner. */
  getRegisteredRecipeId: () => Promise<AgentResult<string | null>>
  /** Milestone 10 S3's own entry point: opens the R3 "this executes code" warning card. Never approves it. */
  createProjectRun: (recipeId: string) => Promise<AgentResult<AgentProjectRunView>>
  /**
   * Milestone 10 S3's own entry point: starts a run whose grant is already ACTIVE. Idempotent ("one start
   * per approval, ever" -- `ProjectService.start()`'s own guarantee) and safe to call speculatively before
   * the grant is active, where it is simply refused. Never a second, orchestrator-only start path.
   */
  startProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
  /** Milestone 10 S1's own entry point: no approval, needs only an already-approved root. */
  createDocumentTask: (objective: string) => Promise<AgentResult<AgentDocumentTaskView>>
  /** Milestone 10 S1's own entry point: adds a file from an already-approved root. No approval of its own. */
  addDocumentFromRoot: (taskId: string, rootId: string, relativePath: string) => Promise<AgentResult<AgentDocumentTaskView>>
  /** Milestone 10 S1's own entry point: bounded local extraction. No approval, no provider call. */
  extractDocument: (taskId: string, fileId: string) => Promise<AgentResult<AgentDocumentTaskView>>
  /** Milestone 10 S1's own entry point: deterministic local comparison. No approval, no provider call. */
  compareDocumentsLocally: (
    taskId: string, firstDocumentId: string, secondDocumentId: string
  ) => Promise<AgentResult<AgentLocalComparisonView>>
}

export class OrchestrationCoordinator {
  constructor(private readonly deps: OrchestrationCoordinatorDependencies) {}

  /** Creates a new orchestration for this objective and runs it to its first pause or terminal state. */
  async createOrchestration(objectiveValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    const created = await this.deps.orchestrations.createOrchestration(objectiveValue)
    if (!created.ok) return created
    return this.run(created.value.orchestrationId)
  }

  /** Read-only passthroughs -- the renderer never needs a raw single-step primitive to see current state. */
  getOrchestration(orchestrationId: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    return this.deps.orchestrations.getOrchestration(orchestrationId)
  }

  getLatestOrchestration(): Promise<AgentResult<AgentOrchestrationView | null>> {
    return this.deps.orchestrations.getLatestOrchestration()
  }

  /**
   * Resumes a paused orchestration and runs it to its next pause or terminal state. Always re-validates
   * durable state first -- never assumes a paused step resolved just because the user pressed Continue.
   */
  continueOrchestration(orchestrationId: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    if (typeof orchestrationId !== 'string') {
      return Promise.resolve({ ok: false, error: { code: 'invalid_request', message: 'That orchestration reference is invalid.' } })
    }
    return this.run(orchestrationId)
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

    // approval_required: the ordinary "waiting on a human" pause. outcome_unknown: a linked capability's
    // own effect is unresolved -- re-checking is still the right move (a person may have since looked and
    // settled it through that capability's own reconciliation path), never a reason to stop trying.
    if (view.status === 'PAUSED' && (view.pauseReason === 'approval_required' || view.pauseReason === 'outcome_unknown')) {
      // A step-specific mechanical continuation, never a second approval: project_start's own approval is
      // the grant becoming ACTIVE through the existing warning card, and start() performs no new effect
      // beyond what that one approval already covers (ProjectService.start() is itself idempotent).
      await this.progressPendingStep(view)
      const resumed = await this.deps.orchestrations.resumeOrchestration(orchestrationId, view.revision)
      if (!resumed.ok) return resumed
      view = resumed.value
    }

    for (let iteration = 0; iteration < MAX_LOOP_ITERATIONS && view.status === 'RUNNING'; iteration += 1) {
      const counted = await this.deps.orchestrations.recordPlannerCall(orchestrationId, view.revision)
      if (!counted.ok) return counted
      view = counted.value
      if (view.status !== 'RUNNING') break

      const availableResources = (view.resources ?? []).map((resource) => resource.ref)
      let decisionKind: 'step' | 'finish' | 'stop'
      let capability: AgentCapabilityId | undefined
      let resources: readonly string[] = []
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
            steps: view.steps.map((step) => ({ sequence: step.sequence, capabilityId: step.capabilityId, status: step.status })),
            resources: (view.resources ?? []).map((resource) => ({ ref: resource.ref, kind: resource.kind, safeLabel: resource.safeLabel }))
          }),
          resultLines: orchestrationResultLines(
            view.steps.map((step) => ({ sequence: step.sequence, capabilityId: step.capabilityId, resultSummary: step.resultSummary ?? null }))
          ),
          available: view.availableCapabilities,
          availableResources
        })
        decisionKind = outcome.decision.kind
        capability = outcome.decision.kind === 'step' ? outcome.decision.capability : undefined
        resources = outcome.decision.kind === 'step' ? (outcome.decision.resources ?? []) : []
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

      const dispatched = await this.dispatch(
        orchestrationId, view.revision, capability, view.objective, resources, view.resources ?? [], view.documentTaskId
      )
      if (!dispatched.ok) return dispatched
      view = dispatched.value
    }

    return { ok: true, value: view }
  }

  stopOrchestration(orchestrationId: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    return this.deps.orchestrations.stopOrchestration(orchestrationId)
  }

  /**
   * Best-effort: for the one capability whose own approval does not by itself finish the effect
   * (`project_start`: the grant becoming ACTIVE still needs `start()` called), nudge it forward. A refusal
   * here (not yet approved, already started, a missing dependency) is swallowed -- the orchestration's own
   * `resume` re-reads the real state afterward and reports it honestly either way.
   */
  private async progressPendingStep(view: AgentOrchestrationView): Promise<void> {
    const pending = view.steps.find((step) => step.status === 'AWAITING_APPROVAL' || step.status === 'PENDING')
    if (!pending?.childTaskId || pending.capabilityId !== 'project_start') return
    await this.deps.startProjectRun(pending.childTaskId)
  }

  /**
   * Milestone 12 S2: makes one already-approved document available to this orchestration as a
   * `document_ref` resource. Never reachable from the planner or the model -- this is the trusted renderer
   * action that supplies the resource a later `document_read` step will cite. Lazily creates the one shared
   * document task this orchestration's document resources refer into (`documentTaskId`), reusing it on
   * every later call so a comparison always has both documents in the same task
   * (`DocumentService.compare_local` requires that).
   */
  async attachApprovedDocument(orchestrationIdValue: unknown, rootIdValue: unknown, relativePathValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    if (typeof orchestrationIdValue !== 'string' || typeof rootIdValue !== 'string' || typeof relativePathValue !== 'string') {
      return { ok: false, error: { code: 'invalid_request', message: 'That document reference is invalid.' } }
    }
    const orchestrationId = orchestrationIdValue
    const current = await this.deps.orchestrations.getOrchestration(orchestrationId)
    if (!current.ok) return current
    let taskId = current.value.documentTaskId
    if (taskId === undefined) {
      const created = await this.deps.createDocumentTask(current.value.objective)
      if (!created.ok) return created
      taskId = created.value.taskId
    }
    const added = await this.deps.addDocumentFromRoot(taskId, rootIdValue, relativePathValue)
    if (!added.ok) return added
    const file = added.value.files.at(-1)
    if (file === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not add that document.' } }
    }
    return this.deps.orchestrations.registerResource(orchestrationId, current.value.revision, {
      kind: 'document_ref', safeLabel: file.displayName, backingId: file.fileId, documentTaskId: taskId
    })
  }

  /**
   * The one closed place a capability id becomes a request to that capability's own boundary. Every branch
   * is a real, already-reviewed entry point; there is no default/dynamic dispatch and no way for a string
   * this module does not explicitly name to reach anything.
   */
  private async dispatch(
    orchestrationId: string, revision: number, capability: AgentCapabilityId, objective: string,
    resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[],
    documentTaskId: string | undefined
  ): Promise<AgentResult<AgentOrchestrationView>> {
    if (capability === 'public_research') {
      const created = await this.deps.createResearchTask(objective)
      if (!created.ok) return created
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, capability, { taskId: created.value.task.taskId, resources })
    }
    if (capability === 'project_status') {
      const latest = await this.deps.getLatestProjectRun()
      if (!latest.ok) return latest
      const summary = latest.value
        ? `Project run phase: ${latest.value.phase}${latest.value.ready ? ', ready' : ', not ready yet'}.`
        : 'No project run has been started yet.'
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, capability, { resolvedSummary: summary, resources })
    }
    if (capability === 'project_start') {
      const recipe = await this.deps.getRegisteredRecipeId()
      if (!recipe.ok) return recipe
      if (recipe.value === null) {
        return { ok: false, error: { code: 'orchestration_refused', message: 'No project recipe is registered to start.' } }
      }
      const created = await this.deps.createProjectRun(recipe.value)
      if (!created.ok) return created
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, capability, { taskId: created.value.taskId, resources })
    }
    if (capability === 'document_read') {
      return this.dispatchDocumentRead(orchestrationId, revision, resources, availableResources, documentTaskId)
    }
    if (capability === 'document_compare') {
      return this.dispatchDocumentCompare(orchestrationId, revision, resources, availableResources, documentTaskId)
    }
    // Not reachable in the ordinary case: the runtime only ever offers a planner the capabilities it has
    // itself composed. Fail closed rather than silently doing nothing.
    return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi does not yet know how to use that capability.' } }
  }

  /**
   * `document_read`: the planner cited exactly one `document_ref` (the runtime itself enforces this;
   * anything else is refused before this is ever called). Extracts through `DocumentService`'s own existing
   * no-approval method, then reports a summary built ONLY from a character count -- never any extracted
   * text, term or heading, matching `document_read`'s `document_private` classification.
   */
  private async dispatchDocumentRead(
    orchestrationId: string, revision: number, resources: readonly string[],
    availableResources: readonly AgentOrchestrationResourceView[], documentTaskId: string | undefined
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const resolved = this.resolveDocumentResource(resources[0], availableResources, documentTaskId, 'document_ref')
    if (resolved === null) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that document.' } }
    }
    const extracted = await this.deps.extractDocument(resolved.taskId, resolved.backingId)
    if (!extracted.ok) return extracted
    const document = extracted.value.documents.filter((item) => item.fileId === resolved.backingId).at(-1)
    if (document === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not extract that document.' } }
    }
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'document_read', {
      resources, resultBackingId: document.documentId,
      resolvedSummary: `Extracted ${document.textChars} characters from an approved document.`
    })
  }

  /**
   * `document_compare`: the planner cited exactly two `document_result_ref`s. Compares through
   * `DocumentService`'s own existing local method, then reports a summary built ONLY from the numeric
   * overlap fraction -- never any shared term, heading or quote from either document.
   */
  private async dispatchDocumentCompare(
    orchestrationId: string, revision: number, resources: readonly string[],
    availableResources: readonly AgentOrchestrationResourceView[], documentTaskId: string | undefined
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const first = this.resolveDocumentResource(resources[0], availableResources, documentTaskId, 'document_result_ref')
    const second = this.resolveDocumentResource(resources[1], availableResources, documentTaskId, 'document_result_ref')
    // `first.taskId !== second.taskId` cannot actually differ today (both come from the one
    // `documentTaskId` this orchestration owns) -- kept as defense in depth against a future change that
    // gives documents more than one task, not as the primary guarantee (that is documentTaskId itself).
    if (first === null || second === null || first.taskId !== second.taskId) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve those documents.' } }
    }
    const compared = await this.deps.compareDocumentsLocally(first.taskId, first.backingId, second.backingId)
    if (!compared.ok) return compared
    const overlapPercent = Math.round(compared.value.overlap * 100)
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'document_compare', {
      resources,
      resolvedSummary: `Local comparison: ${overlapPercent}% term overlap between two approved documents.`
    })
  }

  /**
   * Resolves a cited ref to its backing document task/id, from the SAME fresh `view` the planner was shown
   * for this call -- never a stale or separately-fetched one, and never instance state shared across
   * concurrent orchestrations. `null` if the ref is missing, is the wrong kind for this call, carries no
   * backing id, or no document task exists yet. The runtime's own `advance()` re-checks kind/ownership/
   * freshness independently and is the actual authority; this check exists so a wrong-kind citation fails
   * here, before any local `DocumentService` call is even attempted, rather than only after one runs.
   */
  private resolveDocumentResource(
    ref: string | undefined, availableResources: readonly AgentOrchestrationResourceView[],
    documentTaskId: string | undefined, expectedKind: 'document_ref' | 'document_result_ref'
  ): { taskId: string; backingId: string } | null {
    if (ref === undefined || documentTaskId === undefined) return null
    const resource = availableResources.find((item) => item.ref === ref)
    if (resource === undefined || resource.kind !== expectedKind || resource.backingId === undefined) return null
    return { taskId: documentTaskId, backingId: resource.backingId }
  }
}
