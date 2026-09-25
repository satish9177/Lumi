import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type {
  AgentBrowserProfileView,
  AgentDesktopActionView,
  AgentDesktopReadView,
  AgentDesktopScrollStep,
  AgentDesktopScrollTargetList,
  AgentDesktopSurfaceList,
  AgentRegisteredApp,
  AgentResult,
  AgentTaskSnapshot
} from '../../shared/agent-contracts'
import type { AgentProjectRunView } from '../../shared/project-contracts'
import type { AgentDocumentTaskView, AgentLocalComparisonView } from '../../shared/document-contracts'
import type { AgentOrchestrationResourceView, AgentOrchestrationView } from '../../shared/orchestration-contracts'
import {
  ModelRoutingError,
  orchestrationResultLines,
  orchestrationStateLines,
  type OrchestrationDesktopSafeActionOperation,
  type OrchestrationPlanOutcome
} from '../agent/orchestration-planner'

/** Milestone 12 S4: phases in which a supervised run may still be meaningfully stopped. */
const STOPPABLE_PROJECT_PHASES: ReadonlySet<string> = new Set(['starting', 'running', 'ready'])

/** `s1`..`s16` `|` a monotonic epoch, exactly `DESKTOP_TARGET_BACKING_PATTERN` on the Python side. */
const DESKTOP_TARGET_REF_PATTERN = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|(s(?:[1-9]|1[0-6]))\|([1-9][0-9]{0,8})$/

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
  /**
   * Milestone 12 S3: Milestone 8a S3's own entry point for `account_read` -- the same task creation and
   * trusted scope card a direct request already shows, over an already-authenticated profile.
   */
  createAccountReadTask: (objective: string, profileId: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Milestone 12 S3: the Continue-time re-observe nudge for a paused `account_read` step's linked task --
   * one forced, fresh observation, never an assumption that the requested human action happened.
   */
  continueAccountRead: (taskId: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Milestone 8a S2's own entry point: every browser profile main knows about, signed-in or not. */
  listBrowserProfiles: () => Promise<AgentResult<AgentBrowserProfileView[]>>
  /** Milestone 9 S1's own entry point: the user-visible Windows surfaces, read-only. */
  listDesktopSurfaces: () => Promise<AgentResult<AgentDesktopSurfaceList>>
  /**
   * Milestone 9 S1's own entry point: one bounded, local, read-only observation. Never disclosed to any
   * provider or forwarded into the orchestrator's own context beyond the bounded facts this method itself
   * returns -- desktop text stays private and untrusted, exactly as M9 S1 requires.
   */
  observeDesktopTarget: (
    workerGeneration: string, surfaceRef: string, surfaceEpoch: number
  ) => Promise<AgentResult<{ nodeCount: number; truncated: boolean }>>
  /**
   * Milestone 9 S2's own entry point: observes the surface locally and opens the existing disclosure card.
   * Nothing is sent to a provider until the human approves and runs it on that card's own existing surface.
   */
  createDesktopReasonTask: (
    objective: string, target: { workerGeneration: string; surfaceRef: string; surfaceEpoch: number }
  ) => Promise<AgentResult<AgentDesktopReadView>>
  /** Milestone 9 S3's own entry point: opens the exact focus approval card. Nothing happens yet. */
  proposeDesktopFocus: (
    workerGeneration: string, surfaceRef: string, surfaceEpoch: number
  ) => Promise<AgentResult<AgentDesktopActionView>>
  /**
   * Milestone 9 S3's own entry point: a fresh local observation listing only the scrollable controls.
   * Trusted code -- never the planner -- chooses which one a scroll step actually targets.
   */
  findDesktopScrollTargets: (
    workerGeneration: string, surfaceRef: string, surfaceEpoch: number
  ) => Promise<AgentResult<AgentDesktopScrollTargetList>>
  /** Milestone 9 S3's own entry point: opens the exact scroll approval card. Nothing happens yet. */
  proposeDesktopScroll: (
    workerGeneration: string, observationId: string, controlRef: string, step: AgentDesktopScrollStep
  ) => Promise<AgentResult<AgentDesktopActionView>>
  /** Milestone 9 S3's own entry point: every application the user has already registered. */
  listDesktopApps: () => Promise<AgentResult<AgentRegisteredApp[]>>
  /** Milestone 9 S3's own entry point: opens the exact launch approval card for a registered app. */
  proposeDesktopLaunch: (appId: string) => Promise<AgentResult<AgentDesktopActionView>>
  /** Milestone 10 S3's own entry point: ends only this run's own supervised process job. */
  stopProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
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
    // manual_handoff_required (Milestone 12 S3): a human was asked to act outside Lumi (sign in, clear a
    // CAPTCHA); Continue never assumes that happened -- `progressPendingStep` forces one fresh, real
    // re-observation of the linked task before anything here is treated as resolved.
    if (view.status === 'PAUSED' && (
      view.pauseReason === 'approval_required' || view.pauseReason === 'outcome_unknown' ||
      view.pauseReason === 'manual_handoff_required'
    )) {
      // A step-specific mechanical continuation, never a second approval: project_start's own approval is
      // the grant becoming ACTIVE through the existing warning card, and start() performs no new effect
      // beyond what that one approval already covers (ProjectService.start() is itself idempotent).
      // account_read's own continuation is a real re-observation, never a second approval either -- see
      // `progressPendingStep`.
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
      let operation: OrchestrationDesktopSafeActionOperation | undefined
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
        operation = outcome.decision.kind === 'step' ? outcome.decision.operation : undefined
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
        orchestrationId, view.revision, capability, view.objective, resources, view.resources ?? [], view.documentTaskId,
        operation
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
   * Best-effort: for a capability whose own approval does not by itself finish the effect, or whose pause
   * needs a real re-observation rather than a passive re-read, nudge it forward. A refusal here (not yet
   * approved, already started, a missing dependency) is swallowed -- the orchestration's own `resume`
   * re-reads the real state afterward and reports it honestly either way, never assuming this call's own
   * outcome.
   *
   * `project_start`: the grant becoming ACTIVE still needs `start()` called.
   * `account_read` (Milestone 12 S3): a `manual_handoff_required` pause (login, an unrecognised or changed
   * account, a navigation outside the approved site) is never cleared just because the user pressed
   * Continue -- `continueAccountRead` attempts one fresh, forced observation against the linked task and
   * reports whatever it actually finds, including "still paused" or "this account is wrong; refused".
   */
  private async progressPendingStep(view: AgentOrchestrationView): Promise<void> {
    const pending = view.steps.find((step) => step.status === 'AWAITING_APPROVAL' || step.status === 'PENDING')
    if (!pending?.childTaskId) return
    if (pending.capabilityId === 'project_start') {
      await this.deps.startProjectRun(pending.childTaskId)
      return
    }
    // Only a real manual handoff needs a fresh, forced observation: the ordinary `approval_required` wait
    // (nobody has confirmed the scope card yet, so there is no active grant at all) has nothing to
    // re-observe, and `outcome_unknown` is not account_read's own concern here either.
    if (pending.capabilityId === 'account_read' && view.pauseReason === 'manual_handoff_required') {
      await this.deps.continueAccountRead(pending.childTaskId)
    }
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
   * Milestone 12 S3: makes one already-authenticated browser profile available to this orchestration as an
   * `account_context_ref` resource. Never reachable from the planner or the model -- this is the trusted
   * renderer action, exactly like approving a file root or attaching a document: the user themselves picks
   * which signed-in profile the orchestration may later choose to read under. Selecting `account_read` on
   * the resulting ref is still not approval -- the linked `authenticated_read` task's own existing scope
   * card gates every read, exactly as a direct request would.
   */
  async attachApprovedAccount(orchestrationIdValue: unknown, profileIdValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    if (typeof orchestrationIdValue !== 'string' || typeof profileIdValue !== 'string') {
      return { ok: false, error: { code: 'invalid_request', message: 'That account reference is invalid.' } }
    }
    const profiles = await this.deps.listBrowserProfiles()
    if (!profiles.ok) return profiles
    const profile = profiles.value.find((item) => item.profileId === profileIdValue)
    if (profile === undefined || profile.status !== 'AUTHENTICATED' || profile.activeTakeover) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Choose one of your signed-in profiles.' } }
    }
    const current = await this.deps.orchestrations.getOrchestration(orchestrationIdValue)
    if (!current.ok) return current
    return this.deps.orchestrations.registerResource(orchestrationIdValue, current.value.revision, {
      kind: 'account_context_ref', safeLabel: `approved signed-in account context for ${profile.site}`, backingId: profile.profileId
    })
  }

  /**
   * Milestone 12 S4: makes one currently-live Windows window available to this orchestration as a
   * `desktop_target_ref` resource. Never reachable from the planner or the model -- the renderer supplies
   * only an opaque choice from `listDesktopSurfaces()`'s own current listing; main re-checks that choice
   * against a FRESH listing of its own before registering (and the runtime itself re-checks a third time --
   * see `app/services/orchestration.py`'s own `register_resource`), so a stale or invented window cannot be
   * attached even if the renderer's own listing was already out of date by the time this call arrives.
   */
  async attachApprovedDesktopTarget(
    orchestrationIdValue: unknown, workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown
  ): Promise<AgentResult<AgentOrchestrationView>> {
    if (
      typeof orchestrationIdValue !== 'string' || typeof workerGenerationValue !== 'string' ||
      typeof surfaceRefValue !== 'string' || typeof surfaceEpochValue !== 'number'
    ) {
      return { ok: false, error: { code: 'invalid_request', message: 'That desktop window reference is invalid.' } }
    }
    const surfaces = await this.deps.listDesktopSurfaces()
    if (!surfaces.ok) return surfaces
    if (surfaces.value.workerGeneration !== workerGenerationValue) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'That window is no longer available. Choose one of the windows Lumi currently sees.' } }
    }
    const surface = surfaces.value.surfaces.find(
      (item) => item.surfaceRef === surfaceRefValue && item.surfaceEpoch === surfaceEpochValue
    )
    if (surface === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'That window is no longer available. Choose one of the windows Lumi currently sees.' } }
    }
    const current = await this.deps.orchestrations.getOrchestration(orchestrationIdValue)
    if (!current.ok) return current
    return this.deps.orchestrations.registerResource(orchestrationIdValue, current.value.revision, {
      kind: 'desktop_target_ref',
      safeLabel: `approved desktop window: ${surface.applicationLabel}`,
      backingText: `${workerGenerationValue}|${surfaceRefValue}|${surfaceEpochValue}`
    })
  }

  /**
   * Milestone 12 S4: makes one already-registered application available to this orchestration as an
   * `app_ref` resource. The renderer supplies only an app id from `listDesktopApps()`'s own trusted
   * registry listing; main re-checks membership before registering, and `DesktopActionService.propose_launch`
   * independently re-checks the registry again at dispatch.
   */
  async attachApprovedApp(orchestrationIdValue: unknown, appIdValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    if (typeof orchestrationIdValue !== 'string' || typeof appIdValue !== 'string') {
      return { ok: false, error: { code: 'invalid_request', message: 'That application reference is invalid.' } }
    }
    const apps = await this.deps.listDesktopApps()
    if (!apps.ok) return apps
    const app = apps.value.find((item) => item.appId === appIdValue)
    if (app === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Choose one of your registered applications.' } }
    }
    const current = await this.deps.orchestrations.getOrchestration(orchestrationIdValue)
    if (!current.ok) return current
    return this.deps.orchestrations.registerResource(orchestrationIdValue, current.value.revision, {
      kind: 'app_ref', safeLabel: app.label, backingText: app.appId
    })
  }

  /**
   * Milestone 12 S4: makes the one currently Lumi-owned, live supervised project run available to this
   * orchestration as a `project_ref` resource. There is no picker: the renderer may only attach the SAME
   * run `getLatestProjectRun()` -- the same read `project_status` already uses -- currently reports, never a
   * run it names itself. `ProjectService.stop()` independently re-checks ownership and phase again at
   * dispatch.
   */
  async attachApprovedProject(orchestrationIdValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    if (typeof orchestrationIdValue !== 'string') {
      return { ok: false, error: { code: 'invalid_request', message: 'That orchestration reference is invalid.' } }
    }
    const run = await this.deps.getLatestProjectRun()
    if (!run.ok) return run
    if (run.value === null || !STOPPABLE_PROJECT_PHASES.has(run.value.phase)) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'There is no running project to attach.' } }
    }
    const current = await this.deps.orchestrations.getOrchestration(orchestrationIdValue)
    if (!current.ok) return current
    return this.deps.orchestrations.registerResource(orchestrationIdValue, current.value.revision, {
      kind: 'project_ref', safeLabel: 'registered project run', backingId: run.value.taskId
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
    documentTaskId: string | undefined, operation: OrchestrationDesktopSafeActionOperation | undefined
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
    if (capability === 'account_read') {
      return this.dispatchAccountRead(orchestrationId, revision, objective, resources, availableResources)
    }
    if (capability === 'desktop_observe') {
      return this.dispatchDesktopObserve(orchestrationId, revision, resources, availableResources)
    }
    if (capability === 'desktop_reason') {
      return this.dispatchDesktopReason(orchestrationId, revision, objective, resources, availableResources)
    }
    if (capability === 'desktop_safe_action') {
      return this.dispatchDesktopSafeAction(orchestrationId, revision, resources, availableResources, operation ?? 'focus')
    }
    if (capability === 'launch_registered_app') {
      return this.dispatchLaunchRegisteredApp(orchestrationId, revision, resources, availableResources)
    }
    if (capability === 'project_stop') {
      return this.dispatchProjectStop(orchestrationId, revision, resources, availableResources)
    }
    // Not reachable in the ordinary case: the runtime only ever offers a planner the capabilities it has
    // itself composed. Fail closed rather than silently doing nothing.
    return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi does not yet know how to use that capability.' } }
  }

  /**
   * Resolves a cited ref to the desktop window identity it names -- `(workerGeneration, surfaceRef,
   * surfaceEpoch)` only, never a HWND, a PID or a coordinate. `null` if the ref is missing, the wrong kind,
   * or its backing text is not shaped as `DESKTOP_TARGET_BACKING_PATTERN` demands (defense in depth: the
   * runtime's own `register_resource` already refused anything else at mint time). The freshness re-check
   * itself happens inside the EXISTING M9 call this identity is handed to next (`observeDesktopTarget`,
   * `createDesktopReasonTask`, `proposeDesktopFocus`/`proposeDesktopScroll`) -- never here, and never
   * skipped: a recreated or closed window fails there exactly as a direct request already would.
   */
  private resolveDesktopTarget(
    ref: string | undefined, availableResources: readonly AgentOrchestrationResourceView[]
  ): { workerGeneration: string; surfaceRef: string; surfaceEpoch: number } | null {
    if (ref === undefined) return null
    const resource = availableResources.find((item) => item.ref === ref)
    if (resource === undefined || resource.kind !== 'desktop_target_ref' || resource.backingText === undefined) return null
    const match = DESKTOP_TARGET_REF_PATTERN.exec(resource.backingText)
    if (match === null) return null
    return { workerGeneration: match[1], surfaceRef: match[2], surfaceEpoch: Number(match[3]) }
  }

  /**
   * `desktop_observe`: reads the approved window locally, through M9 S1's own existing, unchanged
   * observation call, then reports a summary built ONLY from a bounded node count -- never a role, a name
   * or any observed text. Application UI stays `untrusted_environment`; nothing here lets it reach the
   * planner's own context or become a capability request.
   */
  private async dispatchDesktopObserve(
    orchestrationId: string, revision: number, resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[]
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const target = this.resolveDesktopTarget(resources[0], availableResources)
    if (target === null) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that desktop window.' } }
    }
    const observed = await this.deps.observeDesktopTarget(target.workerGeneration, target.surfaceRef, target.surfaceEpoch)
    if (!observed.ok) return observed
    const summary = `Desktop observation completed: ${observed.value.nodeCount} element(s) observed${observed.value.truncated ? ', truncated' : ''}.`
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'desktop_observe', { resolvedSummary: summary, resources })
  }

  /**
   * `desktop_reason`: opens the SAME existing desktop-disclosure card a direct request would, over the
   * approved window the ref resolves to. Nothing is read or sent until the human approves and runs it on
   * that card's own existing surface -- selecting `desktop_reason` here is never itself approval.
   */
  private async dispatchDesktopReason(
    orchestrationId: string, revision: number, objective: string,
    resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[]
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const target = this.resolveDesktopTarget(resources[0], availableResources)
    if (target === null) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that desktop window.' } }
    }
    const created = await this.deps.createDesktopReasonTask(objective, target)
    if (!created.ok) return created
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'desktop_reason', { taskId: created.value.taskId, resources })
  }

  /**
   * `desktop_safe_action`: focus or one semantic scroll step, matching the catalog's own closed
   * description -- never M9 S4's `SetValue`/`Select`/`Invoke` mutations, which this module never opens a
   * task for under any capability. `operation` is the one closed sub-choice `OrchestrationPlanner` allows
   * for this capability alone; a scroll's actual target control is chosen by trusted code
   * (`findDesktopScrollTargets`'s own first result), never by the planner.
   */
  private async dispatchDesktopSafeAction(
    orchestrationId: string, revision: number, resources: readonly string[],
    availableResources: readonly AgentOrchestrationResourceView[], operation: OrchestrationDesktopSafeActionOperation
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const target = this.resolveDesktopTarget(resources[0], availableResources)
    if (target === null) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that desktop window.' } }
    }
    if (operation === 'focus') {
      const proposed = await this.deps.proposeDesktopFocus(target.workerGeneration, target.surfaceRef, target.surfaceEpoch)
      if (!proposed.ok) return proposed
      return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'desktop_safe_action', { taskId: proposed.value.taskId, resources })
    }
    const targets = await this.deps.findDesktopScrollTargets(target.workerGeneration, target.surfaceRef, target.surfaceEpoch)
    if (!targets.ok) return targets
    const first = targets.value.targets[0]
    if (first === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi found nothing scrollable in that window.' } }
    }
    const step: AgentDesktopScrollStep = operation === 'scroll_down' ? 'small_down' : 'small_up'
    const proposed = await this.deps.proposeDesktopScroll(target.workerGeneration, targets.value.observationId, first.controlRef, step)
    if (!proposed.ok) return proposed
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'desktop_safe_action', { taskId: proposed.value.taskId, resources })
  }

  /**
   * `launch_registered_app`: the planner cited exactly one `app_ref`. Opens the SAME existing launch
   * approval card a direct request would, for the registered application that ref resolves to -- never an
   * exe path, an argument or an unregistered location.
   */
  private async dispatchLaunchRegisteredApp(
    orchestrationId: string, revision: number, resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[]
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const ref = resources[0]
    const resource = ref === undefined ? undefined : availableResources.find((item) => item.ref === ref)
    if (resource === undefined || resource.kind !== 'app_ref' || resource.backingText === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that application.' } }
    }
    const proposed = await this.deps.proposeDesktopLaunch(resource.backingText)
    if (!proposed.ok) return proposed
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'launch_registered_app', { taskId: proposed.value.taskId, resources })
  }

  /**
   * `project_stop`: the planner cited exactly one `project_ref`. Stops through `ProjectService.stop()`'s
   * own existing entry point -- ending only that run's own supervised process job -- and reports a summary
   * built ONLY from the resulting phase, exactly like `project_status`.
   */
  private async dispatchProjectStop(
    orchestrationId: string, revision: number, resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[]
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const ref = resources[0]
    const resource = ref === undefined ? undefined : availableResources.find((item) => item.ref === ref)
    if (resource === undefined || resource.kind !== 'project_ref' || resource.backingId === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that project run.' } }
    }
    const stopped = await this.deps.stopProjectRun(resource.backingId)
    if (!stopped.ok) return stopped
    const summary = `Project run stopped (phase: ${stopped.value.phase}).`
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'project_stop', { resolvedSummary: summary, resources })
  }

  /**
   * `account_read`: the planner cited exactly one `account_context_ref` (the runtime itself enforces this;
   * anything else is refused before this is ever called). Creates the linked `authenticated_read` task
   * through `AuthenticatedReadService`'s own existing boundary -- its own scope card, grant and disclosure
   * recipient, unchanged -- over the profile that ref resolves to. Nothing is opened, read or sent until the
   * human confirms that task's own card, exactly as a direct request would.
   */
  private async dispatchAccountRead(
    orchestrationId: string, revision: number, objective: string,
    resources: readonly string[], availableResources: readonly AgentOrchestrationResourceView[]
  ): Promise<AgentResult<AgentOrchestrationView>> {
    const ref = resources[0]
    const resource = ref === undefined ? undefined : availableResources.find((item) => item.ref === ref)
    if (resource === undefined || resource.kind !== 'account_context_ref' || resource.backingId === undefined) {
      return { ok: false, error: { code: 'orchestration_refused', message: 'Lumi could not resolve that account.' } }
    }
    const created = await this.deps.createAccountReadTask(objective, resource.backingId)
    if (!created.ok) return created
    return this.deps.orchestrations.advanceOrchestration(orchestrationId, revision, 'account_read', { taskId: created.value.task.taskId, resources })
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
