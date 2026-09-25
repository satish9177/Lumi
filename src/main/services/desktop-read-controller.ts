import type {
  AgentDesktopReadView,
  AgentDesktopSurfaceList,
  AgentError,
  AgentResult
} from '../../shared/agent-contracts'
import type { DesktopReader } from '../agent/desktop-reader'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import {
  parseDesktopClaim,
  parseDesktopRead,
  parseDesktopScanSummary,
  parseDesktopSurfaceList,
  parseLatestDesktopRead,
  projectDesktopRuntimeError,
  type AgentDesktopScanSummary
} from './desktop-read-wire'

/**
 * The trusted domain client for Milestone 9 S2: explicit desktop disclosure and read-only reasoning.
 *
 * Deliberately its own class, not a method group on `AgentTaskController`: `VoiceTaskBackend` is a
 * `Pick<AgentTaskController, ...>`, so a class this is never a member of cannot be reached from voice
 * by construction. It also does not import the request interpreter, the conversation window, memory or
 * the diagnostics-of-content anything: the user's typed question goes from the renderer to the local
 * runtime and, only after the trusted click, to the ONE approved provider. It never passes through the
 * conversational model first.
 *
 * What this class cannot do is the design. Every mutation takes ids and the revision the trusted card
 * showed. There is no method that takes a provider, a model, a snapshot, a digest, a handle, a process,
 * a selector, coordinates, or an action, and none that focuses, invokes, types into, selects, scrolls,
 * clicks or launches anything. The provider is chosen here, from main's own configuration, when the
 * read is created; the renderer never supplies it.
 */

export interface DesktopRuntimeRequester {
  request(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply>
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const MAX_OBJECTIVE = 500
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/

const TIMEOUTS = {
  read: 10_000,
  // Listing may start the desktop worker cold (measured 16-21 s on this machine), and one
  // observation has its own 30 s deadline in the runtime.
  list: 60_000,
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
  if (typeof value !== 'string') fail('invalid_request', 'Type a question about the window first.')
  const text = value.trim()
  if (!text || text.length > MAX_OBJECTIVE || CONTROL_CHARS.test(text)) {
    fail('invalid_request', `Type a question of up to ${MAX_OBJECTIVE} characters about the window.`)
  }
  return text
}

export class DesktopReadController {
  private readonly inFlight = new Set<string>()

  constructor(
    private readonly runtime: DesktopRuntimeRequester,
    private readonly reader: DesktopReader | undefined
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
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectDesktopRuntimeError(reply.status, reply.body))
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

  /** Read-only: the user-visible surfaces. Takes no argument. The strings are display text only. */
  listDesktopSurfaces(): Promise<AgentResult<AgentDesktopSurfaceList>> {
    return this.exclusive('desktop:list', async () => {
      const reply = await this.call('GET', '/desktop/surfaces', undefined, TIMEOUTS.list)
      return parseDesktopSurfaceList(reply.body)
    })
  }

  /**
   * Milestone 12 S4: one bounded, local, read-only S1 scan of an already-approved window, for the
   * orchestrator's own `desktop_observe` capability. Returns ONLY a node count and a truncation flag --
   * never a node, a role, a name or any scanned text, which stays private and untrusted and never
   * reaches this method's own caller, let alone a provider.
   */
  observeDesktopSurface(
    workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown
  ): Promise<AgentResult<AgentDesktopScanSummary>> {
    let workerGeneration: string
    let surfaceRef: string
    let surfaceEpoch: number
    try {
      if (typeof workerGenerationValue !== 'string' || !UUID.test(workerGenerationValue)) fail('invalid_request', 'That window reference is invalid.')
      workerGeneration = workerGenerationValue
      if (typeof surfaceRefValue !== 'string' || !SURFACE_REF.test(surfaceRefValue)) fail('invalid_request', 'That window reference is invalid.')
      surfaceRef = surfaceRefValue
      if (typeof surfaceEpochValue !== 'number' || !Number.isSafeInteger(surfaceEpochValue) || surfaceEpochValue < 1) fail('invalid_request', 'That window reference is invalid.')
      surfaceEpoch = surfaceEpochValue
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop:observe', async () => {
      const reply = await this.call('POST', '/desktop/observations', {
        worker_generation: workerGeneration, surface_ref: surfaceRef, surface_epoch: surfaceEpoch
      }, TIMEOUTS.observe)
      return parseDesktopScanSummary(reply.body)
    })
  }

  /**
   * Choose ONE surface and type a question: Lumi inspects it locally and opens the trusted card.
   * Nothing is sent to any provider. If no provider is configured, nothing is even inspected.
   */
  createDesktopRead(
    objectiveValue: unknown, workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown
  ): Promise<AgentResult<AgentDesktopReadView>> {
    let objective: string
    let workerGeneration: string
    let surfaceRef: string
    let surfaceEpoch: number
    try {
      objective = parseObjective(objectiveValue)
      if (typeof workerGenerationValue !== 'string' || !UUID.test(workerGenerationValue)) fail('invalid_request', 'That window reference is invalid.')
      workerGeneration = workerGenerationValue
      if (typeof surfaceRefValue !== 'string' || !SURFACE_REF.test(surfaceRefValue)) fail('invalid_request', 'That window reference is invalid.')
      surfaceRef = surfaceRefValue
      if (typeof surfaceEpochValue !== 'number' || !Number.isSafeInteger(surfaceEpochValue) || surfaceEpochValue < 1) fail('invalid_request', 'That window reference is invalid.')
      surfaceEpoch = surfaceEpochValue
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop:create', async () => {
      // The one recipient, chosen from main's own configuration -- never by the renderer.
      const candidate = this.reader?.candidate()
      if (!candidate) {
        fail('model_unavailable', 'No AI provider is configured to read a window. Nothing was inspected and nothing was sent.')
      }
      const reply = await this.call('POST', '/desktop/read-tasks', {
        objective,
        recipient: candidate.recipient,
        model: candidate.model,
        worker_generation: workerGeneration,
        surface_ref: surfaceRef,
        surface_epoch: surfaceEpoch
      }, TIMEOUTS.observe)
      return parseDesktopRead(reply.body)
    })
  }

  /** Read-only: the newest desktop read, or none. */
  getDesktopRead(): Promise<AgentResult<AgentDesktopReadView | null>> {
    return this.exclusive('desktop:get', async () => {
      const reply = await this.call('GET', '/desktop/read-tasks/latest', undefined, TIMEOUTS.read)
      return parseLatestDesktopRead(reply.body)
    })
  }

  private async latest(): Promise<AgentDesktopReadView | null> {
    const reply = await this.call('GET', '/desktop/read-tasks/latest', undefined, TIMEOUTS.read)
    return parseLatestDesktopRead(reply.body)
  }

  /** The trusted "Allow once" click. Only the grant shown, at the revision shown. */
  grantDesktopDisclosure(grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopReadView>> {
    return this.grantMutation(grantIdValue, revisionValue, 'grant')
  }

  /** The trusted "Cancel" click. Before the claim nothing was sent. */
  declineDesktopDisclosure(grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopReadView>> {
    return this.grantMutation(grantIdValue, revisionValue, 'decline')
  }

  private grantMutation(
    grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'decline'
  ): Promise<AgentResult<AgentDesktopReadView>> {
    let grantId: string
    let revision: number
    try {
      grantId = parseGrantId(grantIdValue)
      revision = parseRevision(revisionValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop:grant', async () => {
      const current = await this.latest()
      if (!current?.card || current.card.grantId !== grantId) fail('not_found', 'That approval does not belong to the current desktop read.')
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
      const reply = await this.call('POST', `/desktop/read-tasks/${current.taskId}/${kind === 'grant' ? 'grant' : 'revoke'}`, body, TIMEOUTS.write)
      const view = parseDesktopRead(reply.body)
      if (view.taskId !== current.taskId) throw new WireError('desktop.read.task_id')
      return view
    })
  }

  /**
   * Run the approved read: claim the single-use approval, then make ONE call to the approval's one
   * provider and model, then record what came back.
   *
   * Order is the safety property. The provider is checked to still be configured BEFORE the claim, so
   * an unavailable provider spends nothing. After the claim has committed, the approval is spent
   * whatever happens: a failure, a timeout, unparseable output or a lost response is final, is never
   * retried and never goes to another provider. If Lumi cannot record the result (the runtime went
   * away), the runtime's own record says `OUTCOME_UNKNOWN` and the user must approve again.
   */
  runDesktopRead(): Promise<AgentResult<AgentDesktopReadView>> {
    return this.exclusive('desktop:run', async () => {
      const reader = this.reader
      if (!reader) fail('model_unavailable', 'No AI provider is configured to read a window. Nothing was sent.')
      const current = await this.latest()
      if (!current?.card) fail('no_active_task', 'There is no desktop read to run.')
      if (current.phase !== 'approved') {
        fail('desktop_read_stale', current.phase === 'awaiting_approval'
          ? 'Allow the disclosure on the card first. Nothing was sent to an AI.'
          : 'That approval is not usable now. Nothing was sent to an AI.')
      }
      const { recipient, model } = current.card
      if (!reader.canServe(recipient, model)) {
        fail('model_unavailable', 'The AI provider you approved is not available. Nothing was sent, and Lumi will not send it to another provider.')
      }
      const claimReply = await this.call('POST', `/desktop/read-tasks/${current.taskId}/disclosure`, {}, TIMEOUTS.write)
      const claim = parseDesktopClaim(claimReply.body)
      if (claim.taskId !== current.taskId || claim.recipient !== recipient || claim.model !== model) {
        throw new WireError('desktop.claim.binding')
      }
      // ONE attempt. Whatever this returns is final.
      const outcome = await reader.read({
        context: { objective: claim.objective, projection: claim.projection },
        taskId: claim.taskId,
        recipient: claim.recipient,
        model: claim.model
      })
      const body = outcome.kind === 'result'
        ? { disclosure_id: claim.disclosureId, result: outcome.result }
        : { disclosure_id: claim.disclosureId, failure: outcome.code }
      const recorded = await this.call('POST', `/desktop/read-tasks/${claim.taskId}/result`, body, TIMEOUTS.write)
      const view = parseDesktopRead(recorded.body)
      if (view.taskId !== claim.taskId) throw new WireError('desktop.read.task_id')
      return view
    })
  }
}
