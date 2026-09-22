import type {
  AgentDesktopPlanView,
  AgentError,
  AgentPlanValueInput,
  AgentResult
} from '../../shared/agent-contracts'
import type { DesktopPlanner } from '../agent/desktop-planner'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import {
  parseDesktopPlan,
  parseDesktopPlanClaim,
  parseLatestDesktopPlan,
  projectDesktopPlanRuntimeError
} from './desktop-planning-wire'

/**
 * The trusted domain client for Milestone 9 S4: bounded desktop-action planning.
 *
 * Its own class, like `DesktopReadController`: `VoiceTaskBackend` is a `Pick<AgentTaskController, ...>`,
 * so a class it is never a member of cannot be reached from voice by construction. It imports no
 * interpreter or memory code. The objective and candidate values go from the renderer to the local
 * runtime and, only after the trusted click, to the ONE approved provider -- never through the
 * conversational model first.
 *
 * **This is disclosure authority only.** Nothing this class does can run anything: `runDesktopPlan`
 * ends with a validated, recorded PROPOSAL. Turning that into something that can actually run is
 * the desktop action controller's own "from a plan" method, a separate class, a separate trusted card,
 * a separate approval.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const MAX_OBJECTIVE = 500
const MAX_VALUES = 4
const MAX_VALUE_CHARS = 500
const MAX_CLASSIFICATION_CHARS = 32
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/

const TIMEOUTS = {
  read: 10_000,
  observe: 90_000,
  write: 30_000
} as const

function fail(code: AgentError['code'], message: string): never {
  throw new AgentRequestError({ code, message })
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

function parseGrantId(value: unknown): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', 'That approval reference is invalid.')
  return value
}

function parseRevision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) fail('invalid_request', 'That approval revision is invalid.')
  return value
}

function parseObjective(value: unknown): string {
  if (typeof value !== 'string') fail('invalid_request', 'Type what you want Lumi to do first.')
  const text = value.trim()
  if (!text || text.length > MAX_OBJECTIVE || CONTROL_CHARS.test(text)) {
    fail('invalid_request', `Type an objective of up to ${MAX_OBJECTIVE} characters.`)
  }
  return text
}

function parseValues(values: AgentPlanValueInput[]): Array<{ classification: string; value: string }> {
  if (!Array.isArray(values) || values.length > MAX_VALUES) fail('invalid_request', `Type at most ${MAX_VALUES} candidate values.`)
  return values.map((item) => {
    const classification = typeof item?.classification === 'string' ? item.classification.trim() : ''
    if (!classification || classification.length > MAX_CLASSIFICATION_CHARS || CONTROL_CHARS.test(classification)) {
      fail('invalid_request', 'Give each candidate value a short label.')
    }
    if (typeof item?.value !== 'string' || item.value.length > MAX_VALUE_CHARS || CONTROL_CHARS.test(item.value)) {
      fail('invalid_request', `Each candidate value must be at most ${MAX_VALUE_CHARS} characters.`)
    }
    return { classification, value: item.value }
  })
}

export class DesktopPlanningController {
  private readonly inFlight = new Set<string>()

  constructor(
    private readonly runtime: DesktopRuntimeRequester,
    private readonly planner: DesktopPlanner | undefined
  ) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was sent.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectDesktopPlanRuntimeError(reply.status, reply.body))
    return reply
  }

  private async exclusive<T>(key: string, work: () => Promise<T>): Promise<AgentResult<T>> {
    if (this.inFlight.has(key)) return { ok: false, error: { code: 'busy', message: 'Lumi is already working on that.' } }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  /**
   * Choose ONE surface, type an objective and (optionally) up to 4 candidate values: Lumi inspects
   * the window locally and opens the planning card. Nothing is sent to any provider yet.
   */
  createDesktopPlan(
    objectiveValue: unknown, workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown,
    valuesValue: unknown
  ): Promise<AgentResult<AgentDesktopPlanView>> {
    let objective: string
    let workerGeneration: string
    let surfaceRef: string
    let surfaceEpoch: number
    let values: Array<{ classification: string; value: string }>
    try {
      objective = parseObjective(objectiveValue)
      if (typeof workerGenerationValue !== 'string' || !UUID.test(workerGenerationValue)) fail('invalid_request', 'That window reference is invalid.')
      workerGeneration = workerGenerationValue
      if (typeof surfaceRefValue !== 'string' || !SURFACE_REF.test(surfaceRefValue)) fail('invalid_request', 'That window reference is invalid.')
      surfaceRef = surfaceRefValue
      if (typeof surfaceEpochValue !== 'number' || !Number.isSafeInteger(surfaceEpochValue) || surfaceEpochValue < 1) fail('invalid_request', 'That window reference is invalid.')
      surfaceEpoch = surfaceEpochValue
      values = parseValues(Array.isArray(valuesValue) ? valuesValue as AgentPlanValueInput[] : [])
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-plan:create', async () => {
      const candidate = this.planner?.candidate()
      if (!candidate) fail('model_unavailable', 'No AI provider is configured to plan a desktop action. Nothing was inspected and nothing was sent.')
      const reply = await this.call('POST', '/desktop/action-plans', {
        objective,
        recipient: candidate.recipient,
        model: candidate.model,
        worker_generation: workerGeneration,
        surface_ref: surfaceRef,
        surface_epoch: surfaceEpoch,
        values: values.map((item) => ({ classification: item.classification, value: item.value }))
      }, TIMEOUTS.observe)
      return parseDesktopPlan(reply.body)
    })
  }

  getDesktopPlan(): Promise<AgentResult<AgentDesktopPlanView | null>> {
    return this.exclusive('desktop-plan:get', async () => {
      const reply = await this.call('GET', '/desktop/action-plans/latest', undefined, TIMEOUTS.read)
      return parseLatestDesktopPlan(reply.body)
    })
  }

  private async latest(): Promise<AgentDesktopPlanView | null> {
    const reply = await this.call('GET', '/desktop/action-plans/latest', undefined, TIMEOUTS.read)
    return parseLatestDesktopPlan(reply.body)
  }

  grantDesktopPlan(grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopPlanView>> {
    return this.grantMutation(grantIdValue, revisionValue, 'grant')
  }

  declineDesktopPlan(grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopPlanView>> {
    return this.grantMutation(grantIdValue, revisionValue, 'decline')
  }

  private grantMutation(
    grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'decline'
  ): Promise<AgentResult<AgentDesktopPlanView>> {
    let grantId: string
    let revision: number
    try {
      grantId = parseGrantId(grantIdValue)
      revision = parseRevision(revisionValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-plan:grant', async () => {
      const current = await this.latest()
      if (!current?.card || current.card.grantId !== grantId) fail('not_found', 'That approval does not belong to the current desktop plan.')
      if (current.card.grantRevision !== revision) {
        throw new AgentRequestError({
          code: 'desktop_read_stale',
          message: 'The approval changed since you reviewed it. Review the current card.',
          currentRevision: current.card.grantRevision
        })
      }
      const body = kind === 'grant'
        ? { grant_id: grantId, expected_revision: revision }
        : { grant_id: grantId, expected_revision: revision, reason: 'user_declined' }
      const reply = await this.call('POST', `/desktop/action-plans/${current.taskId}/${kind === 'grant' ? 'grant' : 'revoke'}`, body, TIMEOUTS.write)
      const view = parseDesktopPlan(reply.body)
      if (view.taskId !== current.taskId) throw new WireError('desktop.plan.task_id')
      return view
    })
  }

  /**
   * Claim the single-use approval, make ONE call to the approval's one provider, and record what it
   * proposed. Order is the safety property, exactly like `DesktopReadController.runDesktopRead`: the
   * provider is checked to still be configured BEFORE the claim; after the claim commits the approval
   * is spent whatever happens next, and nothing here is ever retried or sent to another provider.
   */
  runDesktopPlan(): Promise<AgentResult<AgentDesktopPlanView>> {
    return this.exclusive('desktop-plan:run', async () => {
      const planner = this.planner
      if (!planner) fail('model_unavailable', 'No AI provider is configured to plan a desktop action. Nothing was sent.')
      const current = await this.latest()
      if (!current?.card) fail('no_active_task', 'There is no desktop plan to run.')
      if (current.phase !== 'approved') {
        fail('desktop_read_stale', current.phase === 'awaiting_approval'
          ? 'Allow the disclosure on the card first. Nothing was sent to an AI.'
          : 'That approval is not usable now. Nothing was sent to an AI.')
      }
      const { recipient, model } = current.card
      if (!planner.canServe(recipient, model)) {
        fail('model_unavailable', 'The AI provider you approved is not available. Nothing was sent, and Lumi will not send it to another provider.')
      }
      const claimReply = await this.call('POST', `/desktop/action-plans/${current.taskId}/claim`, {}, TIMEOUTS.write)
      const claim = parseDesktopPlanClaim(claimReply.body)
      if (claim.taskId !== current.taskId || claim.recipient !== recipient || claim.model !== model) {
        throw new WireError('desktop.plan.claim.binding')
      }
      const outcome = await planner.plan({
        context: { objective: claim.objective, projection: claim.projection, values: claim.values },
        taskId: claim.taskId,
        recipient: claim.recipient,
        model: claim.model
      })
      const body = outcome.kind === 'result'
        ? { plan_id: claim.planId, result: outcome.result }
        : { plan_id: claim.planId, failure: outcome.code }
      const recorded = await this.call('POST', `/desktop/action-plans/${claim.taskId}/result`, body, TIMEOUTS.write)
      const view = parseDesktopPlan(recorded.body)
      if (view.taskId !== claim.taskId) throw new WireError('desktop.plan.task_id')
      return view
    })
  }
}
