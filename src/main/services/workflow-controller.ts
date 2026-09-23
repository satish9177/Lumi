import type { AgentError, AgentResult, AgentTaskSnapshot, AgentWorkflowProvenance } from '../../shared/agent-contracts'
import type { AgentWorkflowAdoptionView, AgentWorkflowApi, AgentWorkflowView } from '../../shared/workflow-contracts'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import { parseLatestWorkflow, parseWorkflow, projectWorkflowRuntimeError } from './workflow-wire'

/**
 * The trusted domain client for Milestone 10 S4: one cross-app preparation workflow.
 *
 * Its own class (not reachable from voice or from any planner). It only creates steps and manages candidate
 * adoption; every step's own approvals stay where they were (the S2 download card and its native dialog, the
 * S1 disclosure card, the account-reading card, the planning grant and the exact form manifest). There is no
 * method here that submits, uploads, clicks or presses a key.
 *
 * Adopting a candidate is confirmed twice: the renderer's card, then a NATIVE message box owned by main that
 * shows the detail's kind, its exact value and where it came from, read back from the RUNTIME -- so a
 * compromised renderer cannot adopt a value by itself, nor relabel a provider suggestion as its own text.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const TIMEOUTS = { read: 10_000, write: 30_000, documents: 60_000 } as const

type WorkflowFailureCode = 'invalid_request' | 'not_found' | 'workflow_state_changed' | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: WorkflowFailureCode, message: string, currentRevision?: number): never {
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

function fileName(value: unknown): string {
  const name = line(value, 'a file name', 128)
  if (/[\\/:*?"<>|]/.test(name) || name === '.' || name === '..') fail('invalid_request', 'Type just a file name, not a folder or path.')
  return name
}

export interface AdoptionConfirmation {
  kind: string
  value: string
  provenance: AgentWorkflowProvenance
  documentLabel: string
  workflowObjective: string
}

export interface WorkflowControllerDependencies {
  runtime: DesktopRuntimeRequester
  /** A NATIVE confirmation owned by main. Resolves true only if the person chose Adopt. */
  confirmAdoption: (card: AdoptionConfirmation) => Promise<boolean>
  /**
   * Make the workflow's form step the active account task and open its account-reading card (PENDING), as
   * `AgentTaskController.createAuthenticatedTask` does. Absent in builds without account reading.
   */
  activateFormTask?: (taskId: string, recipientId: string) => Promise<AgentResult<AgentTaskSnapshot>>
}

export class WorkflowController implements AgentWorkflowApi {
  private readonly inFlight = new Set<string>()

  constructor(private readonly dependencies: WorkflowControllerDependencies) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.dependencies.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was prepared.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectWorkflowRuntimeError(reply.status, reply.body))
    return reply
  }

  private async guarded<T>(key: string, parse: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      parse()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    // One request per workflow at a time: a double click can never become two steps or two adoptions.
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

  private async current(workflowId: string): Promise<AgentWorkflowView> {
    const view = parseWorkflow((await this.call('GET', `/workflows/${workflowId}`, undefined, TIMEOUTS.read)).body)
    if (view.workflowId !== workflowId) throw new WireError('workflow.id')
    return view
  }

  private async post(workflowId: string, suffix: string, body: unknown, timeoutMs: number): Promise<AgentWorkflowView> {
    const view = parseWorkflow((await this.call('POST', `/workflows/${workflowId}/${suffix}`, body, timeoutMs)).body)
    if (view.workflowId !== workflowId) throw new WireError('workflow.id')
    return view
  }

  private async pendingAdoption(workflowId: string, actionId: string, expected: number): Promise<{ workflow: AgentWorkflowView; card: AgentWorkflowAdoptionView }> {
    const workflow = await this.current(workflowId)
    const card = workflow.adoptions.find((item) => item.actionId === actionId)
    if (!card) fail('not_found', 'That adoption does not belong to this workflow.')
    if (card.revision !== expected) {
      fail('workflow_state_changed', 'The adoption changed since you reviewed it. Review the current card.', card.revision)
    }
    return { workflow, card }
  }

  createWorkflow(objectiveValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let objective = ''
    return this.guarded('workflows:create', () => {
      objective = line(objectiveValue, 'what this workflow is for', 300)
    }, async () => parseWorkflow((await this.call('POST', '/workflows', { objective }, TIMEOUTS.write)).body))
  }

  getWorkflow(workflowIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    return this.guarded(`workflows:get:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
    }, async () => this.current(workflowId))
  }

  getLatestWorkflow(): Promise<AgentResult<AgentWorkflowView | null>> {
    return this.guarded('workflows:latest', () => undefined, async () =>
      parseLatestWorkflow((await this.call('GET', '/workflows/latest', undefined, TIMEOUTS.read)).body))
  }

  startWorkflowDownload(
    workflowIdValue: unknown, urlValue: unknown, rootIdValue: unknown, nameValue: unknown, intentValue: unknown
  ): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    let body: Record<string, string> = {}
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      body = {
        url: address(urlValue),
        root_id: id(rootIdValue, 'folder'),
        file_name: fileName(nameValue),
        intent: line(intentValue, 'what this file is', 300)
      }
    }, async () => this.post(workflowId, 'download', body, TIMEOUTS.write))
  }

  startWorkflowDocuments(workflowIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
    }, async () => this.post(workflowId, 'documents', {}, TIMEOUTS.documents))
  }

  extractWorkflowCandidates(workflowIdValue: unknown, documentIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    let documentId = ''
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      documentId = id(documentIdValue, 'document')
    }, async () => this.post(workflowId, 'candidates/extract', { document_id: documentId }, TIMEOUTS.write))
  }

  deriveWorkflowCandidates(workflowIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
    }, async () => this.post(workflowId, 'candidates/derive', {}, TIMEOUTS.write))
  }

  proposeWorkflowAdoption(workflowIdValue: unknown, candidateIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    let candidateId = ''
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      candidateId = id(candidateIdValue, 'detail')
    }, async () => this.post(workflowId, `candidates/${candidateId}/adopt`, {}, TIMEOUTS.write))
  }

  approveWorkflowAdoption(workflowIdValue: unknown, actionIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentWorkflowView | null>> {
    let workflowId = ''
    let actionId = ''
    let expected = 0
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      actionId = id(actionIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      const { workflow, card } = await this.pendingAdoption(workflowId, actionId, expected)
      if (card.actionStatus !== 'WAITING_APPROVAL' || card.value === undefined) {
        fail('workflow_state_changed', 'That adoption is no longer waiting for you.')
      }
      // Main's own confirmation, built from what the RUNTIME holds, not from anything the renderer said.
      const allowed = await this.dependencies.confirmAdoption({
        kind: card.kind,
        value: card.value,
        provenance: card.provenance,
        documentLabel: card.documentLabel,
        workflowObjective: workflow.objective
      })
      if (!allowed) return null
      await this.call('POST', `/workflows/adoptions/${actionId}/approve`, { expected_revision: expected }, TIMEOUTS.write)
      return this.current(workflowId)
    })
  }

  rejectWorkflowAdoption(workflowIdValue: unknown, actionIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    let actionId = ''
    let expected = 0
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      actionId = id(actionIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      await this.pendingAdoption(workflowId, actionId, expected)
      await this.call('POST', `/workflows/adoptions/${actionId}/reject`, { expected_revision: expected }, TIMEOUTS.write)
      return this.current(workflowId)
    })
  }

  startWorkflowForm(
    workflowIdValue: unknown, profileIdValue: unknown, objectiveValue: unknown, recipientIdValue: unknown
  ): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    let profileId = ''
    let objective = ''
    let recipientId = ''
    return this.guarded(`workflows:step:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
      profileId = id(profileIdValue, 'account')
      objective = line(objectiveValue, 'what to prepare', 300)
      recipientId = line(recipientIdValue, 'an AI provider', 32)
    }, async () => {
      const activate = this.dependencies.activateFormTask
      if (!activate) fail('invalid_request', 'Account forms are not available in this build.')
      // Idempotent: a form step created a moment ago (whose card could not be opened) is re-activated, never
      // duplicated -- the runtime also refuses a second form step.
      let view = await this.current(workflowId)
      if (!view.steps.some((item) => item.role === 'form')) {
        view = await this.post(workflowId, 'form', { profile_id: profileId, objective }, TIMEOUTS.write)
      }
      const form = view.steps.find((item) => item.role === 'form')
      if (!form) throw new WireError('workflow.form')
      if (form.taskStatus !== 'CREATED') return view  // already active and carded; the account cards drive it
      const activated = await activate(form.taskId, recipientId)
      if (!activated.ok) throw new AgentRequestError(activated.error)
      return this.current(workflowId)
    })
  }

  stopWorkflow(workflowIdValue: unknown): Promise<AgentResult<AgentWorkflowView>> {
    let workflowId = ''
    // Stop is never blocked behind another request on the same workflow: it has its own key.
    return this.guarded(`workflows:stop:${String(workflowIdValue)}`, () => {
      workflowId = id(workflowIdValue, 'workflow')
    }, async () => this.post(workflowId, 'stop', { reason: 'user_stopped' }, TIMEOUTS.write))
  }
}
