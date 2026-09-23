import type { AgentError, AgentResult } from '../../shared/agent-contracts'
import type {
  AgentDocumentApi,
  AgentDocumentTaskView,
  AgentFileRootView,
  AgentLocalComparisonView,
  AgentRootListingView
} from '../../shared/document-contracts'
import type { DocumentComparer } from '../agent/document-comparer'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import type { DroppedFileLookup } from './dropped-files'
import {
  parseDocumentProviderContext,
  parseDocumentTask,
  parseFileRoot,
  parseFileRoots,
  parseLocalComparison,
  parseRootListing,
  projectDocumentRuntimeError
} from './document-wire'

/**
 * The trusted domain client for Milestone 10 S1: M10 file roots and approved documents.
 *
 * Its own class, like the desktop controllers: `VoiceTaskBackend` is a pick of `AgentTaskController`, so
 * nothing here is reachable from voice. Nothing here accepts an absolute path from the renderer:
 *
 * * a folder comes from a NATIVE dialog opened here (`chooseFolder`), never from a renderer argument;
 * * a dropped file is named by its opaque `droppedId` and resolved from main's own store, revalidated;
 * * a root-relative name is shape-checked here and resolved (and refused if it escapes) by the runtime.
 *
 * Extracted text leaves this class for exactly one provider, once, after a committed claim of a trusted
 * approval -- through `DocumentComparer.compare`, the single call site.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const ABSOLUTE = /^([A-Za-z]:|[\\/])/
const TIMEOUTS = { read: 10_000, write: 30_000, extract: 45_000, provider: 60_000 } as const

type DocumentFailureCode = 'invalid_request' | 'model_unavailable' | 'not_found' | 'no_active_task' | 'document_state_changed' | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: DocumentFailureCode, message: string): never {
  throw new AgentRequestError({ code, message })
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

function line(value: unknown, what: string, maximum: number, allowEmpty = false): string {
  if (typeof value !== 'string') fail('invalid_request', `Type ${what} first.`)
  const text = value.trim()
  if ((!text && !allowEmpty) || text.length > maximum || CONTROL_CHARS.test(text)) {
    fail('invalid_request', `Type ${what} of up to ${maximum} characters.`)
  }
  return text
}

/** A root-relative name as the listing showed it. Absolute, UNC, device and `..` never leave main. */
function relative(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 512 || CONTROL_CHARS.test(value) || ABSOLUTE.test(value) || value.includes(':')) {
    fail('invalid_request', 'That file reference is invalid.')
  }
  if (value.split(/[\\/]/).some((part) => part === '..' || part === '.' || part === '')) fail('invalid_request', 'That file reference is invalid.')
  return value
}

function flag(value: unknown): boolean {
  if (typeof value !== 'boolean') fail('invalid_request', 'That permission is invalid.')
  return value
}

export interface DocumentControllerDependencies {
  runtime: DesktopRuntimeRequester
  comparer: DocumentComparer | undefined
  /**
   * Opens a native folder dialog and then a native confirmation, both owned by main, naming the
   * permissions (S1 review finding 8). Resolves to the chosen folder, or undefined if the person
   * cancelled either.
   */
  chooseFolder: (grant: { label: string; canRead: boolean; canCreate: boolean }) => Promise<string | undefined>
  droppedFiles: DroppedFileLookup | undefined
}

export class DocumentController implements AgentDocumentApi {
  private readonly inFlight = new Set<string>()

  constructor(private readonly dependencies: DocumentControllerDependencies) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.dependencies.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was read or sent.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectDocumentRuntimeError(reply.status, reply.body))
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

  private guarded<T>(key: string, parse: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      parse()
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive(key, work)
  }

  // ---- roots ----------------------------------------------------------------------------------

  listFileRoots(): Promise<AgentResult<AgentFileRootView[]>> {
    return this.exclusive('roots:list', async () => parseFileRoots((await this.call('GET', '/file-roots', undefined, TIMEOUTS.read)).body))
  }

  addFileRoot(labelValue: unknown, canReadValue: unknown, canCreateValue: unknown): Promise<AgentResult<AgentFileRootView | null>> {
    let label = ''
    let canRead = false
    let canCreate = false
    return this.guarded('roots:add', () => {
      label = line(labelValue, 'a name for the folder', 64)
      canRead = flag(canReadValue)
      canCreate = flag(canCreateValue)
      if (!canRead && !canCreate) fail('invalid_request', 'Choose at least one permission.')
    }, async () => {
      // The trusted gesture: a NATIVE dialog in main. The renderer never supplies the path.
      const path = await this.dependencies.chooseFolder({ label, canRead, canCreate })
      if (!path) return null
      const reply = await this.call('POST', '/file-roots', { path, label, can_read: canRead, can_create: canCreate, can_modify: false }, TIMEOUTS.write)
      return parseFileRoot(reply.body)
    })
  }

  revokeFileRoot(rootIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentFileRootView>> {
    let rootId = ''
    let expected = 0
    return this.guarded('roots:revoke', () => {
      rootId = id(rootIdValue, 'folder')
      expected = revision(revisionValue)
    }, async () => parseFileRoot((await this.call('POST', `/file-roots/${rootId}/revoke`, { expected_revision: expected }, TIMEOUTS.write)).body))
  }

  listFileRootFiles(rootIdValue: unknown): Promise<AgentResult<AgentRootListingView>> {
    let rootId = ''
    return this.guarded('roots:files', () => {
      rootId = id(rootIdValue, 'folder')
    }, async () => parseRootListing((await this.call('GET', `/file-roots/${rootId}/files`, undefined, TIMEOUTS.write)).body))
  }

  // ---- document tasks ------------------------------------------------------------------------

  createDocumentTask(objectiveValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let objective = ''
    return this.guarded('documents:create', () => {
      objective = line(objectiveValue, 'what you want to do', 300, true)
    }, async () => parseDocumentTask((await this.call('POST', '/document-tasks', { objective }, TIMEOUTS.write)).body))
  }

  getDocumentTask(taskIdValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    return this.guarded('documents:get', () => {
      taskId = id(taskIdValue, 'task')
    }, async () => this.current(taskId))
  }

  private async current(taskId: string): Promise<AgentDocumentTaskView> {
    const view = parseDocumentTask((await this.call('GET', `/document-tasks/${taskId}`, undefined, TIMEOUTS.read)).body)
    if (view.taskId !== taskId) throw new WireError('document_task.id')
    return view
  }

  addDocumentFromRoot(taskIdValue: unknown, rootIdValue: unknown, relativeValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    let rootId = ''
    let relativePath = ''
    return this.guarded('documents:add', () => {
      taskId = id(taskIdValue, 'task')
      rootId = id(rootIdValue, 'folder')
      relativePath = relative(relativeValue)
    }, async () => parseDocumentTask(
      (await this.call('POST', `/document-tasks/${taskId}/files`, { root_id: rootId, relative_path: relativePath }, TIMEOUTS.extract)).body
    ))
  }

  addDroppedDocument(taskIdValue: unknown, droppedIdValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    let droppedId = ''
    return this.guarded('documents:add-dropped', () => {
      taskId = id(taskIdValue, 'task')
      droppedId = id(droppedIdValue, 'dropped file')
    }, async () => {
      const store = this.dependencies.droppedFiles
      const snapshot = store?.snapshot(droppedId)
      // Resolved from main's own store and revalidated now; the renderer supplied only the opaque id.
      const path = snapshot ? await store?.resolve(droppedId) : undefined
      if (!snapshot || !path) fail('not_found', 'That dropped file is no longer available. Drop it again to use it.')
      const reply = await this.call(
        'POST', `/document-tasks/${taskId}/dropped-files`, { path, display_name: snapshot.fileName }, TIMEOUTS.extract
      )
      return parseDocumentTask(reply.body)
    })
  }

  extractDocument(taskIdValue: unknown, fileIdValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    let fileId = ''
    return this.guarded('documents:extract', () => {
      taskId = id(taskIdValue, 'task')
      fileId = id(fileIdValue, 'file')
    }, async () => parseDocumentTask((await this.call('POST', `/document-tasks/${taskId}/extract`, { file_id: fileId }, TIMEOUTS.extract)).body))
  }

  compareDocumentsLocally(taskIdValue: unknown, firstValue: unknown, secondValue: unknown): Promise<AgentResult<AgentLocalComparisonView>> {
    let taskId = ''
    let first = ''
    let second = ''
    return this.guarded('documents:compare', () => {
      taskId = id(taskIdValue, 'task')
      first = id(firstValue, 'document')
      second = id(secondValue, 'document')
    }, async () => parseLocalComparison(
      (await this.call('POST', `/document-tasks/${taskId}/compare`, { first_document_id: first, second_document_id: second }, TIMEOUTS.write)).body
    ))
  }

  // ---- the exact disclosure ------------------------------------------------------------------

  createDocumentDisclosure(taskIdValue: unknown, documentIdsValue: unknown, purposeValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    let documentIds: string[] = []
    let purpose = ''
    return this.guarded('documents:disclosure', () => {
      taskId = id(taskIdValue, 'task')
      if (!Array.isArray(documentIdsValue) || documentIdsValue.length < 1 || documentIdsValue.length > 2) fail('invalid_request', 'Choose one or two documents.')
      documentIds = documentIdsValue.map((value) => id(value, 'document'))
      if (new Set(documentIds).size !== documentIds.length) fail('invalid_request', 'Choose two different documents.')
      purpose = line(purposeValue, 'why you want an AI to compare them', 400)
    }, async () => {
      const candidate = this.dependencies.comparer?.candidate()
      if (!candidate) fail('model_unavailable', 'No AI provider is configured to compare documents. Nothing was sent.')
      // The provider and model are main's choice from its own configuration, shown on the card.
      const reply = await this.call('POST', `/document-tasks/${taskId}/disclosure`, {
        document_ids: documentIds, recipient: candidate.recipient, model: candidate.model, purpose
      }, TIMEOUTS.write)
      return parseDocumentTask(reply.body)
    })
  }

  grantDocumentDisclosure(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    return this.grantMutation(taskIdValue, grantIdValue, revisionValue, 'grant')
  }

  declineDocumentDisclosure(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    return this.grantMutation(taskIdValue, grantIdValue, revisionValue, 'revoke')
  }

  private grantMutation(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'revoke'): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    let grantId = ''
    let expected = 0
    return this.guarded('documents:grant', () => {
      taskId = id(taskIdValue, 'task')
      grantId = id(grantIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      const current = await this.current(taskId)
      if (!current.card || current.card.grantId !== grantId) fail('not_found', 'That approval does not belong to this task.')
      if (current.card.grantRevision !== expected) {
        throw new AgentRequestError({
          code: 'document_state_changed', message: 'The approval changed since you reviewed it. Review the current card.', currentRevision: current.card.grantRevision
        })
      }
      const reply = await this.call('POST', `/document-tasks/${taskId}/disclosure/${kind}`, { grant_id: grantId, expected_revision: expected }, TIMEOUTS.write)
      return parseDocumentTask(reply.body)
    })
  }

  /**
   * Claim the single-use approval, make ONE call to its one provider, record what came back. The provider
   * is checked to still be configured BEFORE the claim; after the claim commits the approval is spent
   * whatever happens, and nothing here is ever retried or sent to another provider.
   */
  runDocumentDisclosure(taskIdValue: unknown): Promise<AgentResult<AgentDocumentTaskView>> {
    let taskId = ''
    return this.guarded('documents:run', () => {
      taskId = id(taskIdValue, 'task')
    }, async () => {
      const comparer = this.dependencies.comparer
      if (!comparer) fail('model_unavailable', 'No AI provider is configured to compare documents. Nothing was sent.')
      const current = await this.current(taskId)
      if (!current.card) fail('no_active_task', 'There is no comparison to run.')
      if (current.phase !== 'approved') {
        fail('document_state_changed', current.phase === 'awaiting_approval'
          ? 'Allow the comparison on the card first. Nothing was sent.'
          : 'That approval is not usable now. Nothing was sent.')
      }
      const { provider, model } = current.card
      if (!comparer.canServe(provider, model)) {
        fail('model_unavailable', 'The AI provider you approved is not available. Nothing was sent, and Lumi will not send it to another provider.')
      }
      const claim = parseDocumentProviderContext((await this.call('POST', `/document-tasks/${taskId}/disclosure/claim`, {}, TIMEOUTS.write)).body)
      if (claim.taskId !== taskId || claim.recipient !== provider || claim.model !== model) throw new WireError('document_claim.binding')
      const outcome = await comparer.compare({ projection: claim.projection, taskId, recipient: claim.recipient, model: claim.model })
      const body = outcome.kind === 'result'
        ? { disclosure_id: claim.disclosureId, result: outcome.result }
        : { disclosure_id: claim.disclosureId, failure: outcome.code }
      return parseDocumentTask((await this.call('POST', `/document-tasks/${taskId}/disclosure/result`, body, TIMEOUTS.write)).body)
    })
  }
}
