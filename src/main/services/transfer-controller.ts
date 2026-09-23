import type { AgentError, AgentResult } from '../../shared/agent-contracts'
import type { AgentTransferApi, AgentTransferCardView, AgentTransferView } from '../../shared/transfer-contracts'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import { parseLatestTransfer, parseTransfer, projectTransferRuntimeError } from './transfer-wire'

/**
 * The trusted domain client for Milestone 10 S2: one controlled download into one approved folder.
 *
 * Its own class (not reachable from voice). The renderer supplies a URL, a folder id, a file name and an
 * intent; the runtime checks each against its own policy. There is no path, overwrite flag, header, cookie
 * or "open" anywhere here. The approval is confirmed twice: the renderer's card, then a NATIVE message box
 * owned by main that names the source, the folder and the name read back from the runtime -- so a
 * compromised renderer cannot approve a download on its own.
 *
 * Download and placement are two separate clicks, each spending one single-use step at most once. Nothing
 * here retries: an uncertain step is settled only by `reconcileTransfer`, which never downloads again.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const TIMEOUTS = { read: 10_000, write: 30_000, download: 90_000, place: 30_000 } as const

type TransferFailureCode = 'invalid_request' | 'not_found' | 'transfer_state_changed' | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: TransferFailureCode, message: string, currentRevision?: number): never {
  throw new AgentRequestError({ code, message, ...(currentRevision !== undefined ? { currentRevision } : {}) })
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

function id(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', `That ${what} reference is invalid.`)
  return value
}

function revision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) fail('invalid_request', 'That approval revision is invalid.')
  return value
}

function line(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string') fail('invalid_request', `Type ${what} first.`)
  const text = value.trim()
  if (!text || text.length > maximum || CONTROL_CHARS.test(text)) fail('invalid_request', `Type ${what} of up to ${maximum} characters.`)
  return text
}

/** An http(s) address with no credentials. The runtime applies the real destination policy. */
function address(value: unknown): string {
  const text = line(value, 'the address to download', 2048)
  let parsed: URL
  try {
    parsed = new URL(text)
  } catch {
    fail('invalid_request', 'Type a full web address that starts with https://.')
  }
  if ((parsed.protocol !== 'https:' && parsed.protocol !== 'http:') || parsed.username || parsed.password) {
    fail('invalid_request', 'Type a full web address that starts with https://.')
  }
  return text
}

/** Just a name: never a separator, drive, stream or parent reference. The runtime validates it fully. */
function fileName(value: unknown): string {
  const name = line(value, 'a file name', 128)
  if (/[\\/:*?"<>|]/.test(name) || name === '.' || name === '..') fail('invalid_request', 'Type just a file name, not a folder or path.')
  return name
}

export interface TransferConfirmation {
  sourceUrl: string
  sourceOrigin: string
  destRootLabel: string
  destName: string
  expectedKind: string
  maxBytes: number
}

export interface TransferControllerDependencies {
  runtime: DesktopRuntimeRequester
  /** A NATIVE confirmation owned by main. Resolves true only if the person chose Allow. */
  confirmTransfer: (card: TransferConfirmation) => Promise<boolean>
}

export class TransferController implements AgentTransferApi {
  private readonly inFlight = new Set<string>()

  constructor(private readonly dependencies: TransferControllerDependencies) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.dependencies.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was downloaded or saved.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectTransferRuntimeError(reply.status, reply.body))
    return reply
  }

  private async guarded<T>(key: string, parse: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      parse()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    // One step per transfer at a time: a double click can never become two requests.
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

  private async current(taskId: string): Promise<AgentTransferView> {
    const view = parseTransfer((await this.call('GET', `/transfers/${taskId}`, undefined, TIMEOUTS.read)).body)
    if (view.taskId !== taskId) throw new WireError('transfer.id')
    return view
  }

  private async approvedCard(taskId: string, grantId: string, expected: number): Promise<AgentTransferCardView> {
    const current = await this.current(taskId)
    if (!current.card || current.card.grantId !== grantId) fail('not_found', 'That approval does not belong to this download.')
    if (current.card.grantRevision !== expected) {
      fail('transfer_state_changed', 'The approval changed since you reviewed it. Review the current card.', current.card.grantRevision)
    }
    return current.card
  }

  createTransfer(urlValue: unknown, rootIdValue: unknown, nameValue: unknown, intentValue: unknown): Promise<AgentResult<AgentTransferView>> {
    let url = ''
    let rootId = ''
    let name = ''
    let intent = ''
    return this.guarded('transfers:create', () => {
      url = address(urlValue)
      rootId = id(rootIdValue, 'folder')
      name = fileName(nameValue)
      intent = line(intentValue, 'what this file is', 300)
    }, async () => parseTransfer(
      (await this.call('POST', '/transfers', { url, root_id: rootId, file_name: name, intent }, TIMEOUTS.write)).body
    ))
  }

  getTransfer(taskIdValue: unknown): Promise<AgentResult<AgentTransferView>> {
    let taskId = ''
    return this.guarded(`transfers:get:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'download')
    }, async () => this.current(taskId))
  }

  getLatestTransfer(): Promise<AgentResult<AgentTransferView | null>> {
    return this.guarded('transfers:latest', () => undefined, async () =>
      parseLatestTransfer((await this.call('GET', '/transfers/latest', undefined, TIMEOUTS.read)).body))
  }

  grantTransfer(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentTransferView | null>> {
    let taskId = ''
    let grantId = ''
    let expected = 0
    return this.guarded(`transfers:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'download')
      grantId = id(grantIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      const card = await this.approvedCard(taskId, grantId, expected)
      // Main's own confirmation, built from what the RUNTIME holds, not from anything the renderer said.
      const allowed = await this.dependencies.confirmTransfer({
        sourceUrl: card.sourceUrl,
        sourceOrigin: card.sourceOrigin,
        destRootLabel: card.destRootLabel,
        destName: card.destName,
        expectedKind: card.expectedKind,
        maxBytes: card.maxBytes
      })
      if (!allowed) return null
      const reply = await this.call('POST', `/transfers/${taskId}/grant`, { grant_id: grantId, expected_revision: expected }, TIMEOUTS.write)
      return parseTransfer(reply.body)
    })
  }

  declineTransfer(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentTransferView>> {
    let taskId = ''
    let grantId = ''
    let expected = 0
    return this.guarded(`transfers:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'download')
      grantId = id(grantIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      await this.approvedCard(taskId, grantId, expected)
      const reply = await this.call('POST', `/transfers/${taskId}/revoke`, { grant_id: grantId, expected_revision: expected }, TIMEOUTS.write)
      return parseTransfer(reply.body)
    })
  }

  downloadTransfer(taskIdValue: unknown): Promise<AgentResult<AgentTransferView>> {
    return this.step(taskIdValue, 'download', TIMEOUTS.download)
  }

  placeTransfer(taskIdValue: unknown): Promise<AgentResult<AgentTransferView>> {
    return this.step(taskIdValue, 'place', TIMEOUTS.place)
  }

  reconcileTransfer(taskIdValue: unknown): Promise<AgentResult<AgentTransferView>> {
    return this.step(taskIdValue, 'reconcile', TIMEOUTS.read)
  }

  private step(taskIdValue: unknown, step: 'download' | 'place' | 'reconcile', timeoutMs: number): Promise<AgentResult<AgentTransferView>> {
    let taskId = ''
    return this.guarded(`transfers:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'download')
    }, async () => {
      const view = parseTransfer((await this.call('POST', `/transfers/${taskId}/${step}`, {}, timeoutMs)).body)
      if (view.taskId !== taskId) throw new WireError('transfer.id')
      return view
    })
  }
}
