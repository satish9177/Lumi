import type { FormPlanningContext } from '../agent/form-planner'
import {
  ACTION_STATUSES,
  APPROVAL_STATUSES,
  ATTEMPT_OUTCOMES,
  AUTHENTICATED_OPERATIONS,
  AUTHENTICATED_PAUSE_REASONS,
  BROWSER_PROFILE_STATUSES,
  BOOKING_DAYS,
  CHANGED_FACT_FIELDS,
  CLINIC_INFO_TOPICS,
  DISCLOSURE_RECIPIENTS,
  DISPATCH_STATUSES,
  GRANT_STATUSES,
  PROTECTED_DATA_KINDS,
  LOOKUP_STATUSES,
  PAGE_ANSWER_STATUSES,
  RESEARCH_ANSWER_STATUSES,
  RESEARCH_OPERATIONS,
  RESEARCH_STOP_REASONS,
  RISK_TIERS,
  TASK_EVENT_TYPES,
  TASK_STATUSES,
  type AgentActionView,
  type AgentApprovalView,
  type AgentAttemptOutcome,
  type AgentAttemptResultView,
  type AgentAttemptView,
  type AgentAuthenticatedAnswerView,
  type AgentAuthenticatedBudgets,
  type AgentAuthenticatedGrantView,
  type AgentAuthenticatedObservationView,
  type AgentAuthenticatedPauseReason,
  type AgentAuthenticatedProfileView,
  type AgentAuthenticatedRequestView,
  type AgentAuthenticatedScopeView,
  type AgentAuthenticatedView,
  type AgentBookingCriteria,
  type AgentBookingView,
  type AgentChangedFact,
  type AgentClinicInfoQuery,
  type AgentDoctorProfileView,
  type AgentDisclosureCardView,
  type AgentDisclosureFieldView,
  type AgentError,
  type AgentEventView,
  type AgentFormGrantView,
  type AgentFormPlanView,
  type AgentProtectedDataKind,
  type AgentSavedDetailView,
  type AgentFoundBookingView,
  type AgentInspectionAttemptView,
  type AgentInspectionProposalView,
  type AgentInspectionRequestView,
  type AgentInspectionView,
  type AgentObservationMetaView,
  type AgentPageAnswerView,
  type AgentReceiptView,
  type AgentReconciliationView,
  type AgentResearchAnswerView,
  type AgentResearchBudgets,
  type AgentResearchGrantView,
  type AgentResearchObservationView,
  type AgentResearchRequestView,
  type AgentResearchScopeView,
  type AgentResearchSessionView,
  type AgentResearchView,
  type AgentSlotView,
  type AgentTaskView
} from '../../shared/agent-contracts'
import { describeRefusal } from '../agent/public-url-policy'
import type { PageObservationDetail } from '../agent/page-answer'

/**
 * Strict parsers from runtime JSON to closed desktop DTOs.
 *
 * Structural fields the UI depends on are required and validated; a violation
 * rejects the whole response (`WireError`) rather than guessing. Stored JSON
 * that the runtime treats as opaque (attempt results, event payloads,
 * reconciliation evidence) is projected field by field: only known, bounded,
 * well-formed values are copied and everything else is dropped.
 */

export class WireError extends Error {
  constructor(what: string) {
    super(`Unexpected agent runtime response (${what}).`)
    this.name = 'WireError'
  }
}

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const DIGEST = /^[0-9a-f]{64}$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const SLOT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
const SITE = /^[a-z][a-z0-9_]{0,63}$/
const CURRENCY = /^[A-Z]{3}$/
const BOOKING_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/
// Offset-bearing ISO-8601 instants as emitted by Pydantic.
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const SPECIALTY = /^[A-Za-z][A-Za-z .'-]{0,59}$/
const CLOCK = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const TURN_ID = /^[A-Za-z0-9_-]{1,64}$/
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const PERSON_NAME = /^[A-Za-z][A-Za-z .'-]{0,59}$/
const DOCTOR_ID = /^[a-z0-9][a-z0-9-]{0,39}$/
const LANGUAGE = /^[A-Za-z][A-Za-z -]{0,29}$/
const MAX_TEXT = 120
const MAX_SLOTS = 50

export function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function integer(value: unknown, what: string, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new WireError(what)
  }
  return value
}

function instant(value: unknown, what: string): string {
  if (typeof value !== 'string' || !INSTANT.test(value) || Number.isNaN(Date.parse(value))) {
    throw new WireError(what)
  }
  return value
}

function text(value: unknown, what: string, pattern?: RegExp, maximum = MAX_TEXT): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum || (pattern && !pattern.test(value))) {
    throw new WireError(what)
  }
  // Control characters have no business in a displayed booking field.
  if (/[\x00-\x1f\x7f]/.test(value)) throw new WireError(what)
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

/** Optional-field projection: invalid means absent, never a throw. */
function optional<T>(read: () => T): T | undefined {
  try {
    return read()
  } catch (error) {
    if (error instanceof WireError) return undefined
    throw error
  }
}

function nullableInstant(value: unknown, what: string): string | undefined {
  return value === null || value === undefined ? undefined : instant(value, what)
}

// ---- tasks -----------------------------------------------------------------

/**
 * Constraints from a task request or an event payload (same snake_case keys).
 * Specialty and day keep M4's lenient reading; a bound is shown only when it
 * is well formed, and a price ceiling only together with its currency.
 */
export function parseCriteria(request: Json): AgentBookingCriteria {
  const specialty = typeof request.specialty === 'string' && SPECIALTY.test(request.specialty) ? request.specialty : ''
  const day = typeof request.day === 'string' && (BOOKING_DAYS as readonly string[]).includes(request.day)
    ? request.day as AgentBookingCriteria['day']
    : ''
  const criteria: AgentBookingCriteria = { specialty, day }
  if (typeof request.earliest_time === 'string' && CLOCK.test(request.earliest_time)) criteria.earliestTime = request.earliest_time
  if (typeof request.latest_time === 'string' && CLOCK.test(request.latest_time)) criteria.latestTime = request.latest_time
  const maxPrice = optional(() => integer(request.max_price, 'criteria.max_price', 0, 10_000_000))
  const currency = optional(() => text(request.max_price_currency, 'criteria.currency', CURRENCY, 3))
  if (maxPrice !== undefined && currency !== undefined) {
    criteria.maxPrice = maxPrice
    criteria.maxPriceCurrency = currency
  }
  // Both ends or neither, as the runtime stores them.
  if (typeof request.date_from === 'string' && ISO_DATE.test(request.date_from) &&
      typeof request.date_to === 'string' && ISO_DATE.test(request.date_to) &&
      request.date_from <= request.date_to) {
    criteria.dateFrom = request.date_from
    criteria.dateTo = request.date_to
  }
  return criteria
}

export function parseInfoQuery(value: Json): AgentClinicInfoQuery | undefined {
  const specialty = typeof value.specialty === 'string' && (value.specialty === '' || SPECIALTY.test(value.specialty)) ? value.specialty : undefined
  const doctor = typeof value.doctor === 'string' && (value.doctor === '' || PERSON_NAME.test(value.doctor)) ? value.doctor : undefined
  const topic = value.topic === undefined ? 'overview' : optional(() => member(CLINIC_INFO_TOPICS, value.topic, 'info.topic'))
  if (specialty === undefined && doctor === undefined) return undefined
  if (topic === undefined) return undefined
  return { specialty: specialty ?? '', doctor: doctor ?? '', topic }
}

/**
 * Doctor profiles are page data. Every field is bounded and shaped; a list
 * with one unreadable profile is refused whole rather than shown partly.
 */
export function parseProfiles(value: unknown, what: string): AgentDoctorProfileView[] {
  if (!Array.isArray(value) || value.length > 10) throw new WireError(what)
  return value.map((raw, index) => {
    const profile = record(raw, `${what}[${index}]`)
    if (!Array.isArray(profile.languages) || profile.languages.length > 12) throw new WireError(`${what}.languages`)
    if (typeof profile.walk_ins !== 'boolean') throw new WireError(`${what}.walk_ins`)
    return {
      doctorId: text(profile.doctor_id, 'profile.doctor_id', DOCTOR_ID, 40),
      doctor: text(profile.doctor, 'profile.doctor'),
      specialty: text(profile.specialty, 'profile.specialty'),
      clinic: text(profile.clinic, 'profile.clinic'),
      address: text(profile.address, 'profile.address', undefined, 200),
      hours: text(profile.hours, 'profile.hours', undefined, 80),
      consultationFee: integer(profile.consultation_fee, 'profile.fee', 0, 10_000_000),
      currency: text(profile.currency, 'profile.currency', CURRENCY, 3),
      languages: profile.languages.map((language) => text(language, 'profile.language', LANGUAGE, 30)),
      walkIns: profile.walk_ins
    }
  })
}

export interface ClinicInfoLookup {
  task: AgentTaskView
  profiles: AgentDoctorProfileView[]
}

export function parseClinicInfo(value: unknown, taskId: string): ClinicInfoLookup {
  const body = record(value, 'clinic_info')
  const task = parseTask(body.task)
  if (task.taskId !== taskId || task.kind !== 'clinic_info') throw new WireError('clinic_info.task')
  return { task, profiles: parseProfiles(body.profiles, 'clinic_info.profiles') }
}

const WEB_URL = /^https?:\/\/[^\s]{1,2040}$/
const BLOCK_ID = /^b[1-9][0-9]{0,2}$/
const LINK_ID = /^l[1-9][0-9]?$/
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const POLICY_VERSION_PATTERN = /^[a-z0-9][a-z0-9.-]{0,39}$/
const MAX_QUESTION = 500

function webUrl(value: unknown, what: string): string {
  return text(value, what, WEB_URL, 2_048)
}

function hostOf(url: string, what: string): string {
  try {
    const host = new URL(url).hostname
    if (!host) throw new WireError(what)
    return host
  } catch {
    throw new WireError(what)
  }
}

/** The URL and question a page-inspection task was created with. */
export function parseInspectionRequest(request: Json): AgentInspectionRequestView {
  const url = webUrl(request.url, 'task.url')
  return { url, host: hostOf(url, 'task.url'), question: text(request.question, 'task.question', undefined, MAX_QUESTION) }
}

export function parseTask(value: unknown): AgentTaskView {
  const task = record(value, 'task')
  const request = record(task.request, 'task.request')
  if (
    request.type !== 'appointment_booking' && request.type !== 'clinic_info' &&
    request.type !== 'page_inspection' && request.type !== 'public_research' &&
    request.type !== 'authenticated_read'
  ) {
    throw new WireError('task.type')
  }
  const kind = request.type
  const infoQuery = kind === 'clinic_info' ? parseInfoQuery(request) : undefined
  if (kind === 'clinic_info' && !infoQuery) throw new WireError('task.info_query')
  const inspection = kind === 'page_inspection' ? parseInspectionRequest(request) : undefined
  const research = kind === 'public_research' ? parseResearchRequest(request) : undefined
  const authenticated = kind === 'authenticated_read' ? parseAuthenticatedRequest(request) : undefined
  return {
    taskId: uuid(task.id, 'task.id'),
    status: member(TASK_STATUSES, task.status, 'task.status'),
    revision: integer(task.revision, 'task.revision', 1),
    lastEventSequence: integer(task.last_event_sequence, 'task.last_event_sequence', 1),
    kind,
    criteria: kind === 'appointment_booking' ? parseCriteria(request) : { specialty: '', day: '' },
    ...(infoQuery ? { infoQuery } : {}),
    ...(inspection ? { inspection } : {}),
    ...(research ? { research } : {}),
    ...(authenticated ? { authenticated } : {}),
    ...(typeof request.voice_turn_id === 'string' && TURN_ID.test(request.voice_turn_id)
      ? { voiceTurnId: request.voice_turn_id }
      : {}),
    ...(typeof request.request_id === 'string' && TURN_ID.test(request.request_id)
      ? { requestId: request.request_id }
      : {}),
    createdAt: instant(task.created_at, 'task.created_at'),
    updatedAt: instant(task.updated_at, 'task.updated_at')
  }
}

export function parseHealth(value: unknown): string {
  const health = record(value, 'health')
  if (health.status !== 'ok' || health.database !== 'ok') throw new WireError('health')
  return uuid(health.runtime_generation, 'health.runtime_generation')
}

// ---- search ----------------------------------------------------------------

function parseSlots(value: unknown, what: string): AgentSlotView[] {
  if (!Array.isArray(value) || value.length > MAX_SLOTS) throw new WireError(what)
  const seen = new Set<string>()
  return value.map((raw, index) => {
    const slot = record(raw, `${what}[${index}]`)
    const slotId = text(slot.slot_id, 'slot.slot_id', SLOT_ID, 64)
    if (seen.has(slotId)) throw new WireError(`${what}.duplicate`)
    seen.add(slotId)
    return {
      slotId,
      doctor: text(slot.doctor, 'slot.doctor'),
      specialty: typeof slot.specialty === 'string' && slot.specialty.length <= MAX_TEXT
        ? optional(() => text(slot.specialty, 'slot.specialty')) ?? ''
        : '',
      time: instant(slot.time, 'slot.time'),
      price: integer(slot.price, 'slot.price', 0, 10_000_000),
      currency: text(slot.currency, 'slot.currency', CURRENCY, 3)
    }
  })
}

export function parseSearch(value: unknown, taskId: string): AgentSlotView[] {
  const body = record(value, 'search')
  if (uuid(body.task_id, 'search.task_id') !== taskId) throw new WireError('search.task_id')
  return parseSlots(body.slots, 'search.slots')
}

function parseIdList(value: unknown, what: string): string[] {
  if (!Array.isArray(value) || value.length > 100) throw new WireError(what)
  return value.map((item) => uuid(item, what))
}

export interface CriteriaRevision {
  task: AgentTaskView
  invalidatedActionIds: string[]
}

export function parseCriteriaRevision(value: unknown, taskId: string): CriteriaRevision {
  const body = record(value, 'criteria_revision')
  const task = parseTask(body.task)
  if (task.taskId !== taskId) throw new WireError('criteria_revision.task')
  return { task, invalidatedActionIds: parseIdList(body.invalidated_action_ids, 'criteria_revision.invalidated') }
}

export interface TaskCancellation {
  task: AgentTaskView
  rejectedActionIds: string[]
}

export function parseCancellation(value: unknown, taskId: string): TaskCancellation {
  const body = record(value, 'cancellation')
  const task = parseTask(body.task)
  if (task.taskId !== taskId || task.status !== 'CANCELLED') throw new WireError('cancellation.task')
  return { task, rejectedActionIds: parseIdList(body.rejected_action_ids, 'cancellation.rejected') }
}

// ---- actions ---------------------------------------------------------------

export function parseBooking(value: unknown): AgentBookingView {
  const proposal = record(value, 'action.proposal')
  const allowed = new Set(['site', 'slot_id', 'doctor', 'time', 'price', 'currency'])
  // The proposal is what the user approves. Anything unexpected in it means it
  // was not written by the reviewed preparation path; refuse to render it.
  if (Object.keys(proposal).some((key) => !allowed.has(key))) throw new WireError('action.proposal.fields')
  return {
    site: text(proposal.site, 'proposal.site', SITE, 64),
    slotId: text(proposal.slot_id, 'proposal.slot_id', SLOT_ID, 64),
    doctor: text(proposal.doctor, 'proposal.doctor'),
    time: instant(proposal.time, 'proposal.time'),
    price: integer(proposal.price, 'proposal.price', 0, 10_000_000),
    currency: text(proposal.currency, 'proposal.currency', CURRENCY, 3)
  }
}

function parseApproval(value: unknown, actionId: string): AgentApprovalView | undefined {
  if (value === null || value === undefined) return undefined
  const approval = record(value, 'approval')
  if (uuid(approval.action_id, 'approval.action_id') !== actionId) throw new WireError('approval.action_id')
  const approvedAt = nullableInstant(approval.approved_at, 'approval.approved_at')
  return {
    approvalId: uuid(approval.id, 'approval.id'),
    status: member(APPROVAL_STATUSES, approval.status, 'approval.status'),
    actionRevision: integer(approval.action_revision, 'approval.action_revision', 1),
    proposalDigest: text(approval.proposal_digest, 'approval.proposal_digest', DIGEST, 64),
    createdAt: instant(approval.created_at, 'approval.created_at'),
    expiresAt: instant(approval.expires_at, 'approval.expires_at'),
    ...(approvedAt ? { approvedAt } : {})
  }
}

function parseChangedFacts(value: unknown): AgentChangedFact[] | undefined {
  if (!Array.isArray(value) || value.length > CHANGED_FACT_FIELDS.length) return undefined
  const facts: AgentChangedFact[] = []
  for (const raw of value) {
    const fact = optional(() => {
      const item = record(raw, 'changed_fact')
      return {
        field: member(CHANGED_FACT_FIELDS, item.field, 'changed_fact.field'),
        approved: text(item.approved, 'changed_fact.approved', undefined, 200),
        observed: text(item.observed, 'changed_fact.observed', undefined, 200)
      }
    })
    // A partially readable change list would understate what changed.
    if (!fact) return undefined
    facts.push(fact)
  }
  return facts
}

function parseReceipt(value: unknown): AgentReceiptView | undefined {
  return optional(() => {
    const receipt = record(value, 'receipt')
    return {
      bookingId: text(receipt.booking_id, 'receipt.booking_id', BOOKING_ID, 40),
      doctor: text(receipt.doctor, 'receipt.doctor'),
      price: integer(receipt.price, 'receipt.price', 0, 10_000_000),
      currency: text(receipt.currency, 'receipt.currency', CURRENCY, 3)
    }
  })
}

export function projectAttemptResult(value: unknown): AgentAttemptResultView | undefined {
  if (!isRecord(value)) return undefined
  const result: AgentAttemptResultView = {}
  const dispatchStatus = optional(() => member(DISPATCH_STATUSES, value.status, 'result.status'))
  if (dispatchStatus) result.dispatchStatus = dispatchStatus
  if (typeof value.submitted === 'boolean') result.submitted = value.submitted
  const bookingId = optional(() => text(value.booking_id, 'result.booking_id', BOOKING_ID, 40))
  if (bookingId) result.bookingId = bookingId
  if (value.receipt !== undefined && value.receipt !== null) {
    const receipt = parseReceipt(value.receipt)
    if (receipt) result.receipt = receipt
  }
  if (Array.isArray(value.changed_facts) && value.changed_facts.length > 0) {
    const facts = parseChangedFacts(value.changed_facts)
    if (facts) result.changedFacts = facts
  }
  return Object.keys(result).length > 0 ? result : undefined
}

function parseAttempt(value: unknown, actionId: string): AgentAttemptView {
  const attempt = record(value, 'attempt')
  if (uuid(attempt.action_id, 'attempt.action_id') !== actionId) throw new WireError('attempt.action_id')
  const finishedAt = nullableInstant(attempt.finished_at, 'attempt.finished_at')
  const outcome = attempt.outcome === null || attempt.outcome === undefined
    ? undefined
    : member(ATTEMPT_OUTCOMES, attempt.outcome, 'attempt.outcome')
  if ((finishedAt === undefined) !== (outcome === undefined)) throw new WireError('attempt.finished')
  const errorCode = optional(() => text(attempt.error_code, 'attempt.error_code', CODE, 64))
  const result = projectAttemptResult(attempt.result)
  return {
    attemptId: uuid(attempt.id, 'attempt.id'),
    attemptNumber: integer(attempt.attempt_number, 'attempt.attempt_number', 1),
    runtimeGeneration: uuid(attempt.runtime_generation, 'attempt.runtime_generation'),
    startedAt: instant(attempt.started_at, 'attempt.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    ...(outcome ? { outcome } : {}),
    ...(errorCode ? { errorCode } : {}),
    ...(result ? { result } : {})
  }
}

export function parseAction(value: unknown): AgentActionView {
  const action = record(value, 'action')
  const actionId = uuid(action.id, 'action.id')
  if (action.tool_name !== 'commit_booking') throw new WireError('action.tool_name')
  if (!Array.isArray(action.attempts) || action.attempts.length > 100) throw new WireError('action.attempts')
  const approval = parseApproval(action.approval, actionId)
  const proposalDigest = text(action.proposal_digest, 'action.proposal_digest', DIGEST, 64)
  if (approval && approval.proposalDigest !== proposalDigest) throw new WireError('approval.proposal_digest')
  return {
    actionId,
    taskId: uuid(action.task_id, 'action.task_id'),
    toolName: 'commit_booking',
    status: member(ACTION_STATUSES, action.status, 'action.status'),
    revision: integer(action.revision, 'action.revision', 1),
    riskTier: member(RISK_TIERS, action.risk_tier, 'action.risk_tier'),
    proposalDigest,
    createdAt: instant(action.created_at, 'action.created_at'),
    updatedAt: instant(action.updated_at, 'action.updated_at'),
    booking: parseBooking(action.proposal),
    ...(approval ? { approval } : {}),
    attempts: action.attempts.map((attempt) => parseAttempt(attempt, actionId))
  }
}

export function parseActionList(value: unknown, taskId: string): AgentActionView[] {
  const body = record(value, 'actions')
  if (uuid(body.task_id, 'actions.task_id') !== taskId) throw new WireError('actions.task_id')
  if (!Array.isArray(body.actions)) throw new WireError('actions.actions')
  return body.actions.flatMap((raw) => {
    // Only booking actions belong to this surface; others are not rendered.
    if (isRecord(raw) && raw.tool_name !== 'commit_booking') return []
    const action = parseAction(raw)
    if (action.taskId !== taskId) throw new WireError('action.task_id')
    return [action]
  })
}

// ---- events ----------------------------------------------------------------

function parseFoundBooking(value: unknown): AgentFoundBookingView | undefined {
  return optional(() => {
    const booking = record(value, 'booking')
    return {
      bookingId: text(booking.booking_id, 'booking.booking_id', BOOKING_ID, 40),
      slotId: text(booking.slot_id, 'booking.slot_id', SLOT_ID, 64),
      doctor: text(booking.doctor, 'booking.doctor'),
      time: instant(booking.time, 'booking.time'),
      price: integer(booking.price, 'booking.price', 0, 10_000_000),
      currency: text(booking.currency, 'booking.currency', CURRENCY, 3)
    }
  })
}

export function projectReconciliation(payload: Json): AgentReconciliationView | undefined {
  const result = optional(() => member(ATTEMPT_OUTCOMES, payload.result, 'reconciled.result'))
  if (!result) return undefined
  const evidence = isRecord(payload.evidence) ? payload.evidence : {}
  // A verdict without a readable lookup status is shown as unknown evidence.
  const lookup = optional(() => member(LOOKUP_STATUSES, evidence.lookup, 'evidence.lookup')) ?? 'UNKNOWN'
  const view: AgentReconciliationView = { result, lookup }
  const bookingId = optional(() => text(evidence.booking_id, 'evidence.booking_id', BOOKING_ID, 40))
  if (bookingId) view.bookingId = bookingId
  const bookingCount = optional(() => integer(evidence.booking_count, 'evidence.booking_count', 0, 1_000))
  if (bookingCount !== undefined) view.bookingCount = bookingCount
  const booking = evidence.booking === undefined || evidence.booking === null ? undefined : parseFoundBooking(evidence.booking)
  if (booking) view.booking = booking
  if (typeof evidence.absence_is_authoritative === 'boolean') view.absenceIsAuthoritative = evidence.absence_is_authoritative
  return view
}

export function parseEvent(value: unknown, taskId: string): AgentEventView {
  const event = record(value, 'event')
  if (uuid(event.task_id, 'event.task_id') !== taskId) throw new WireError('event.task_id')
  const type = member(TASK_EVENT_TYPES, event.event_type, 'event.event_type')
  const payload = record(event.payload, 'event.payload')
  const view: AgentEventView = {
    sequence: integer(event.sequence, 'event.sequence', 1),
    type,
    taskRevision: integer(event.task_revision, 'event.task_revision', 1),
    createdAt: instant(event.created_at, 'event.created_at')
  }
  const actionId = optional(() => uuid(payload.action_id, 'payload.action_id'))
  if (actionId) view.actionId = actionId
  const toolName = optional(() => text(payload.tool_name, 'payload.tool_name', CODE, 64))
  if (toolName) view.toolName = toolName
  const actionStatus = optional(() => member(ACTION_STATUSES, payload.action_status, 'payload.action_status'))
  if (actionStatus) view.actionStatus = actionStatus
  const actionRevision = optional(() => integer(payload.action_revision, 'payload.action_revision', 1))
  if (actionRevision !== undefined) view.actionRevision = actionRevision
  const attemptId = optional(() => uuid(payload.attempt_id, 'payload.attempt_id'))
  if (attemptId) view.attemptId = attemptId
  const attemptNumber = optional(() => integer(payload.attempt_number, 'payload.attempt_number', 1))
  if (attemptNumber !== undefined) view.attemptNumber = attemptNumber
  const approvalId = optional(() => uuid(payload.approval_id, 'payload.approval_id'))
  if (approvalId) view.approvalId = approvalId
  const outcome = optional(() => member(ATTEMPT_OUTCOMES, payload.outcome, 'payload.outcome'))
  if (outcome) view.outcome = outcome
  const errorCode = optional(() => text(payload.error_code, 'payload.error_code', CODE, 64))
  if (errorCode) view.errorCode = errorCode
  const reason = optional(() => text(payload.reason, 'payload.reason', CODE, 64))
  if (reason) view.reason = reason
  if (type === 'action.reconciled') {
    const reconciliation = projectReconciliation(payload)
    if (reconciliation) view.reconciliation = reconciliation
  }
  if ((type === 'task.criteria_updated' || type === 'task.search_completed') && isRecord(payload.criteria)) {
    view.criteria = parseCriteria(payload.criteria)
  }
  if (type === 'task.search_completed') {
    // All or nothing: a partly readable result list would misstate the choices.
    const results = optional(() => parseSlots(payload.slots, 'payload.slots'))
    if (results) view.searchResults = results
  }
  if (type === 'task.info_lookup_completed') {
    const query = isRecord(payload.query) ? parseInfoQuery(payload.query) : undefined
    if (query) view.infoQuery = query
    const profiles = optional(() => parseProfiles(payload.profiles, 'payload.profiles'))
    if (profiles) view.profiles = profiles
  }
  if (type === 'task.page_answer_recorded') {
    const answerStatus = optional(() => member(PAGE_ANSWER_STATUSES, payload.answer_status, 'payload.answer_status'))
    if (answerStatus) view.answerStatus = answerStatus
  }
  if (type === 'task.criteria_updated') {
    const invalidated = optional(() => parseIdList(payload.invalidated_action_ids, 'payload.invalidated'))
    if (invalidated) view.invalidatedActionIds = invalidated
  }
  return view
}

export function parseEventPage(value: unknown, taskId: string, afterSequence: number): AgentEventView[] {
  const body = record(value, 'events')
  if (uuid(body.task_id, 'events.task_id') !== taskId) throw new WireError('events.task_id')
  if (!Array.isArray(body.events)) throw new WireError('events.events')
  let previous = afterSequence
  return body.events.map((raw) => {
    const event = parseEvent(raw, taskId)
    // The runtime returns sequence > cursor in order; anything else is a
    // replay bug that would duplicate or reorder the timeline.
    if (event.sequence <= previous) throw new WireError('events.order')
    previous = event.sequence
    return event
  })
}

// ---- page inspection ---------------------------------------------------------

/**
 * The stored proposal is what the user approves. Every field is required and
 * nothing else may be present, exactly as for a booking proposal.
 */
export function parseInspectionProposal(value: unknown): AgentInspectionProposalView {
  const proposal = record(value, 'inspection.proposal')
  const allowed = new Set(['schema_version', 'operation', 'effect', 'url', 'host', 'question', 'policy_version', 'limits', 'disclosure'])
  if (Object.keys(proposal).some((key) => !allowed.has(key))) throw new WireError('inspection.proposal.fields')
  if (proposal.schema_version !== 1 || proposal.operation !== 'inspect_public_page' || proposal.effect !== 'public_read') {
    throw new WireError('inspection.proposal.kind')
  }
  const url = webUrl(proposal.url, 'inspection.url')
  const host = text(proposal.host, 'inspection.host', /^[a-z0-9.-]{1,253}$/, 253)
  if (hostOf(url, 'inspection.url') !== host) throw new WireError('inspection.host')
  const limits = record(proposal.limits, 'inspection.limits')
  const disclosure = record(proposal.disclosure, 'inspection.disclosure')
  if (!Array.isArray(disclosure.recipients) || disclosure.recipients.length < 1 || disclosure.recipients.length > 3) {
    throw new WireError('inspection.recipients')
  }
  return {
    url,
    host,
    question: text(proposal.question, 'inspection.question', undefined, MAX_QUESTION),
    policyVersion: text(proposal.policy_version, 'inspection.policy_version', POLICY_VERSION_PATTERN, 40),
    recipients: disclosure.recipients.map((recipient) => member(DISCLOSURE_RECIPIENTS, recipient, 'inspection.recipient')),
    maxTextChars: integer(disclosure.max_text_chars, 'inspection.max_text_chars', 1, 12_000),
    maxLinks: integer(limits.max_links, 'inspection.max_links', 0, 20),
    maxRedirects: integer(limits.max_redirects, 'inspection.max_redirects', 0, 5)
  }
}

function parseInspectionAttempt(value: unknown, actionId: string): AgentInspectionAttemptView {
  const attempt = record(value, 'attempt')
  if (uuid(attempt.action_id, 'attempt.action_id') !== actionId) throw new WireError('attempt.action_id')
  const finishedAt = nullableInstant(attempt.finished_at, 'attempt.finished_at')
  const outcome = attempt.outcome === null || attempt.outcome === undefined
    ? undefined
    : member(ATTEMPT_OUTCOMES, attempt.outcome, 'attempt.outcome')
  if ((finishedAt === undefined) !== (outcome === undefined)) throw new WireError('attempt.finished')
  const errorCode = optional(() => text(attempt.error_code, 'attempt.error_code', CODE, 64))
  const result = isRecord(attempt.result) ? attempt.result : {}
  const refusal = optional(() => text(result.redirect_refusal ?? result.refusal, 'attempt.refusal', CODE, 64))
  const httpStatus = optional(() => integer(result.http_status, 'attempt.http_status', 100, 599))
  return {
    attemptId: uuid(attempt.id, 'attempt.id'),
    attemptNumber: integer(attempt.attempt_number, 'attempt.attempt_number', 1),
    runtimeGeneration: uuid(attempt.runtime_generation, 'attempt.runtime_generation'),
    startedAt: instant(attempt.started_at, 'attempt.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    ...(outcome ? { outcome } : {}),
    ...(errorCode ? { errorCode } : {}),
    ...(refusal ? { refusal } : {}),
    ...(httpStatus !== undefined ? { httpStatus } : {})
  }
}

export function parseInspectionAction(value: unknown): AgentInspectionView {
  const action = record(value, 'inspection.action')
  const actionId = uuid(action.id, 'action.id')
  if (action.tool_name !== 'inspect_public_page') throw new WireError('action.tool_name')
  if (!Array.isArray(action.attempts) || action.attempts.length > 100) throw new WireError('action.attempts')
  const approval = parseApproval(action.approval, actionId)
  const proposalDigest = text(action.proposal_digest, 'action.proposal_digest', DIGEST, 64)
  if (approval && approval.proposalDigest !== proposalDigest) throw new WireError('approval.proposal_digest')
  return {
    actionId,
    taskId: uuid(action.task_id, 'action.task_id'),
    toolName: 'inspect_public_page',
    status: member(ACTION_STATUSES, action.status, 'action.status'),
    revision: integer(action.revision, 'action.revision', 1),
    riskTier: member(RISK_TIERS, action.risk_tier, 'action.risk_tier'),
    proposalDigest,
    createdAt: instant(action.created_at, 'action.created_at'),
    updatedAt: instant(action.updated_at, 'action.updated_at'),
    proposal: parseInspectionProposal(action.proposal),
    ...(approval ? { approval } : {}),
    attempts: action.attempts.map((attempt) => parseInspectionAttempt(attempt, actionId))
  }
}

function pageText(value: unknown, what: string, maximum: number, allowEmpty = false): string {
  if (allowEmpty && value === '') return ''
  return text(value, what, undefined, maximum)
}

export interface InspectionDetail {
  view: AgentInspectionView
  /** Full bounded page text, for main's answer step only. Never sent to the renderer. */
  observation?: PageObservationDetail
}

/** `GET /actions/{id}/inspection` and the answer route's response. */
export function parseInspection(value: unknown): InspectionDetail {
  const body = record(value, 'inspection')
  const view = parseInspectionAction(body.action)
  if (body.observation === null || body.observation === undefined) {
    if (body.answer !== null && body.answer !== undefined) throw new WireError('inspection.answer_without_observation')
    return { view }
  }
  const raw = record(body.observation, 'observation')
  if (uuid(raw.action_id, 'observation.action_id') !== view.actionId) throw new WireError('observation.action_id')
  if (uuid(raw.task_id, 'observation.task_id') !== view.taskId) throw new WireError('observation.task_id')
  if (raw.provenance !== 'untrusted_environment' || raw.schema_version !== 1) throw new WireError('observation.provenance')
  if (!Array.isArray(raw.blocks) || raw.blocks.length > 200) throw new WireError('observation.blocks')
  if (!Array.isArray(raw.links) || raw.links.length > 20) throw new WireError('observation.links')
  if (!Array.isArray(raw.redirects) || raw.redirects.length > 5) throw new WireError('observation.redirects')
  if (typeof raw.settled !== 'boolean' || typeof raw.truncated !== 'boolean') throw new WireError('observation.flags')
  const blocks = raw.blocks.map((item, index) => {
    const block = record(item, 'observation.block')
    const id = text(block.id, 'block.id', BLOCK_ID, 4)
    if (id !== `b${index + 1}`) throw new WireError('block.order')
    return { id, text: pageText(block.text, 'block.text', 500) }
  })
  const links = raw.links.map((item, index) => {
    const link = record(item, 'observation.link')
    const id = text(link.id, 'link.id', LINK_ID, 3)
    if (id !== `l${index + 1}`) throw new WireError('link.order')
    return { id, text: pageText(link.text, 'link.text', 120, true), url: webUrl(link.url, 'link.url') }
  })
  const observation: PageObservationDetail = {
    observationId: uuid(raw.id, 'observation.id'),
    contentHash: text(raw.content_hash, 'observation.content_hash', DIGEST, 64),
    requestedUrl: webUrl(raw.requested_url, 'observation.requested_url'),
    finalUrl: webUrl(raw.final_url, 'observation.final_url'),
    title: pageText(raw.title, 'observation.title', 200, true),
    observedAt: instant(raw.observed_at, 'observation.observed_at'),
    documentEpoch: integer(raw.document_epoch, 'observation.document_epoch', 1, 1_000),
    settled: raw.settled,
    truncated: raw.truncated,
    blocks,
    links
  }
  if (observation.requestedUrl !== view.proposal.url) throw new WireError('observation.requested_url')
  const meta: AgentObservationMetaView = {
    observationId: observation.observationId,
    requestedUrl: observation.requestedUrl,
    finalUrl: observation.finalUrl,
    redirects: raw.redirects.map((redirect) => webUrl(redirect, 'observation.redirect')),
    title: observation.title,
    documentEpoch: observation.documentEpoch,
    settled: observation.settled,
    truncated: observation.truncated,
    observedAt: observation.observedAt,
    contentHash: observation.contentHash,
    blockCount: blocks.length,
    linkCount: links.length,
    workerGeneration: uuid(raw.worker_generation, 'observation.worker_generation')
  }
  let answer: AgentPageAnswerView | undefined
  if (body.answer !== null && body.answer !== undefined) {
    const rawAnswer = record(body.answer, 'answer')
    if (uuid(rawAnswer.observation_id, 'answer.observation_id') !== observation.observationId) throw new WireError('answer.observation_id')
    if (!Array.isArray(rawAnswer.evidence) || rawAnswer.evidence.length > 3) throw new WireError('answer.evidence')
    answer = {
      observationId: observation.observationId,
      status: member(PAGE_ANSWER_STATUSES, rawAnswer.status, 'answer.status'),
      answer: text(rawAnswer.answer, 'answer.answer', undefined, 600),
      evidence: rawAnswer.evidence.map((item) => {
        const entry = record(item, 'answer.evidence')
        return { block: text(entry.block, 'evidence.block', BLOCK_ID, 4), quote: text(entry.quote, 'evidence.quote', undefined, 300) }
      }),
      provider: member(DISCLOSURE_RECIPIENTS, rawAnswer.provider, 'answer.provider'),
      model: text(rawAnswer.model, 'answer.model', MODEL_NAME, 64),
      answeredAt: instant(rawAnswer.answered_at, 'answer.answered_at')
    }
  }
  return { view: { ...view, observation: meta, ...(answer ? { answer } : {}) }, observation }
}

/** The newest inspection action id in a runtime action list, if any. */
export function latestInspectionActionId(value: unknown, taskId: string): string | undefined {
  const body = record(value, 'actions')
  if (uuid(body.task_id, 'actions.task_id') !== taskId) throw new WireError('actions.task_id')
  if (!Array.isArray(body.actions)) throw new WireError('actions.actions')
  let latest: string | undefined
  for (const raw of body.actions) {
    if (isRecord(raw) && raw.tool_name === 'inspect_public_page') latest = uuid(raw.id, 'action.id')
  }
  return latest
}


// ---- Milestone 7b: public web research -------------------------------------
//
// Notice what is *not* parsed here: the addresses observed links and search
// results point at. The runtime keeps them in its own `targets` table and does
// not send them, so main -- and therefore any model main calls -- never holds
// an address a page or a search provider chose. Page addresses Lumi actually
// visited *are* parsed: they are the sources the user is shown.

const RESEARCH_REF = /^o[1-9][0-9]{0,3}$/
const RESEARCH_LINK_REF = /^l[1-9][0-9]?$/
const RESEARCH_RESULT_REF = /^r[1-9][0-9]?$/
const TAB_REF = /^t[1-5]$/
const HOST = /^[a-z0-9._:-]{1,253}$/
const HTTP_READ_METHOD = /^(GET|HEAD)$/
const SESSION_STATUS = /^[A-Z]{4,6}$/
const MAX_OBJECTIVE = 500
const MAX_RESEARCH_ANSWER = 1_200

function decimal(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1e9) {
    throw new WireError(what)
  }
  return value
}

/** The objective a public-research task was created with. */
export function parseResearchRequest(request: Json): AgentResearchRequestView {
  return { objective: text(request.objective, 'task.objective', undefined, MAX_OBJECTIVE) }
}

function parseResearchBudgets(value: unknown): AgentResearchBudgets {
  const budgets = record(value, 'research.budgets')
  return {
    maxSteps: integer(budgets.max_steps, 'budgets.max_steps', 1, 200),
    maxObservations: integer(budgets.max_observations, 'budgets.max_observations', 1, 300),
    maxPlannerCalls: integer(budgets.max_planner_calls, 'budgets.max_planner_calls', 1, 200),
    maxTabs: integer(budgets.max_tabs, 'budgets.max_tabs', 1, 5),
    maxActiveSeconds: integer(budgets.max_active_seconds, 'budgets.max_active_seconds', 10, 3_600),
    maxModelInputTokens: integer(budgets.max_model_input_tokens, 'budgets.max_input', 1_000, 1_000_000),
    maxModelOutputTokens: integer(budgets.max_model_output_tokens, 'budgets.max_output', 100, 64_000),
    maxVisionCalls: integer(budgets.max_vision_calls, 'budgets.max_vision', 0, 20)
  }
}

function labels(value: unknown, what: string): string[] {
  if (!Array.isArray(value) || value.length > 16) throw new WireError(what)
  return value.map((entry) => text(entry, what, CODE, 64))
}

/**
 * The scope the user is asked to confirm. Every field is required and nothing
 * unexpected may be present: this is the card's contents, and a card that
 * cannot be rendered exactly is not rendered at all.
 */
export function parseResearchScope(value: unknown): AgentResearchScopeView {
  const scope = record(value, 'research.scope')
  const allowed = new Set([
    'schema_version', 'kind', 'policy_version', 'allowed_operations', 'allowed', 'forbidden',
    'schemes', 'methods', 'hosts', 'budgets', 'disclosure', 'seeds'
  ])
  if (Object.keys(scope).some((key) => !allowed.has(key))) throw new WireError('research.scope.fields')
  if (scope.schema_version !== 1 || scope.kind !== 'public_research') throw new WireError('research.scope.kind')
  if (!Array.isArray(scope.allowed_operations) || scope.allowed_operations.length === 0) {
    throw new WireError('research.scope.operations')
  }
  if (!Array.isArray(scope.schemes) || !Array.isArray(scope.methods)) throw new WireError('research.scope.network')
  if (!Array.isArray(scope.seeds) || scope.seeds.length > 3) throw new WireError('research.scope.seeds')
  const disclosure = record(scope.disclosure, 'research.disclosure')
  if (!Array.isArray(disclosure.recipients) || disclosure.recipients.length < 1 || disclosure.recipients.length > 3) {
    throw new WireError('research.recipients')
  }
  let hosts: 'any_public' | string[]
  if (scope.hosts === 'any_public') {
    hosts = 'any_public'
  } else {
    if (!Array.isArray(scope.hosts) || scope.hosts.length > 64) throw new WireError('research.scope.hosts')
    hosts = scope.hosts.map((host) => text(host, 'research.scope.host', undefined, 253))
  }
  return {
    policyVersion: text(scope.policy_version, 'research.policy_version', POLICY_VERSION_PATTERN, 40),
    allowedOperations: scope.allowed_operations.map((operation) =>
      member(RESEARCH_OPERATIONS, operation, 'research.operation')),
    allowed: labels(scope.allowed, 'research.scope.allowed'),
    forbidden: labels(scope.forbidden, 'research.scope.forbidden'),
    schemes: labels(scope.schemes, 'research.scope.schemes'),
    methods: scope.methods.map((method) => text(method, 'research.scope.method', HTTP_READ_METHOD, 4)),
    hosts,
    budgets: parseResearchBudgets(scope.budgets),
    recipients: disclosure.recipients.map((recipient) =>
      member(DISCLOSURE_RECIPIENTS, recipient, 'research.recipient')),
    maxTextChars: integer(disclosure.max_text_chars, 'research.max_text_chars', 1, 12_000),
    seeds: scope.seeds.map((seed) => webUrl(seed, 'research.seed'))
  }
}

function parseResearchGrant(value: unknown, taskId: string): AgentResearchGrantView | undefined {
  if (value === null || value === undefined) return undefined
  const grant = record(value, 'research.grant')
  if (uuid(grant.task_id, 'grant.task_id') !== taskId) throw new WireError('grant.task_id')
  const scope = parseResearchScope(grant.scope)
  const confirmedAt = nullableInstant(grant.confirmed_at, 'grant.confirmed_at')
  const expiresAt = nullableInstant(grant.expires_at, 'grant.expires_at')
  const status = member(GRANT_STATUSES, grant.status, 'grant.status')
  // An active scope always has a window, and anything past PENDING was
  // confirmed. A grant that claims otherwise is not presented as live.
  if (status === 'ACTIVE' && !expiresAt) throw new WireError('grant.expires_at')
  if (status !== 'PENDING' && !confirmedAt) throw new WireError('grant.confirmed_at')
  return {
    grantId: uuid(grant.id, 'grant.id'),
    status,
    revision: integer(grant.revision, 'grant.revision', 1),
    scopeDigest: text(grant.scope_digest, 'grant.scope_digest', DIGEST, 64),
    scope,
    createdAt: instant(grant.created_at, 'grant.created_at'),
    ...(confirmedAt ? { confirmedAt } : {}),
    ...(expiresAt ? { expiresAt } : {})
  }
}

function parseResearchObservation(value: unknown, taskId: string): AgentResearchObservationView {
  const raw = record(value, 'research.observation')
  if (uuid(raw.task_id, 'observation.task_id') !== taskId) throw new WireError('observation.task_id')
  if (raw.provenance !== 'untrusted_environment' || raw.schema_version !== 1) {
    throw new WireError('observation.provenance')
  }
  if (!Array.isArray(raw.blocks) || raw.blocks.length > 120) throw new WireError('observation.blocks')
  if (!Array.isArray(raw.links) || raw.links.length > 25) throw new WireError('observation.links')
  if (!Array.isArray(raw.results) || raw.results.length > 10) throw new WireError('observation.results')
  if (!Array.isArray(raw.open_tabs) || raw.open_tabs.length > 5) throw new WireError('observation.tabs')
  if (typeof raw.settled !== 'boolean' || typeof raw.truncated !== 'boolean') throw new WireError('observation.flags')
  const kind = raw.kind
  if (kind !== 'page' && kind !== 'search_results' && kind !== 'tab_state') throw new WireError('observation.kind')
  const sequence = integer(raw.sequence, 'observation.sequence', 1, 10_000)
  const ref = text(raw.ref, 'observation.ref', RESEARCH_REF, 5)
  if (ref !== `o${sequence}`) throw new WireError('observation.ref')
  const blocks = raw.blocks.map((item, index) => {
    const block = record(item, 'observation.block')
    const id = text(block.id, 'block.id', BLOCK_ID, 4)
    if (id !== `b${index + 1}`) throw new WireError('block.order')
    return { id, text: pageText(block.text, 'block.text', 500) }
  })
  const links = raw.links.map((item, index) => {
    const link = record(item, 'observation.link')
    const id = text(link.id, 'link.id', RESEARCH_LINK_REF, 3)
    if (id !== `l${index + 1}`) throw new WireError('link.order')
    // No address: only a ref, a label and a host cross this boundary.
    if ('url' in link) throw new WireError('link.url_present')
    return {
      ref: id,
      text: pageText(link.text, 'link.text', 120, true),
      host: text(link.host, 'link.host', HOST, 253)
    }
  })
  const results = raw.results.map((item, index) => {
    const result = record(item, 'observation.result')
    const id = text(result.id, 'result.id', RESEARCH_RESULT_REF, 3)
    if (id !== `r${index + 1}`) throw new WireError('result.order')
    if ('url' in result) throw new WireError('result.url_present')
    return {
      ref: id,
      title: pageText(result.title, 'result.title', 200, true),
      host: text(result.host, 'result.host', HOST, 253),
      snippet: pageText(result.snippet, 'result.snippet', 300, true)
    }
  })
  const finalUrl = raw.final_url === null || raw.final_url === undefined
    ? undefined
    : webUrl(raw.final_url, 'observation.final_url')
  const tab = raw.tab === null || raw.tab === undefined ? undefined : text(raw.tab, 'observation.tab', TAB_REF, 2)
  const query = raw.query === null || raw.query === undefined
    ? undefined
    : pageText(raw.query, 'observation.query', 200, true)
  const sessionId = raw.session_id === null || raw.session_id === undefined
    ? undefined
    : uuid(raw.session_id, 'observation.session_id')
  const finalHost = raw.final_host === null || raw.final_host === undefined
    ? undefined
    : text(raw.final_host, 'observation.final_host', HOST, 253)
  return {
    observationId: uuid(raw.id, 'observation.id'),
    ref,
    sequence,
    kind,
    operation: member(RESEARCH_OPERATIONS, raw.operation, 'observation.operation'),
    ...(tab ? { tab } : {}),
    documentEpoch: integer(raw.document_epoch, 'observation.document_epoch', 1, 10_000),
    ...(query !== undefined ? { query } : {}),
    ...(finalUrl ? { finalUrl } : {}),
    ...(finalHost ? { finalHost } : {}),
    title: pageText(raw.title, 'observation.title', 200, true),
    settled: raw.settled,
    truncated: raw.truncated,
    observedAt: instant(raw.observed_at, 'observation.observed_at'),
    contentHash: text(raw.content_hash, 'observation.content_hash', DIGEST, 64),
    blocks,
    links,
    results,
    openTabs: raw.open_tabs.map((entry) => text(entry, 'observation.tab', TAB_REF, 2)),
    ...(sessionId ? { sessionId } : {})
  }
}

function parseResearchAnswer(value: unknown): AgentResearchAnswerView | undefined {
  if (value === null || value === undefined) return undefined
  const answer = record(value, 'research.answer')
  if (!Array.isArray(answer.evidence) || answer.evidence.length > 6) throw new WireError('answer.evidence')
  return {
    status: member(RESEARCH_ANSWER_STATUSES, answer.status, 'answer.status'),
    stopReason: member(RESEARCH_STOP_REASONS, answer.stop_reason, 'answer.stop_reason'),
    answer: text(answer.answer, 'answer.answer', undefined, MAX_RESEARCH_ANSWER),
    evidence: answer.evidence.map((item) => {
      const entry = record(item, 'answer.evidence')
      return {
        observation: text(entry.observation, 'evidence.observation', RESEARCH_REF, 5),
        block: text(entry.block, 'evidence.block', BLOCK_ID, 4),
        quote: text(entry.quote, 'evidence.quote', undefined, 300)
      }
    }),
    provider: member(DISCLOSURE_RECIPIENTS, answer.provider, 'answer.provider'),
    model: text(answer.model, 'answer.model', MODEL_NAME, 64),
    stepsUsed: integer(answer.steps_used, 'answer.steps_used', 0, 1_000),
    observationsUsed: integer(answer.observations_used, 'answer.observations_used', 0, 1_000),
    plannerCalls: integer(answer.planner_calls, 'answer.planner_calls', 0, 1_000),
    createdAt: instant(answer.created_at, 'answer.created_at')
  }
}

export interface ResearchDetail {
  view: AgentResearchView
  task: AgentTaskView
}

/** `GET /tasks/{id}/research`, and the body of every research mutation. */
export function parseResearch(value: unknown): ResearchDetail {
  const body = record(value, 'research')
  const task = parseTask(body.task)
  if (task.kind !== 'public_research') throw new WireError('research.task_kind')
  const usage = record(body.usage, 'research.usage')
  if (!Array.isArray(body.observations) || body.observations.length > 300) {
    throw new WireError('research.observations')
  }
  if (typeof body.search_configured !== 'boolean') throw new WireError('research.search_configured')
  if (typeof body.unresolved_step !== 'boolean') throw new WireError('research.unresolved_step')
  let session: AgentResearchSessionView | undefined
  if (body.session !== null && body.session !== undefined) {
    const raw = record(body.session, 'research.session')
    session = {
      sessionId: uuid(raw.id, 'session.id'),
      status: text(raw.status, 'session.status', SESSION_STATUS, 8),
      createdAt: instant(raw.created_at, 'session.created_at')
    }
  }
  const observations = body.observations.map((item) => parseResearchObservation(item, task.taskId))
  let previous = 0
  for (const observation of observations) {
    if (observation.sequence <= previous) throw new WireError('research.observation_order')
    previous = observation.sequence
  }
  const grant = parseResearchGrant(body.grant, task.taskId)
  const answer = parseResearchAnswer(body.answer)
  return {
    task,
    view: {
      taskId: task.taskId,
      objective: text(body.objective, 'research.objective', undefined, MAX_OBJECTIVE),
      ...(grant ? { grant } : {}),
      ...(session ? { session } : {}),
      observations,
      ...(answer ? { answer } : {}),
      usage: {
        steps: integer(usage.steps, 'usage.steps', 0, 10_000),
        observations: integer(usage.observations, 'usage.observations', 0, 10_000),
        plannerCalls: integer(usage.planner_calls, 'usage.planner_calls', 0, 10_000),
        activeSeconds: decimal(usage.active_seconds, 'usage.active_seconds'),
        tabs: integer(usage.tabs, 'usage.tabs', 0, 5)
      },
      searchConfigured: body.search_configured,
      unresolvedStep: body.unresolved_step
    }
  }
}

export interface ResearchStepOutcome extends ResearchDetail {
  observation?: AgentResearchObservationView
  outcome: AgentAttemptOutcome
  errorCode?: string
  replayed: boolean
}

/** `POST /tasks/{id}/research/steps`: what one step established. */
export function parseResearchStep(value: unknown): ResearchStepOutcome {
  const body = record(value, 'research.step')
  const { view, task } = parseResearch(body.research)
  const action = record(body.action, 'research.step.action')
  if (typeof action.tool_name !== 'string' || !action.tool_name.startsWith('research_')) {
    throw new WireError('research.step.tool_name')
  }
  if (typeof body.replayed !== 'boolean') throw new WireError('research.step.replayed')
  const observation = body.observation === null || body.observation === undefined
    ? undefined
    : parseResearchObservation(body.observation, view.taskId)
  const errorCode = optional(() => text(body.error_code, 'research.step.error_code', CODE, 64))
  return {
    view,
    task,
    ...(observation ? { observation } : {}),
    outcome: member(ATTEMPT_OUTCOMES, body.outcome, 'research.step.outcome'),
    ...(errorCode ? { errorCode } : {}),
    replayed: body.replayed
  }
}

// ---- Milestone 8a S3: authenticated account reading -------------------------------
//
// Notice what is *not* parsed here, and what is refused if it appears: an
// address. The runtime never sends one (the worker keeps every target in its own
// memory), so a `url` or `href` field on a link is a wire violation, not a value
// to ignore. Nor is the account fingerprint or the profile's revoke epoch sent:
// main has no use for them.

const AUTHENTICATED_SCOPE_KEYS = new Set([
  'policy_version', 'site', 'allowed_origins', 'allowed_operations', 'methods', 'classification',
  'allowed', 'forbidden', 'website_side_effects_possible', 'disclosure', 'budgets'
])
const SITE_NAME = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/
const REDACTION_KIND = /^[a-z]{3,12}$/

/** The objective and profile an authenticated task was created with. */
export function parseAuthenticatedRequest(request: Json): AgentAuthenticatedRequestView {
  if (request.classification !== 'account_private') throw new WireError('task.classification')
  return {
    objective: text(request.objective, 'task.objective', undefined, MAX_OBJECTIVE),
    profileId: uuid(request.profile_id, 'task.profile_id'),
    classification: 'account_private'
  }
}

function parseAuthenticatedBudgets(value: unknown): AgentAuthenticatedBudgets {
  const budgets = record(value, 'authenticated.budgets')
  // Structurally zero. A scope that claims otherwise is not presented at all.
  if (budgets.max_vision_calls !== 0) throw new WireError('budgets.max_vision')
  return {
    maxSteps: integer(budgets.max_steps, 'budgets.max_steps', 1, 12),
    maxObservations: integer(budgets.max_observations, 'budgets.max_observations', 1, 12),
    maxPlannerCalls: integer(budgets.max_planner_calls, 'budgets.max_planner_calls', 1, 12),
    maxAnswerCalls: integer(budgets.max_answer_calls, 'budgets.max_answer_calls', 1, 2),
    maxTabs: integer(budgets.max_tabs, 'budgets.max_tabs', 1, 3),
    maxActiveSeconds: integer(budgets.max_active_seconds, 'budgets.max_active_seconds', 10, 300),
    maxVisionCalls: 0
  }
}

function parseAuthenticatedScope(value: unknown): AgentAuthenticatedScopeView {
  const scope = record(value, 'authenticated.scope')
  if (Object.keys(scope).some((key) => !AUTHENTICATED_SCOPE_KEYS.has(key))) {
    throw new WireError('authenticated.scope.fields')
  }
  if (scope.classification !== 'account_private') throw new WireError('authenticated.scope.classification')
  if (scope.website_side_effects_possible !== true) throw new WireError('authenticated.scope.side_effects')
  if (!Array.isArray(scope.allowed_operations) || scope.allowed_operations.length === 0 || scope.allowed_operations.length > 5) {
    throw new WireError('authenticated.scope.operations')
  }
  if (!Array.isArray(scope.methods) || scope.methods.length === 0 || scope.methods.length > 2) {
    throw new WireError('authenticated.scope.methods')
  }
  const disclosure = record(scope.disclosure, 'authenticated.disclosure')
  if (disclosure.identifiers_reduced !== true || disclosure.failover !== 'none') {
    throw new WireError('authenticated.disclosure.terms')
  }
  return {
    policyVersion: text(scope.policy_version, 'authenticated.policy_version', POLICY_VERSION_PATTERN, 40),
    site: text(scope.site, 'authenticated.site', SITE_NAME, 253),
    allowedOperations: scope.allowed_operations.map((operation) =>
      member(AUTHENTICATED_OPERATIONS, operation, 'authenticated.operation')),
    allowed: labels(scope.allowed, 'authenticated.scope.allowed'),
    forbidden: labels(scope.forbidden, 'authenticated.scope.forbidden'),
    methods: scope.methods.map((method) => text(method, 'authenticated.method', HTTP_READ_METHOD, 4)),
    websiteSideEffectsPossible: true,
    recipient: member(DISCLOSURE_RECIPIENTS, disclosure.recipient, 'authenticated.recipient'),
    maxTextChars: integer(disclosure.max_text_chars, 'authenticated.max_text_chars', 1, 4_000),
    maxBlocks: integer(disclosure.max_blocks, 'authenticated.max_blocks', 1, 60),
    budgets: parseAuthenticatedBudgets(scope.budgets)
  }
}

function parseAuthenticatedGrant(value: unknown, taskId: string): AgentAuthenticatedGrantView | undefined {
  if (value === null || value === undefined) return undefined
  const grant = record(value, 'authenticated.grant')
  if (uuid(grant.task_id, 'grant.task_id') !== taskId) throw new WireError('grant.task_id')
  const scope = parseAuthenticatedScope(grant.scope)
  const confirmedAt = nullableInstant(grant.confirmed_at, 'grant.confirmed_at')
  const expiresAt = nullableInstant(grant.expires_at, 'grant.expires_at')
  const status = member(GRANT_STATUSES, grant.status, 'grant.status')
  if (status === 'ACTIVE' && !expiresAt) throw new WireError('grant.expires_at')
  if (status !== 'PENDING' && !confirmedAt) throw new WireError('grant.confirmed_at')
  return {
    grantId: uuid(grant.id, 'grant.id'),
    status,
    revision: integer(grant.revision, 'grant.revision', 1),
    scopeDigest: text(grant.scope_digest, 'grant.scope_digest', DIGEST, 64),
    scope,
    createdAt: instant(grant.created_at, 'grant.created_at'),
    ...(confirmedAt ? { confirmedAt } : {}),
    ...(expiresAt ? { expiresAt } : {})
  }
}

function parseAuthenticatedProfile(value: unknown): AgentAuthenticatedProfileView | undefined {
  if (value === null || value === undefined) return undefined
  const profile = record(value, 'authenticated.profile')
  return {
    profileId: uuid(profile.id, 'profile.id'),
    label: text(profile.label, 'profile.label', undefined, 60),
    site: text(profile.site, 'profile.site', SITE_NAME, 253),
    status: member(BROWSER_PROFILE_STATUSES, profile.status, 'profile.status')
  }
}

function parseAuthenticatedObservation(value: unknown, taskId: string): AgentAuthenticatedObservationView {
  const raw = record(value, 'authenticated.observation')
  if (uuid(raw.task_id, 'observation.task_id') !== taskId) throw new WireError('observation.task_id')
  if (raw.provenance !== 'untrusted_environment' || raw.schema_version !== 1) throw new WireError('observation.provenance')
  if (raw.classification !== 'account_private') throw new WireError('observation.classification')
  if (!Array.isArray(raw.blocks) || raw.blocks.length > 60) throw new WireError('observation.blocks')
  if (!Array.isArray(raw.links) || raw.links.length > 25) throw new WireError('observation.links')
  if (!Array.isArray(raw.open_tabs) || raw.open_tabs.length > 3) throw new WireError('observation.tabs')
  if (typeof raw.settled !== 'boolean' || typeof raw.truncated !== 'boolean') throw new WireError('observation.flags')
  const kind = raw.kind
  if (kind !== 'page' && kind !== 'tab_state') throw new WireError('observation.kind')
  const sequence = integer(raw.sequence, 'observation.sequence', 1, 10_000)
  const ref = text(raw.ref, 'observation.ref', RESEARCH_REF, 5)
  if (ref !== `o${sequence}`) throw new WireError('observation.ref')
  const blocks = raw.blocks.map((item, index) => {
    const block = record(item, 'observation.block')
    const id = text(block.id, 'block.id', BLOCK_ID, 4)
    if (id !== `b${index + 1}`) throw new WireError('block.order')
    return { id, text: pageText(block.text, 'block.text', 500) }
  })
  const links = raw.links.map((item, index) => {
    const link = record(item, 'observation.link')
    const id = text(link.id, 'link.id', RESEARCH_LINK_REF, 3)
    if (id !== `l${index + 1}`) throw new WireError('link.order')
    // An address here would be a private capability crossing the boundary.
    if ('url' in link || 'href' in link) throw new WireError('link.url_present')
    return {
      ref: id,
      text: pageText(link.text, 'link.text', 120, true),
      host: text(link.host, 'link.host', HOST, 253)
    }
  })
  const redactionsRaw = record(raw.redactions, 'observation.redactions')
  const redactions: Record<string, number> = {}
  for (const [name, count] of Object.entries(redactionsRaw)) {
    if (!REDACTION_KIND.test(name)) throw new WireError('observation.redaction')
    redactions[name] = integer(count, 'observation.redaction.count', 0, 100_000)
  }
  const tab = raw.tab === null || raw.tab === undefined ? undefined : text(raw.tab, 'observation.tab', TAB_REF, 2)
  const host = raw.host === null || raw.host === undefined ? undefined : text(raw.host, 'observation.host', HOST, 253)
  return {
    observationId: uuid(raw.id, 'observation.id'),
    ref,
    sequence,
    kind,
    operation: member(AUTHENTICATED_OPERATIONS, raw.operation, 'observation.operation'),
    ...(tab ? { tab } : {}),
    documentEpoch: integer(raw.document_epoch, 'observation.document_epoch', 1, 10_000),
    ...(host ? { host } : {}),
    title: pageText(raw.title, 'observation.title', 200, true),
    settled: raw.settled,
    truncated: raw.truncated,
    observedAt: instant(raw.observed_at, 'observation.observed_at'),
    contentHash: text(raw.content_hash, 'observation.content_hash', DIGEST, 64),
    blocks,
    links,
    openTabs: raw.open_tabs.map((entry) => text(entry, 'observation.tab', TAB_REF, 2)),
    redactions
  }
}

function parseAuthenticatedAnswer(value: unknown): AgentAuthenticatedAnswerView | undefined {
  if (value === null || value === undefined) return undefined
  const answer = record(value, 'authenticated.answer')
  if (answer.classification !== 'account_private') throw new WireError('answer.classification')
  if (!Array.isArray(answer.evidence) || answer.evidence.length > 6) throw new WireError('answer.evidence')
  return {
    classification: 'account_private',
    profileId: uuid(answer.profile_id, 'answer.profile_id'),
    status: member(RESEARCH_ANSWER_STATUSES, answer.status, 'answer.status'),
    stopReason: member(RESEARCH_STOP_REASONS, answer.stop_reason, 'answer.stop_reason'),
    answer: text(answer.answer, 'answer.answer', undefined, MAX_RESEARCH_ANSWER),
    evidence: answer.evidence.map((item) => {
      const entry = record(item, 'answer.evidence')
      return {
        observation: text(entry.observation, 'evidence.observation', RESEARCH_REF, 5),
        block: text(entry.block, 'evidence.block', BLOCK_ID, 4),
        quote: text(entry.quote, 'evidence.quote', undefined, 300)
      }
    }),
    provider: member(DISCLOSURE_RECIPIENTS, answer.provider, 'answer.provider'),
    model: text(answer.model, 'answer.model', MODEL_NAME, 64),
    stepsUsed: integer(answer.steps_used, 'answer.steps_used', 0, 1_000),
    observationsUsed: integer(answer.observations_used, 'answer.observations_used', 0, 1_000),
    plannerCalls: integer(answer.planner_calls, 'answer.planner_calls', 0, 1_000),
    createdAt: instant(answer.created_at, 'answer.created_at')
  }
}

export interface AuthenticatedDetail {
  view: AgentAuthenticatedView
  task: AgentTaskView
}

/** `GET /tasks/{id}/authenticated`, and the body of every account-reading mutation. */
export function parseAuthenticated(value: unknown): AuthenticatedDetail {
  const body = record(value, 'authenticated')
  const task = parseTask(body.task)
  if (task.kind !== 'authenticated_read') throw new WireError('authenticated.task_kind')
  if (body.classification !== 'account_private') throw new WireError('authenticated.classification')
  const usage = record(body.usage, 'authenticated.usage')
  if (!Array.isArray(body.observations) || body.observations.length > 100) throw new WireError('authenticated.observations')
  if (typeof body.unresolved_step !== 'boolean') throw new WireError('authenticated.unresolved_step')
  const observations = body.observations.map((item) => parseAuthenticatedObservation(item, task.taskId))
  let previous = 0
  for (const observation of observations) {
    if (observation.sequence <= previous) throw new WireError('authenticated.observation_order')
    previous = observation.sequence
  }
  const grant = parseAuthenticatedGrant(body.grant, task.taskId)
  const profile = parseAuthenticatedProfile(body.profile)
  const answer = parseAuthenticatedAnswer(body.answer)
  const pauseReason = body.pause_reason === null || body.pause_reason === undefined
    ? undefined
    : member(AUTHENTICATED_PAUSE_REASONS, body.pause_reason, 'authenticated.pause_reason')
  return {
    task,
    view: {
      taskId: task.taskId,
      objective: text(body.objective, 'authenticated.objective', undefined, MAX_OBJECTIVE),
      classification: 'account_private',
      ...(profile ? { profile } : {}),
      ...(grant ? { grant } : {}),
      observations,
      ...(answer ? { answer } : {}),
      usage: {
        steps: integer(usage.steps, 'usage.steps', 0, 10_000),
        observations: integer(usage.observations, 'usage.observations', 0, 10_000),
        plannerCalls: integer(usage.planner_calls, 'usage.planner_calls', 0, 10_000),
        activeSeconds: decimal(usage.active_seconds, 'usage.active_seconds'),
        tabs: integer(usage.tabs, 'usage.tabs', 0, 3)
      },
      ...(pauseReason ? { pauseReason } : {}),
      unresolvedStep: body.unresolved_step
    }
  }
}

export interface AuthenticatedStepOutcome extends AuthenticatedDetail {
  observation?: AgentAuthenticatedObservationView
  outcome: AgentAttemptOutcome
  errorCode?: string
  pauseReason?: AgentAuthenticatedPauseReason
  replayed: boolean
}

/** `POST /tasks/{id}/authenticated/steps`: what one step established. */
export function parseAuthenticatedStep(value: unknown): AuthenticatedStepOutcome {
  const body = record(value, 'authenticated.step')
  const { view, task } = parseAuthenticated(body.authenticated)
  const action = record(body.action, 'authenticated.step.action')
  if (typeof action.tool_name !== 'string' || !action.tool_name.startsWith('authenticated_')) {
    throw new WireError('authenticated.step.tool_name')
  }
  if (typeof body.replayed !== 'boolean') throw new WireError('authenticated.step.replayed')
  const observation = body.observation === null || body.observation === undefined
    ? undefined
    : parseAuthenticatedObservation(body.observation, view.taskId)
  const errorCode = optional(() => text(body.error_code, 'authenticated.step.error_code', CODE, 64))
  const pauseReason = body.pause_reason === null || body.pause_reason === undefined
    ? undefined
    : member(AUTHENTICATED_PAUSE_REASONS, body.pause_reason, 'authenticated.step.pause_reason')
  return {
    view,
    task,
    ...(observation ? { observation } : {}),
    outcome: member(ATTEMPT_OUTCOMES, body.outcome, 'authenticated.step.outcome'),
    ...(errorCode ? { errorCode } : {}),
    ...(pauseReason ? { pauseReason } : {}),
    replayed: body.replayed
  }
}

// ---- errors ----------------------------------------------------------------

/** What each deterministic "profile cannot be read" reason tells the user to do. */
const AUTHENTICATED_PROFILE_MESSAGES: Record<string, string> = {
  profile_not_found: 'That account profile no longer exists. Nothing was opened.',
  profile_deleted: 'That account profile was deleted. Nothing was opened.',
  profile_not_authenticated: 'You are not signed in through this profile. Use "Sign in manually", then ask again. Nothing was opened.',
  login_required: 'The website asked you to sign in again. Use "Sign in manually", then ask again. Nothing was sent to an AI.',
  account_fingerprint_unknown: 'Lumi cannot tell which account this profile is signed in to, so it will not read it. Nothing was opened.',
  account_changed: 'The account signed in through this profile changed. Sign in again and review a new permission card.',
  profile_takeover_active: 'A sign-in window is open for this profile. Finish or cancel it first.',
  profile_in_use: 'This profile is in use by another Lumi window. Nothing was opened.'
}

/** What each deterministic form-planning refusal tells the user. None of them changed a website. */
const FORM_PLAN_MESSAGES: Record<string, string> = {
  protected_value_changed: 'A saved detail changed after this plan was made, so it cannot be approved. Nothing was changed; plan the form again.',
  account_changed: 'The account signed in through this profile changed. Nothing was changed; sign in again and start over.',
  stale_observation: 'The page was read again after this plan was made. Nothing was changed; plan the form again.',
  stale_document_epoch: 'The page changed after this plan was made. Nothing was changed; plan the form again.',
  stale_form_epoch: 'The form changed after this plan was made. Nothing was changed; plan the form again.',
  origin_changed: 'The page is no longer on the site this plan was made for. Nothing was changed.',
  grant_not_usable: 'The form-planning permission is no longer usable. Nothing was sent or changed.',
  no_form_observed: 'Lumi has not read a form on this account yet. Nothing was sent.',
  data_ref_unavailable: 'One of the chosen saved details is not saved yet. Nothing was sent.',
  data_ref_not_allowed: 'That plan used a saved detail you did not allow for planning, so it was refused.',
  recipient_mismatch: 'That plan came from a provider that was not approved for planning, so it was refused.',
  too_many_entries: 'That plan listed more fields than the limit, so it was refused.',
  no_entries: 'That plan listed no fields, so nothing was proposed.',
  unsupported_proposal: 'That plan contained something Lumi does not accept, so it was refused.',
  use_disclosure_route: 'That approval can only be given from the disclosure card.',
  not_a_disclosure_approval: 'That is not a form-disclosure approval.'
}

const ERROR_MAP: Record<string, { code: AgentError['code']; message: string }> = {
  task_not_found: { code: 'not_found', message: 'That task no longer exists.' },
  action_not_found: { code: 'not_found', message: 'That booking no longer exists.' },
  stale_action_revision: { code: 'stale_revision', message: 'The booking changed since you reviewed it. Review the current details.' },
  stale_revision: { code: 'stale_revision', message: 'The task changed since you reviewed it.' },
  approval_not_usable: { code: 'approval_not_usable', message: 'That approval can no longer be used. Review the booking again.' },
  invalid_action_transition: { code: 'invalid_transition', message: 'That step is not available for this booking any more.' },
  no_unfinished_attempt: { code: 'invalid_transition', message: 'That step is not available for this booking any more.' },
  concurrent_modification: { code: 'stale_revision', message: 'The booking changed at the same time. Review the current details.' },
  action_already_open: { code: 'action_already_open', message: 'This task already has a booking in progress. Resolve it first.' },
  booking_slot_unavailable: { code: 'slot_unavailable', message: 'That appointment is no longer offered. Nothing was booked.' },
  browser_worker_unavailable: { code: 'browser_unavailable', message: 'The browser worker is not available right now. Nothing was submitted.' },
  browser_worker_not_configured: { code: 'browser_unavailable', message: 'Browser booking is not configured. Nothing was submitted.' },
  browser_observation_failed: { code: 'browser_unavailable', message: 'The appointment site could not be read. Nothing was submitted.' },
  task_not_accepting_actions: { code: 'not_accepting_actions', message: 'This task is closed.' },
  task_not_cancellable: { code: 'not_accepting_actions', message: 'This task is closed.' },
  task_has_unresolved_action: { code: 'active_task_unresolved', message: 'The current booking has an unresolved outcome. Check it first.' },
  task_already_booked: { code: 'already_booked', message: 'This task already has a confirmed booking.' },
  invalid_booking_criteria: { code: 'invalid_criteria', message: 'Those booking constraints could not be applied.' },
  booking_criteria_mismatch: { code: 'criteria_mismatch', message: 'That appointment no longer matches what you asked for. Nothing was prepared.' },
  task_kind_mismatch: { code: 'invalid_request', message: 'That step does not apply to this kind of task.' },
  destination_not_allowed: { code: 'destination_not_allowed', message: 'That address cannot be inspected.' },
  public_inspection_not_configured: { code: 'inspection_unavailable', message: 'Page inspection is not set up on this computer. Nothing was opened.' },
  invalid_inspection_proposal: { code: 'invalid_response', message: 'The stored inspection could not be read. Nothing was opened.' },
  observation_not_available: { code: 'answer_unavailable', message: 'There is no saved page observation to answer from.' },
  stale_observation: { code: 'stale_observation', message: 'The page was inspected again. Lumi answers only from the newest inspection.' },
  answer_not_grounded: { code: 'answer_unavailable', message: 'Lumi refused an answer the inspected page did not support.' },
  browser_execution_not_supported: { code: 'invalid_request', message: 'That step does not apply to this kind of task.' },
  research_not_configured: { code: 'research_unavailable', message: 'Public web research is not set up on this computer. Nothing was searched or opened.' },
  research_grant_not_found: { code: 'research_not_granted', message: 'That research task has no scope to work under. Start it again.' },
  research_grant_not_usable: { code: 'research_not_granted', message: 'That research permission is no longer usable. Nothing was searched or opened.' },
  research_step_refused: { code: 'research_refused', message: 'Lumi refused that research step.' },
  research_budget_exhausted: { code: 'research_budget_exhausted', message: 'This research task reached one of its limits and stopped.' },
  research_step_in_flight: { code: 'research_in_flight', message: 'Lumi is still finishing the previous research step.' },
  research_session_unavailable: { code: 'research_unavailable', message: 'The isolated research browser is not available. Nothing was opened.' },
  research_answer_already_recorded: { code: 'invalid_transition', message: 'This research task already has an answer.' },
  research_answer_not_grounded: { code: 'answer_unavailable', message: 'Lumi refused an answer the pages it read did not support.' },
  research_search_failed: { code: 'research_unavailable', message: 'The public search could not be completed. Nothing else was opened.' },
  authenticated_not_configured: { code: 'authenticated_unavailable', message: 'Account reading is not set up on this computer. Nothing was opened.' },
  authenticated_profile_unavailable: { code: 'authenticated_unavailable', message: 'That account profile cannot be read right now. Nothing was opened.' },
  authenticated_grant_not_found: { code: 'authenticated_not_granted', message: 'That account-reading task has no permission to work under. Start it again.' },
  authenticated_grant_not_usable: { code: 'authenticated_not_granted', message: 'That account-reading permission is no longer usable. Nothing was opened or sent.' },
  authenticated_step_refused: { code: 'authenticated_refused', message: 'Lumi refused that account-reading step.' },
  authenticated_budget_exhausted: { code: 'authenticated_budget_exhausted', message: 'This account-reading task reached one of its limits and stopped.' },
  authenticated_step_in_flight: { code: 'authenticated_in_flight', message: 'Lumi is still finishing the previous account-reading step.' },
  authenticated_answer_already_recorded: { code: 'invalid_transition', message: 'This account-reading task already has an answer.' },
  form_prepare_refused: { code: 'form_plan_refused', message: 'Lumi refused that form plan. Nothing was changed.' },
  form_prepare_state_changed: { code: 'form_plan_stale', message: 'Something changed since that plan was made. Nothing was changed; plan the form again.' },
  protected_value_refused: { code: 'invalid_request', message: 'Lumi refused that saved detail.' },
  invalid_request: { code: 'invalid_request', message: 'Lumi refused an invalid request.' }
}

export function projectRuntimeError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const runtimeCode = typeof body?.code === 'string' ? body.code : ''
  const mapped = ERROR_MAP[runtimeCode]
  const error: AgentError = mapped
    ? { ...mapped }
    : { code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed', message: 'The agent runtime could not complete that request.' }
  const current = body?.current_revision
  if (typeof current === 'number' && Number.isSafeInteger(current) && current >= 1) error.currentRevision = current
  if (runtimeCode === 'destination_not_allowed' && typeof body?.reason === 'string' && CODE.test(body.reason)) {
    error.message = describeRefusal(body.reason)
  }
  if (runtimeCode === 'authenticated_profile_unavailable' && typeof body?.reason === 'string') {
    error.message = AUTHENTICATED_PROFILE_MESSAGES[body.reason] ?? error.message
  }
  if ((runtimeCode === 'form_prepare_refused' || runtimeCode === 'form_prepare_state_changed') && typeof body?.reason === 'string') {
    error.message = FORM_PLAN_MESSAGES[body.reason] ?? error.message
  }
  return error
}

// ---- Milestone 8b S5: form planning and exact disclosure approval ------------------------

const DATA_KIND = (value: unknown, what: string): AgentProtectedDataKind => member(PROTECTED_DATA_KINDS, value, what)
const FORM_REF = /^f[1-5]$/
const ELEMENT_REF = /^e([1-9]|[1-3][0-9]|40)$/
const OPTION_REF = /^op([1-9]|1[0-9]|2[0-5])$/
const OBSERVATION_REF = /^o[1-9][0-9]{0,3}$/
const PLAN_TEXT = 200

/** Keys that must never appear anywhere in a form-plan payload from the runtime. */
const FORBIDDEN_PLAN_KEYS = ['value', 'value_digest', 'manifest_digest', 'element_identity_hash', 'option_identity_hash', 'account_fingerprint', 'account_binding', 'recipient_origin', 'origin', 'selector', 'profile_revoke_epoch']

function assertNoForbiddenKeys(value: unknown, what: string, depth = 0): void {
  if (depth > 8) throw new WireError(what)
  if (Array.isArray(value)) {
    for (const item of value) assertNoForbiddenKeys(item, what, depth + 1)
  } else if (isRecord(value)) {
    for (const [key, child] of Object.entries(value)) {
      if (FORBIDDEN_PLAN_KEYS.includes(key)) throw new WireError(what)
      assertNoForbiddenKeys(child, what, depth + 1)
    }
  }
}

function parseSavedDetail(value: unknown): AgentSavedDetailView {
  const detail = record(value, 'saved_detail')
  const kind = DATA_KIND(detail.kind, 'saved_detail.kind')
  if (DATA_KIND(detail.data_ref, 'saved_detail.data_ref') !== kind) throw new WireError('saved_detail.data_ref')
  return {
    dataRef: kind,
    kind,
    preview: text(detail.preview, 'saved_detail.preview', undefined, 120),
    updatedAt: instant(detail.updated_at, 'saved_detail.updated_at')
  }
}

function parseFormGrant(value: unknown): AgentFormGrantView | undefined {
  if (value === null || value === undefined) return undefined
  const grant = record(value, 'form_plan.grant')
  const scope = record(grant.scope, 'form_plan.grant.scope')
  if (scope.failover !== 'none' || scope.freeze_required !== true || scope.classification !== 'account_private') {
    throw new WireError('form_plan.grant.scope')
  }
  if (!Array.isArray(scope.allowed_data_refs) || scope.allowed_data_refs.length === 0 || scope.allowed_data_refs.length > PROTECTED_DATA_KINDS.length) {
    throw new WireError('form_plan.grant.allowed_data_refs')
  }
  const expiresAt = nullableInstant(grant.expires_at, 'form_plan.grant.expires_at')
  const status = member(GRANT_STATUSES, grant.status, 'form_plan.grant.status')
  if (status === 'ACTIVE' && !expiresAt) throw new WireError('form_plan.grant.expires_at')
  return {
    grantId: uuid(grant.grant_id, 'form_plan.grant.grant_id'),
    status,
    revision: integer(grant.revision, 'form_plan.grant.revision', 1),
    ...(expiresAt ? { expiresAt } : {}),
    allowedDataRefs: scope.allowed_data_refs.map((item) => DATA_KIND(item, 'form_plan.grant.allowed_data_refs')),
    planningRecipient: member(DISCLOSURE_RECIPIENTS, scope.planning_recipient, 'form_plan.grant.planning_recipient'),
    maxFields: integer(scope.max_fields, 'form_plan.grant.max_fields', 1, 12)
  }
}

function parseDisclosureField(value: unknown): AgentDisclosureFieldView {
  const field = record(value, 'form_plan.field')
  const base = {
    fieldLabel: text(field.field_label, 'form_plan.field.label', undefined, PLAN_TEXT),
    controlType: text(field.control_type, 'form_plan.field.control_type', CODE, 32)
  }
  switch (field.kind) {
    case 'saved_detail':
      return { kind: 'saved_detail', ...base, dataRef: DATA_KIND(field.data_ref, 'form_plan.field.data_ref'), preview: text(field.preview, 'form_plan.field.preview', undefined, 120) }
    case 'option':
      return { kind: 'option', ...base, optionLabel: text(field.option_label, 'form_plan.field.option_label', undefined, PLAN_TEXT) }
    case 'checkbox':
      if (typeof field.checked !== 'boolean') throw new WireError('form_plan.field.checked')
      return { kind: 'checkbox', ...base, checked: field.checked }
    default:
      throw new WireError('form_plan.field.kind')
  }
}

function parseDisclosureCard(value: unknown): AgentDisclosureCardView | undefined {
  if (value === null || value === undefined) return undefined
  const card = record(value, 'form_plan.disclosure')
  if (!Array.isArray(card.fields) || card.fields.length === 0 || card.fields.length > 12) throw new WireError('form_plan.disclosure.fields')
  if (typeof card.reveals_country !== 'boolean') throw new WireError('form_plan.disclosure.reveals_country')
  const approvalStatus = card.approval_status === null || card.approval_status === undefined
    ? undefined
    : member(APPROVAL_STATUSES, card.approval_status, 'form_plan.disclosure.approval_status')
  const expires = nullableInstant(card.approval_expires_at, 'form_plan.disclosure.approval_expires_at')
  const resultCode = card.result_code === null || card.result_code === undefined
    ? undefined
    : member(['prepared_nothing'] as const, card.result_code, 'form_plan.disclosure.result_code')
  const formLabel = card.form_label === null || card.form_label === undefined
    ? undefined
    : text(card.form_label, 'form_plan.disclosure.form_label', undefined, PLAN_TEXT)
  return {
    actionId: uuid(card.action_id, 'form_plan.disclosure.action_id'),
    revision: integer(card.revision, 'form_plan.disclosure.revision', 1),
    actionStatus: member(ACTION_STATUSES, card.action_status, 'form_plan.disclosure.action_status'),
    ...(approvalStatus ? { approvalStatus } : {}),
    ...(expires ? { approvalExpiresAt: expires } : {}),
    site: text(card.site, 'form_plan.disclosure.site', SITE_NAME, 253),
    ...(formLabel ? { formLabel } : {}),
    fields: card.fields.map(parseDisclosureField),
    revealsCountry: card.reveals_country,
    ...(resultCode ? { resultCode } : {})
  }
}

export function parseFormPlan(value: unknown): AgentFormPlanView {
  assertNoForbiddenKeys(value, 'form_plan.forbidden_key')
  const body = record(value, 'form_plan')
  if (!Array.isArray(body.saved_details) || body.saved_details.length > PROTECTED_DATA_KINDS.length) throw new WireError('form_plan.saved_details')
  const grant = parseFormGrant(body.grant)
  const disclosure = parseDisclosureCard(body.disclosure)
  const site = body.site === null || body.site === undefined ? undefined : text(body.site, 'form_plan.site', SITE_NAME, 253)
  return {
    taskId: uuid(body.task_id, 'form_plan.task_id'),
    ...(site ? { site } : {}),
    savedDetails: body.saved_details.map(parseSavedDetail),
    ...(grant ? { grant } : {}),
    ...(disclosure ? { disclosure } : {}),
    formCount: integer(body.form_count, 'form_plan.form_count', 0, 5),
    candidateElementCount: integer(body.candidate_element_count, 'form_plan.candidate_element_count', 0, 40)
  }
}

/** The one planning context the runtime returns under a confirmed grant. */
export function parsePlanningContext(value: unknown): FormPlanningContext {
  assertNoForbiddenKeys(value, 'planning_context.forbidden_key')
  const body = record(value, 'planning_context')
  if (!Array.isArray(body.forms) || body.forms.length === 0 || body.forms.length > 5) throw new WireError('planning_context.forms')
  if (!Array.isArray(body.saved_data) || body.saved_data.length === 0 || body.saved_data.length > PROTECTED_DATA_KINDS.length) {
    throw new WireError('planning_context.saved_data')
  }
  return {
    grantId: uuid(body.grant_id, 'planning_context.grant_id'),
    recipient: member(DISCLOSURE_RECIPIENTS, body.recipient, 'planning_context.recipient'),
    objective: text(body.objective, 'planning_context.objective', undefined, MAX_OBJECTIVE),
    siteDisplay: text(body.site_display, 'planning_context.site_display', SITE_NAME, 253),
    observation: text(body.observation, 'planning_context.observation', OBSERVATION_REF, 8),
    forms: body.forms.map((raw) => {
      const form = record(raw, 'planning_context.form')
      if (!Array.isArray(form.elements) || form.elements.length > 40) throw new WireError('planning_context.elements')
      const label = form.label === null || form.label === undefined ? null : text(form.label, 'planning_context.form.label', undefined, PLAN_TEXT)
      return {
        formRef: text(form.form_ref, 'planning_context.form_ref', FORM_REF, 4),
        label,
        elements: form.elements.map((item) => {
          const element = record(item, 'planning_context.element')
          const options = Array.isArray(element.option_refs) ? element.option_refs : []
          if (options.length > 25) throw new WireError('planning_context.options')
          const flag = (key: string): boolean => {
            if (typeof element[key] !== 'boolean') throw new WireError(`planning_context.${key}`)
            return element[key] as boolean
          }
          return {
            elementRef: text(element.element_ref, 'planning_context.element_ref', ELEMENT_REF, 4),
            role: text(element.role, 'planning_context.role', CODE, 32),
            controlType: text(element.control_type, 'planning_context.control_type', CODE, 32),
            accessibleName: typeof element.accessible_name === 'string' && element.accessible_name.length <= PLAN_TEXT
              ? element.accessible_name.replace(/[\x00-\x1f\x7f]/g, ' ')
              : (() => { throw new WireError('planning_context.accessible_name') })(),
            required: flag('required'),
            enabled: flag('enabled'),
            visible: flag('visible'),
            readOnly: flag('read_only'),
            maxLength: element.max_length === null || element.max_length === undefined
              ? null
              : integer(element.max_length, 'planning_context.max_length', 0, 1_000_000),
            submitLike: flag('submit_like'),
            optionRefs: options.map((option) => {
              const parsed = record(option, 'planning_context.option')
              return {
                ref: text(parsed.ref, 'planning_context.option.ref', OPTION_REF, 5),
                label: typeof parsed.label === 'string' && parsed.label.length <= PLAN_TEXT
                  ? parsed.label.replace(/[\x00-\x1f\x7f]/g, ' ')
                  : (() => { throw new WireError('planning_context.option.label') })()
              }
            })
          }
        })
      }
    }),
    savedData: body.saved_data.map((raw) => {
      const item = record(raw, 'planning_context.saved_data')
      const kind = DATA_KIND(item.kind, 'planning_context.saved_data.kind')
      if (DATA_KIND(item.data_ref, 'planning_context.saved_data.data_ref') !== kind) throw new WireError('planning_context.saved_data.data_ref')
      return { dataRef: kind, kind, preview: text(item.preview, 'planning_context.saved_data.preview', undefined, 120) }
    })
  }
}
