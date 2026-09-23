import { WORKFLOW_PROVENANCES, type AgentError, type AgentTaskStatus } from '../../shared/agent-contracts'
import {
  WORKFLOW_CANDIDATE_STATUSES,
  WORKFLOW_ROLES,
  type AgentWorkflowAdoptionView,
  type AgentWorkflowCandidateView,
  type AgentWorkflowStepView,
  type AgentWorkflowValueView,
  type AgentWorkflowView
} from '../../shared/workflow-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parser from runtime JSON to the closed Milestone 10 S4 workflow DTO. A violation rejects the whole
 * response and only known, bounded fields are copied. Runtime-authored labels are refused if they look like
 * an absolute Windows path; a candidate's value and quote are the person's own document text and are only
 * bounded (a document line that happens to look like a path must not make the workflow unviewable).
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const STATUS = /^[A-Z][A-Z_]{0,31}$/
const KIND = /^[a-z][a-z_]{1,31}$/
const ABSOLUTE_PATH = /(^|[\s"'(])([A-Za-z]:[\\/]|\\\\|\/\/\?\/)/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const MAX_LIST = 64

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function integer(value: unknown, what: string, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum || value > maximum) throw new WireError(what)
  return value
}

function label(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum || ABSOLUTE_PATH.test(value)) throw new WireError(what)
  return value
}

function documentText(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum) throw new WireError(what)
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function pattern(value: unknown, what: string, shape: RegExp): string {
  if (typeof value !== 'string' || !shape.test(value)) throw new WireError(what)
  return value
}

function optional<T>(value: unknown, parse: (value: unknown) => T): T | undefined {
  return value === null || value === undefined ? undefined : parse(value)
}

function list(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value) || value.length > MAX_LIST) throw new WireError(what)
  return value
}

function step(value: unknown): AgentWorkflowStepView {
  const raw = record(value, 'workflow.step')
  return {
    role: member(WORKFLOW_ROLES, raw.role, 'workflow.step.role'),
    taskId: uuid(raw.task_id, 'workflow.step.task_id'),
    taskStatus: member(TASK_STATUSES, raw.task_status, 'workflow.step.task_status')
  }
}

function candidate(value: unknown): AgentWorkflowCandidateView {
  const raw = record(value, 'workflow.candidate')
  const text = optional(raw.value, (v) => documentText(v, 'workflow.candidate.value', 300))
  const quote = optional(raw.quote, (v) => documentText(v, 'workflow.candidate.quote', 200))
  return {
    candidateId: uuid(raw.candidate_id, 'workflow.candidate.candidate_id'),
    kind: pattern(raw.kind, 'workflow.candidate.kind', KIND),
    provenance: member(WORKFLOW_PROVENANCES, raw.provenance, 'workflow.candidate.provenance'),
    ...(text !== undefined ? { value: text } : {}),
    preview: label(raw.preview, 'workflow.candidate.preview', 120),
    status: member(WORKFLOW_CANDIDATE_STATUSES, raw.status, 'workflow.candidate.status'),
    documentLabel: label(raw.document_label, 'workflow.candidate.document_label', 255),
    ...(quote !== undefined ? { quote } : {})
  }
}

function adoptedValue(value: unknown): AgentWorkflowValueView {
  const raw = record(value, 'workflow.value')
  if (typeof raw.purged !== 'boolean') throw new WireError('workflow.value.purged')
  if ('value' in raw) throw new WireError('workflow.value.raw')
  return {
    kind: pattern(raw.kind, 'workflow.value.kind', KIND),
    provenance: member(WORKFLOW_PROVENANCES, raw.provenance, 'workflow.value.provenance'),
    preview: label(raw.preview, 'workflow.value.preview', 120),
    purged: raw.purged
  }
}

function adoption(value: unknown): AgentWorkflowAdoptionView {
  const raw = record(value, 'workflow.adoption')
  const approvalStatus = optional(raw.approval_status, (v) => pattern(v, 'workflow.adoption.approval_status', STATUS))
  const text = optional(raw.value, (v) => documentText(v, 'workflow.adoption.value', 300))
  return {
    actionId: uuid(raw.action_id, 'workflow.adoption.action_id'),
    revision: integer(raw.revision, 'workflow.adoption.revision', 1),
    actionStatus: pattern(raw.action_status, 'workflow.adoption.action_status', STATUS),
    ...(approvalStatus ? { approvalStatus } : {}),
    candidateId: uuid(raw.candidate_id, 'workflow.adoption.candidate_id'),
    kind: pattern(raw.kind, 'workflow.adoption.kind', KIND),
    provenance: member(WORKFLOW_PROVENANCES, raw.provenance, 'workflow.adoption.provenance'),
    preview: label(raw.preview, 'workflow.adoption.preview', 120),
    ...(text !== undefined ? { value: text } : {}),
    documentLabel: label(raw.document_label, 'workflow.adoption.document_label', 255)
  }
}

export function parseWorkflow(value: unknown): AgentWorkflowView {
  const raw = record(value, 'workflow')
  if (typeof raw.live !== 'boolean') throw new WireError('workflow.live')
  const stopReason = optional(raw.stop_reason, (v) => pattern(v, 'workflow.stop_reason', CODE))
  const transferStatus = optional(raw.transfer_status, (v) => pattern(v, 'workflow.transfer_status', STATUS))
  const placedName = optional(raw.placed_name, (v) => label(v, 'workflow.placed_name', 255))
  const disclosureStatus = optional(raw.disclosure_status, (v) => pattern(v, 'workflow.disclosure_status', STATUS))
  return {
    workflowId: uuid(raw.workflow_id, 'workflow.workflow_id'),
    status: member(['ACTIVE', 'STOPPED'] as const, raw.status, 'workflow.status'),
    live: raw.live,
    revision: integer(raw.revision, 'workflow.revision', 1),
    objective: label(raw.objective, 'workflow.objective', 300),
    expiresAt: pattern(raw.expires_at, 'workflow.expires_at', INSTANT),
    ...(stopReason ? { stopReason } : {}),
    steps: list(raw.steps, 'workflow.steps').map(step),
    ...(transferStatus ? { transferStatus } : {}),
    ...(placedName ? { placedName } : {}),
    documentCount: integer(raw.document_count, 'workflow.document_count', 0, 16),
    ...(disclosureStatus ? { disclosureStatus } : {}),
    candidates: list(raw.candidates, 'workflow.candidates').map(candidate),
    values: list(raw.values, 'workflow.values').map(adoptedValue),
    adoptions: list(raw.adoptions, 'workflow.adoptions').map(adoption)
  }
}

export function parseLatestWorkflow(value: unknown): AgentWorkflowView | null {
  const raw = record(value, 'latest_workflow')
  return raw.workflow === null || raw.workflow === undefined ? null : parseWorkflow(raw.workflow)
}

const REASONS: Record<string, string> = {
  workflow_not_found: 'That workflow no longer exists.',
  workflow_not_active: 'That workflow was stopped. Nothing further will be prepared.',
  workflow_expired: 'That workflow expired after a day. Start a new one.',
  step_exists: 'That step already exists in this workflow.',
  step_missing: 'Do the earlier step first.',
  not_placed: 'Save the downloaded file into the folder first.',
  placed_file_changed: 'The saved file changed after Lumi placed it, so Lumi will not read it for this workflow.',
  document_not_in_workflow: 'That document is not part of this workflow.',
  document_expired: 'That document’s text was removed after a day.',
  disclosure_not_in_workflow: 'No provider comparison was made in this workflow.',
  disclosure_not_succeeded: 'The provider comparison did not finish, so it cannot suggest details.',
  candidate_not_found: 'That detail is not part of this workflow.',
  candidate_not_proposed: 'That detail was already used or dismissed.',
  candidate_changed: 'That detail no longer matches its document, so it cannot be adopted.',
  value_already_adopted: 'A detail of that kind is already adopted in this workflow.',
  projection_changed: 'The document shown to the provider changed, so its suggestion cannot be used.',
  root_revoked: 'That folder is no longer approved.',
  permission_missing: 'That folder is not approved for reading.',
  downloads_not_configured: 'Downloads are not available in this build.',
  form_not_configured: 'Account forms are not available in this build.'
}

export function projectWorkflowRuntimeError(status: number, body: unknown): AgentError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined
  const code = typeof error?.code === 'string' && CODE.test(error.code) ? error.code : undefined
  const reason = typeof error?.reason === 'string' && CODE.test(error.reason) ? error.reason : undefined
  if (code === 'workflow_state_changed' || code === 'workflow_refused') {
    return {
      code,
      message: (reason !== undefined ? REASONS[reason] : undefined)
        ?? (code === 'workflow_state_changed' ? 'That workflow step can no longer go ahead as approved.' : 'Lumi refused that workflow request.')
    }
  }
  if (code === 'transfer_refused' || code === 'transfer_state_changed') {
    return { code, message: 'Lumi refused that download request. Nothing was downloaded or saved.' }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That workflow step no longer exists.' }
  return { code: status === 422 ? 'invalid_request' : 'request_failed', message: 'Lumi could not complete that workflow request.' }
}
