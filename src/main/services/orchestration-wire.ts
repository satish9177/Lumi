import { isAgentCapabilityId, type AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentError } from '../../shared/agent-contracts'
import {
  ORCHESTRATION_PAUSE_REASONS,
  ORCHESTRATION_STATUSES,
  ORCHESTRATION_STEP_STATUSES,
  type AgentOrchestrationResourceView,
  type AgentOrchestrationStepView,
  type AgentOrchestrationView
} from '../../shared/orchestration-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parser from runtime JSON to the closed Milestone 11 S2 orchestration DTO. A violation rejects the
 * whole response; only known, bounded fields are copied. `capability_id` and `available_capabilities`
 * entries are re-checked against the closed catalog here too -- the runtime's own CHECK constraint is not
 * trusted as the only gate.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const REF = /^r[1-9][0-9]{0,5}$/
//: Milestone 12 S1's closed resource-kind vocabulary, spelled identically to
//: `app/domain/orchestration_resources.py`'s `RESOURCE_KINDS`. A kind outside this list is refused here even
//: if it is otherwise shaped like a plausible identifier.
const RESOURCE_KINDS = [
  'public_url_ref', 'research_result_ref',
  'account_context_ref', 'account_result_ref',
  'document_ref', 'document_result_ref', 'transfer_ref',
  'desktop_target_ref', 'desktop_snapshot_ref', 'desktop_result_ref',
  'app_ref', 'project_ref', 'project_status_ref',
  'form_target_ref', 'form_result_ref', 'workflow_ref'
] as const
const PRIVACY_CLASSES = ['public', 'private', 'none'] as const
const MAX_LIST = 32
const MAX_SUMMARY = 600
const MAX_SAFE_LABEL = 200

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

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function label(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum) throw new WireError(what)
  return value
}

function optional<T>(value: unknown, parse: (value: unknown) => T): T | undefined {
  return value === null || value === undefined ? undefined : parse(value)
}

function list(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value) || value.length > MAX_LIST) throw new WireError(what)
  return value
}

function capabilityId(value: unknown, what: string): AgentCapabilityId {
  if (!isAgentCapabilityId(value)) throw new WireError(what)
  return value
}

function step(value: unknown): AgentOrchestrationStepView {
  const raw = record(value, 'orchestration.step')
  const childTaskId = optional(raw.child_task_id, (v) => uuid(v, 'orchestration.step.child_task_id'))
  const resultHandle = optional(raw.result_handle, (v) => label(v, 'orchestration.step.result_handle', 40))
  const resultSummary = optional(raw.result_summary, (v) => label(v, 'orchestration.step.result_summary', MAX_SUMMARY))
  return {
    sequence: integer(raw.sequence, 'orchestration.step.sequence', 1),
    capabilityId: capabilityId(raw.capability_id, 'orchestration.step.capability_id'),
    status: member(ORCHESTRATION_STEP_STATUSES, raw.status, 'orchestration.step.status'),
    ...(childTaskId !== undefined ? { childTaskId } : {}),
    ...(resultHandle !== undefined ? { resultHandle } : {}),
    ...(resultSummary !== undefined ? { resultSummary } : {})
  }
}

const MAX_BACKING_TEXT = 2048

function resource(value: unknown): AgentOrchestrationResourceView {
  const raw = record(value, 'orchestration.resource')
  if (typeof raw.ref !== 'string' || !REF.test(raw.ref)) throw new WireError('orchestration.resource.ref')
  if (typeof raw.single_use !== 'boolean') throw new WireError('orchestration.resource.single_use')
  const backingId = optional(raw.backing_id, (v) => uuid(v, 'orchestration.resource.backing_id'))
  const backingText = optional(raw.backing_text, (v) => label(v, 'orchestration.resource.backing_text', MAX_BACKING_TEXT))
  return {
    ref: raw.ref,
    kind: member(RESOURCE_KINDS, raw.kind, 'orchestration.resource.kind'),
    privacyClass: member(PRIVACY_CLASSES, raw.privacy_class, 'orchestration.resource.privacy_class'),
    safeLabel: label(raw.safe_label, 'orchestration.resource.safe_label', MAX_SAFE_LABEL),
    singleUse: raw.single_use,
    ...(backingId !== undefined ? { backingId } : {}),
    ...(backingText !== undefined ? { backingText } : {})
  }
}

export function parseOrchestration(value: unknown): AgentOrchestrationView {
  const raw = record(value, 'orchestration')
  if (typeof raw.live !== 'boolean') throw new WireError('orchestration.live')
  const pauseReason = optional(raw.pause_reason, (v) => member(ORCHESTRATION_PAUSE_REASONS, v, 'orchestration.pause_reason'))
  const stoppedAt = optional(raw.stopped_at, (v) => label(v, 'orchestration.stopped_at', 40))
  return {
    orchestrationId: uuid(raw.orchestration_id, 'orchestration.orchestration_id'),
    status: member(ORCHESTRATION_STATUSES, raw.status, 'orchestration.status'),
    ...(pauseReason !== undefined ? { pauseReason } : {}),
    live: raw.live,
    revision: integer(raw.revision, 'orchestration.revision', 1),
    objective: label(raw.objective, 'orchestration.objective', 500),
    stepCount: integer(raw.step_count, 'orchestration.step_count'),
    childTaskCount: integer(raw.child_task_count, 'orchestration.child_task_count'),
    plannerCalls: integer(raw.planner_calls, 'orchestration.planner_calls'),
    createdAt: label(raw.created_at, 'orchestration.created_at', 40),
    expiresAt: label(raw.expires_at, 'orchestration.expires_at', 40),
    ...(stoppedAt !== undefined ? { stoppedAt } : {}),
    availableCapabilities: list(raw.available_capabilities, 'orchestration.available_capabilities')
      .map((item) => capabilityId(item, 'orchestration.available_capabilities.item')),
    resources: list(raw.resources, 'orchestration.resources').map(resource),
    ...(optional(raw.document_task_id, (v) => uuid(v, 'orchestration.document_task_id')) !== undefined
      ? { documentTaskId: raw.document_task_id as string }
      : {}),
    steps: list(raw.steps, 'orchestration.steps').map(step)
  }
}

export function parseLatestOrchestration(value: unknown): AgentOrchestrationView | null {
  const raw = record(value, 'latest_orchestration')
  return raw.orchestration === null || raw.orchestration === undefined ? null : parseOrchestration(raw.orchestration)
}

const REASONS: Record<string, string> = {
  orchestration_not_found: 'That orchestration no longer exists.',
  orchestration_not_active: 'That orchestration is not running right now.',
  orchestration_expired: 'That orchestration expired after 30 minutes. Start a new one.',
  revision_conflict: 'That orchestration changed since it was last read.',
  capability_unknown: 'Lumi does not recognise that capability.',
  objective_invalid: 'Say what task Lumi should work on.',
  task_not_found: 'That step’s task no longer exists.',
  task_kind_mismatch: 'That task does not match the capability chosen for it.',
  child_task_already_linked: 'That task is already part of another step.',
  task_id_required: 'That capability needs a task to link.',
  task_id_not_allowed: 'That capability does not take a task reference.',
  resolved_summary_not_allowed: 'That capability does not take a result summary directly.',
  resolved_summary_required: 'That capability needs a result to record.',
  nothing_to_finish: 'Lumi has not completed a step yet, so there is nothing to finish.',
  resources_invalid: 'That resource reference was not in a form Lumi recognises.',
  resources_not_supported: 'That capability does not take a resource reference.',
  resource_not_found: 'That resource no longer exists for this task.',
  resource_consumed: 'That resource has already been used.',
  resource_expired: 'That resource is no longer fresh enough to use.',
  resource_kind_mismatch: 'That resource is not the right kind for this capability.',
  document_task_mismatch: 'That document belongs to a different task than the one already in use here.',
  result_backing_id_required: 'That capability needs to say which result it produced.',
  result_backing_id_not_allowed: 'That capability does not take a result reference.'
}

const CODE = /^[a-z][a-z0-9_]{0,63}$/

export function projectOrchestrationRuntimeError(status: number, body: unknown): AgentError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined
  const code = typeof error?.code === 'string' && CODE.test(error.code) ? error.code : undefined
  const reason = typeof error?.reason === 'string' && CODE.test(error.reason) ? error.reason : undefined
  if (code === 'orchestration_state_changed' || code === 'orchestration_refused') {
    return {
      code,
      message: (reason !== undefined ? REASONS[reason] : undefined)
        ?? (code === 'orchestration_state_changed' ? 'That orchestration can no longer go ahead as approved.' : 'Lumi refused that orchestration request.')
    }
  }
  return { code: status === 422 ? 'invalid_request' : 'request_failed', message: 'Lumi could not complete that orchestration request.' }
}
