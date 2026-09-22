import type { AgentDesktopVisionView, AgentError, AgentResult } from '../../shared/agent-contracts'
import type { DesktopVisionReasoner } from '../agent/desktop-vision'
import { candidatesToWire } from '../agent/desktop-vision'
import type { LocalOcrEngine } from '../vision/ocr-engine'
import { isCaptureBlocked } from './capture'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import {
  parseDesktopVision,
  parseDisclosureProviderContext,
  parseRawCapture,
  projectDesktopVisionRuntimeError
} from './desktop-vision-wire'

/**
 * The trusted domain client for Milestone 9 S5: scoped desktop visual fallback.
 *
 * Its own class, like the S4 planning controller: `VoiceTaskBackend` is a `Pick<AgentTaskController,
 * ...>`, so a class it is never a member of cannot be reached from voice by construction.
 *
 * Two SEPARATE grant flows, matched by two SEPARATE claim methods here:
 *
 *   `runDesktopCapture`           claims the capture grant, takes ONE screenshot, runs LOCAL OCR on
 *                                  it if available, and discards the pixels -- they never leave this
 *                                  method. Nothing is sent to any provider.
 *   `runDesktopVisionDisclosure`  claims the disclosure grant, takes a BRAND NEW screenshot (never
 *                                  the capture's own bytes), sends it to the ONE approved provider,
 *                                  and records the closed candidate list (or the failure). The image
 *                                  never touches the renderer and is never persisted anywhere.
 *
 * `isCaptureBlocked()` (the same guard `capture.ts`'s own screen-capture flow already uses) is
 * checked again here, immediately before either screenshot: a login takeover or an open form-draft
 * window refuses a desktop screenshot too, exactly like it already refuses the unrelated capture path.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const MAX_OBJECTIVE = 500
const MAX_PURPOSE = 400
const MAX_TARGET_HINT = 200
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const TIMEOUTS = {
  read: 10_000,
  observe: 90_000,
  write: 30_000,
  capture: 30_000
} as const

type DesktopVisionFailureCode =
  | 'invalid_request' | 'model_unavailable' | 'not_found' | 'no_active_task' | 'desktop_read_stale'
  | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: DesktopVisionFailureCode, message: string): never {
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

function parseTaskId(value: unknown): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', 'That task reference is invalid.')
  return value
}

function parseObjective(value: unknown, label: string, max: number): string {
  if (typeof value !== 'string') fail('invalid_request', `Type ${label} first.`)
  const text = value.trim()
  if (!text || text.length > max || CONTROL_CHARS.test(text)) {
    fail('invalid_request', `Type ${label} of up to ${max} characters.`)
  }
  return text
}

export class DesktopVisionController {
  private readonly inFlight = new Set<string>()

  constructor(
    private readonly runtime: DesktopRuntimeRequester,
    private readonly reasoner: DesktopVisionReasoner | undefined,
    private readonly getOcrEngine: () => LocalOcrEngine | undefined
  ) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was sent.')
      if (error instanceof RuntimeRestartedError) {
        fail('desktop_read_stale', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectDesktopVisionRuntimeError(reply.status, reply.body))
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

  // ---- capture: local use only ------------------------------------------------------------------

  /**
   * Observe ONE surface locally (S1's silent read) and open the capture card, but only if
   * deterministic code finds UIA insufficient. Nothing is captured yet.
   */
  createDesktopCapture(
    objectiveValue: unknown, workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown,
    targetHintValue: unknown
  ): Promise<AgentResult<AgentDesktopVisionView>> {
    let objective: string
    let workerGeneration: string
    let surfaceRef: string
    let surfaceEpoch: number
    let targetHint: string | undefined
    try {
      objective = parseObjective(objectiveValue, 'what you are looking for', MAX_OBJECTIVE)
      if (typeof workerGenerationValue !== 'string' || !UUID.test(workerGenerationValue)) fail('invalid_request', 'That window reference is invalid.')
      workerGeneration = workerGenerationValue
      if (typeof surfaceRefValue !== 'string' || !SURFACE_REF.test(surfaceRefValue)) fail('invalid_request', 'That window reference is invalid.')
      surfaceRef = surfaceRefValue
      if (typeof surfaceEpochValue !== 'number' || !Number.isSafeInteger(surfaceEpochValue) || surfaceEpochValue < 1) fail('invalid_request', 'That window reference is invalid.')
      surfaceEpoch = surfaceEpochValue
      if (targetHintValue !== undefined && targetHintValue !== null) {
        targetHint = parseObjective(targetHintValue, 'a hint', MAX_TARGET_HINT)
      }
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-capture:create', async () => {
      const reply = await this.call('POST', '/desktop/captures', {
        objective,
        worker_generation: workerGeneration,
        surface_ref: surfaceRef,
        surface_epoch: surfaceEpoch,
        ...(targetHint !== undefined ? { target_hint: targetHint } : {})
      }, TIMEOUTS.observe)
      return parseDesktopVision(reply.body)
    })
  }

  getDesktopCapture(taskIdValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    try {
      taskId = parseTaskId(taskIdValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-capture:get', async () => {
      const reply = await this.call('GET', `/desktop/captures/${taskId}`, undefined, TIMEOUTS.read)
      return parseDesktopVision(reply.body)
    })
  }

  private async current(taskId: string): Promise<AgentDesktopVisionView> {
    const reply = await this.call('GET', `/desktop/captures/${taskId}`, undefined, TIMEOUTS.read)
    return parseDesktopVision(reply.body)
  }

  grantDesktopCapture(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    return this.captureGrantMutation(taskIdValue, grantIdValue, revisionValue, 'grant')
  }

  declineDesktopCapture(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    return this.captureGrantMutation(taskIdValue, grantIdValue, revisionValue, 'revoke')
  }

  private captureGrantMutation(
    taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'revoke'
  ): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    let grantId: string
    let revision: number
    try {
      taskId = parseTaskId(taskIdValue)
      grantId = parseGrantId(grantIdValue)
      revision = parseRevision(revisionValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-capture:grant', async () => {
      const current = await this.current(taskId)
      if (!current.captureCard || current.captureCard.grantId !== grantId) fail('not_found', 'That approval does not belong to the current task.')
      if (current.captureCard.grantRevision !== revision) {
        throw new AgentRequestError({
          code: 'desktop_read_stale', message: 'The approval changed since you reviewed it. Review the current card.', currentRevision: current.captureCard.grantRevision
        })
      }
      const reply = await this.call(
        'POST', `/desktop/captures/${taskId}/${kind}`, { grant_id: grantId, expected_revision: revision }, TIMEOUTS.write
      )
      return parseDesktopVision(reply.body)
    })
  }

  /**
   * Claim the single-use capture approval, take ONE screenshot, run LOCAL OCR on it if available,
   * and discard the pixels. Nothing here can reach a provider: the reasoner is never called from
   * this method.
   */
  runDesktopCapture(taskIdValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    try {
      taskId = parseTaskId(taskIdValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-capture:run', async () => {
      const current = await this.current(taskId)
      if (!current.captureCard) fail('no_active_task', 'There is no capture to take.')
      if (current.phase !== 'approved') {
        fail('desktop_read_stale', current.phase === 'awaiting_approval'
          ? 'Allow the capture on the card first. Nothing was captured.'
          : 'That approval is not usable now. Nothing was captured.')
      }
      if (isCaptureBlocked()) fail('desktop_read_stale', 'A sign-in or a form window is open. Nothing was captured.')
      const claimReply = await this.call('POST', `/desktop/captures/${taskId}/claim`, {}, TIMEOUTS.capture)
      const raw = parseRawCapture(claimReply.body)
      await this.runLocalOcr(raw.imageBase64)
      const view = await this.current(taskId)
      if (view.taskId !== taskId) throw new WireError('desktop.vision.capture.task_id')
      return view
    })
  }

  /** Runs local OCR for its own side effect (nothing is returned or stored by this controller): a
   * future step may add a way for the renderer to read it back, but this slice's job is only to
   * prove the boundary (bounded, local, never sent anywhere) is exercised end to end. A missing or
   * failing OCR engine never fails the capture itself. */
  private async runLocalOcr(imageBase64: string): Promise<void> {
    const engine = this.getOcrEngine()
    if (!engine) return
    try {
      await engine.recognize(Buffer.from(imageBase64, 'base64'))
    } catch {
      // OCR is best-effort. A missing language pack or a transient failure never blocks the capture.
    }
  }

  // ---- vision disclosure: one provider, one purpose --------------------------------------------

  /**
   * Names ONE provider/model -- chosen by main from its own configuration, exactly like the S4
   * planning controller's own `createDesktopPlan`, and never a renderer field -- plus the person's
   * own typed purpose, and opens the SEPARATE disclosure card. Nothing is sent yet.
   */
  createDesktopVisionDisclosure(taskIdValue: unknown, purposeValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    let purpose: string
    try {
      taskId = parseTaskId(taskIdValue)
      purpose = parseObjective(purposeValue, 'why you need an AI to look at this image', MAX_PURPOSE)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-vision-disclosure:create', async () => {
      const reasoner = this.reasoner
      const candidate = reasoner?.candidateProvider()
      if (!reasoner || !candidate) fail('model_unavailable', 'No AI provider is configured to look at a screenshot. Nothing was sent.')
      const reply = await this.call('POST', `/desktop/captures/${taskId}/disclosure`, {
        recipient: candidate.recipient, model: candidate.model, purpose
      }, TIMEOUTS.write)
      return parseDesktopVision(reply.body)
    })
  }

  grantDesktopVisionDisclosure(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    return this.disclosureGrantMutation(taskIdValue, grantIdValue, revisionValue, 'grant')
  }

  declineDesktopVisionDisclosure(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    return this.disclosureGrantMutation(taskIdValue, grantIdValue, revisionValue, 'revoke')
  }

  private disclosureGrantMutation(
    taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'revoke'
  ): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    let grantId: string
    let revision: number
    try {
      taskId = parseTaskId(taskIdValue)
      grantId = parseGrantId(grantIdValue)
      revision = parseRevision(revisionValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-vision-disclosure:grant', async () => {
      const current = await this.current(taskId)
      if (!current.disclosureCard || current.disclosureCard.grantId !== grantId) fail('not_found', 'That approval does not belong to the current task.')
      if (current.disclosureCard.grantRevision !== revision) {
        throw new AgentRequestError({
          code: 'desktop_read_stale', message: 'The approval changed since you reviewed it. Review the current card.', currentRevision: current.disclosureCard.grantRevision
        })
      }
      const reply = await this.call(
        'POST', `/desktop/captures/${taskId}/disclosure/${kind}`, { grant_id: grantId, expected_revision: revision }, TIMEOUTS.write
      )
      return parseDesktopVision(reply.body)
    })
  }

  /**
   * Claim the single-use disclosure approval, take a BRAND NEW screenshot (never the first
   * capture's own bytes), make ONE call to the approval's one provider, and record what it saw.
   * Order is the safety property, exactly like the S4 planning controller's own `runDesktopPlan`: the
   * provider is checked to still be configured BEFORE the claim; after the claim commits, the
   * approval is spent whatever happens next, and nothing here is ever retried or sent to another
   * provider.
   */
  runDesktopVisionDisclosure(taskIdValue: unknown): Promise<AgentResult<AgentDesktopVisionView>> {
    let taskId: string
    try {
      taskId = parseTaskId(taskIdValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('desktop-vision-disclosure:run', async () => {
      const reasoner = this.reasoner
      if (!reasoner) fail('model_unavailable', 'No AI provider is configured to look at a screenshot. Nothing was sent.')
      const current = await this.current(taskId)
      if (!current.disclosureCard) fail('no_active_task', 'There is no vision disclosure to run.')
      if (current.phase !== 'disclosure_approved') {
        fail('desktop_read_stale', current.phase === 'awaiting_disclosure_approval'
          ? 'Allow the disclosure on the card first. Nothing was sent to an AI.'
          : 'That approval is not usable now. Nothing was sent to an AI.')
      }
      const { provider, model, applicationLabel } = current.disclosureCard
      if (!reasoner.canServe(provider, model)) {
        fail('model_unavailable', 'The AI provider you approved is not available. Nothing was sent, and Lumi will not send it to another provider.')
      }
      if (isCaptureBlocked()) fail('desktop_read_stale', 'A sign-in or a form window is open. Nothing was captured or sent.')
      const claimReply = await this.call('POST', `/desktop/captures/${taskId}/disclosure/claim`, {}, TIMEOUTS.capture)
      const claim = parseDisclosureProviderContext(claimReply.body)
      if (claim.taskId !== taskId || claim.recipient !== provider || claim.model !== model) {
        throw new WireError('desktop.vision.disclosure.claim.binding')
      }
      const outcome = await reasoner.reason({
        context: {
          purpose: claim.purpose,
          applicationLabel,
          image: { mimeType: 'image/png', base64: claim.capture.imageBase64 }
        },
        taskId: claim.taskId,
        recipient: claim.recipient,
        model: claim.model
      })
      const body = outcome.kind === 'result'
        ? { disclosure_id: claim.disclosureId, result: { schema_version: 1, candidates: candidatesToWire(outcome.candidates) } }
        : { disclosure_id: claim.disclosureId, failure: outcome.code }
      const recorded = await this.call('POST', `/desktop/captures/${claim.taskId}/disclosure/result`, body, TIMEOUTS.write)
      const view = parseDesktopVision(recorded.body)
      if (view.taskId !== claim.taskId) throw new WireError('desktop.vision.task_id')
      return view
    })
  }
}
