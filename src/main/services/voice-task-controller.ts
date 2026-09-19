import {
  BOOKING_DAYS,
  OPEN_ACTION_STATUSES,
  TERMINAL_TASK_STATUSES,
  UNRESOLVED_ACTION_STATUSES,
  type AgentActionView,
  type AgentBookingCriteria,
  type AgentBookingDay,
  type AgentClinicInfoQuery,
  type AgentDoctorProfileView,
  type AgentError,
  type AgentEventView,
  type AgentResult,
  type AgentSlotView,
  type AgentTaskSnapshot
} from '../../shared/agent-contracts'
import {
  MAX_PLAN_STEPS,
  MAX_VOICE_ORDINAL,
  MAX_VOICE_PRICE,
  PART_OF_DAY_WINDOWS,
  PLAN_CHOICE_STRATEGIES,
  PROGRESSING_VOICE_COMMANDS,
  VOICE_CLEARABLE_FIELDS,
  VOICE_PARTS_OF_DAY,
  VOICE_PRICE_CURRENCY,
  VOICE_SPECIALTIES,
  type VoiceBookingFact,
  type VoiceClarification,
  type VoiceClearableField,
  type VoiceCommandKind,
  type VoiceInspectionState,
  type VoiceConstraintFact,
  type PlanChoice,
  type PlanStepName,
  type PlanStepReport,
  type TaskPlan,
  type VoiceDateOption,
  type VoiceNarration,
  type VoiceProfileFact,
  type VoiceRefinement,
  type VoiceSearchConstraints,
  type VoiceSelection,
  type VoiceSlotFact,
  type VoiceTaskCommand,
  type VoiceTaskFocus,
  type VoiceTaskOutcome,
  type VoiceTurn
} from '../../shared/voice-task-contracts'
import type { PreferenceKey, PreferenceValue } from '../../shared/model-contracts'
import {
  parseRelativeDayPhrase,
  resolveRelativeDay,
  type ResolvedDateWindow
} from '../../shared/relative-dates'
import { parsePreferenceValue, type PreferenceMemory } from '../agent/agent-memory'
import { NO_DIAGNOSTICS, type DiagnosticsSink } from '../agent/diagnostics'
import { AgentRequestError, parseClinicInfoQuery, toAgentError, type AgentTaskController } from './agent-tasks'
import { isRecord } from './agent-wire'

/**
 * The voice front door to the durable booking task.
 *
 * A realtime model's tool call becomes a closed `VoiceTaskCommand`; this class
 * decides whether it is legal in the current durable state and runs it through
 * the same `AgentTaskController` the booking panel uses. It owns no task state
 * of its own beyond remembering which completed turns it already handled.
 *
 * What it can never do is visible in `VoiceTaskBackend`: approving and
 * executing are not part of the capability it is given. "Book it" surfaces the
 * trusted approval card and nothing else.
 *
 * Every fact in an outcome is read back from durable, browser-observed task
 * state after the step ran, so what Lumi says, what the panel shows and what a
 * later selection resolves against are the same record.
 */

/** Exactly the controller operations voice may reach. No approve, no execute. */
export type VoiceTaskBackend = Pick<
  AgentTaskController,
  | 'loadActiveTask'
  | 'createBookingTask'
  | 'reviseCriteria'
  | 'cancelActiveTask'
  | 'searchAppointments'
  | 'prepareBooking'
  | 'requestApproval'
  | 'rejectAction'
  | 'reconcileAction'
  | 'createClinicInfoTask'
  | 'lookupClinicInfo'
>

const TURN_ID = /^[A-Za-z0-9_-]{1,64}$/
const CLOCK = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const SELECTION_DOCTOR = /^[\p{L}\p{M}][\p{L}\p{M} .'-]{0,59}$/u
const NARRATABLE_NAME = /^[\p{L}\p{M}][\p{L}\p{M} .'-]{0,59}$/u
const MAX_UTTERANCE = 1_000
const MAX_REMEMBERED_TURNS = 256
const UNNAMED_DOCTOR = 'the doctor shown on screen'

// ---- command parsing ---------------------------------------------------------

class VoiceCommandError extends AgentRequestError {
  constructor(message: string) {
    super({ code: 'invalid_request', message })
  }
}

function reject(message: string): never {
  throw new VoiceCommandError(message)
}

function closedRecord(value: unknown, allowed: readonly string[], what: string): Record<string, unknown> {
  if (!isRecord(value)) reject(`The ${what} is invalid.`)
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) reject(`The ${what} has an unexpected field.`)
  }
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) reject(`The ${what} is invalid.`)
  return value as T
}

function clock(value: unknown, what: string): string {
  if (typeof value !== 'string' || !CLOCK.test(value)) reject(`The ${what} must be 24-hour HH:MM.`)
  return value
}

function parseTurn(value: unknown): VoiceTurn {
  const turn = closedRecord(value, ['turnId', 'utterance'], 'voice turn')
  if (typeof turn.turnId !== 'string' || !TURN_ID.test(turn.turnId)) reject('The voice turn is invalid.')
  if (typeof turn.utterance !== 'string') reject('The voice turn is invalid.')
  const utterance = turn.utterance.replace(/\s+/gu, ' ').trim()
  // eslint-disable-next-line no-control-regex
  if (!utterance || utterance.length > MAX_UTTERANCE || /[\x00-\x1f\x7f]/u.test(utterance)) {
    reject('The voice turn is invalid.')
  }
  return { turnId: turn.turnId, utterance }
}

const CONSTRAINT_KEYS = ['specialty', 'day', 'partOfDay', 'earliestTime', 'latestTime', 'maxPriceInr', 'when'] as const

function parseConstraintFields(value: Record<string, unknown>): VoiceSearchConstraints {
  const constraints: VoiceSearchConstraints = {}
  if (value.specialty !== undefined) constraints.specialty = member(VOICE_SPECIALTIES, value.specialty, 'specialty')
  if (value.day !== undefined) constraints.day = member(BOOKING_DAYS, value.day, 'day')
  if (value.partOfDay !== undefined) constraints.partOfDay = member(VOICE_PARTS_OF_DAY, value.partOfDay, 'part of day')
  if (value.earliestTime !== undefined) constraints.earliestTime = clock(value.earliestTime, 'earliest time')
  if (value.latestTime !== undefined) constraints.latestTime = clock(value.latestTime, 'latest time')
  if (value.maxPriceInr !== undefined) {
    const price = value.maxPriceInr
    if (typeof price !== 'number' || !Number.isSafeInteger(price) || price < 0 || price > MAX_VOICE_PRICE) {
      reject('The price limit is invalid.')
    }
    constraints.maxPriceInr = price
  }
  if (value.when !== undefined) {
    try {
      constraints.when = parseRelativeDayPhrase(value.when)
    } catch {
      reject('The day is invalid.')
    }
  }
  return constraints
}

function parseRefinement(value: unknown): VoiceRefinement {
  const changesValue = closedRecord(value, [...CONSTRAINT_KEYS, 'clear'], 'refinement')
  const changes: VoiceRefinement = parseConstraintFields(changesValue)
  if (changesValue.clear !== undefined) {
    const clear = changesValue.clear
    if (!Array.isArray(clear) || clear.length > VOICE_CLEARABLE_FIELDS.length) reject('The refinement is invalid.')
    const fields = clear.map((field) => member(VOICE_CLEARABLE_FIELDS, field, 'refinement'))
    if (new Set(fields).size !== fields.length) reject('The refinement is invalid.')
    if (fields.length > 0) changes.clear = fields
  }
  if (Object.keys(changes).length === 0) reject('Say what should change.')
  return changes
}

function parseChoice(value: unknown): PlanChoice {
  const raw = closedRecord(value, ['strategy', 'ordinal', 'time', 'doctor'], 'choice')
  const strategy = member(PLAN_CHOICE_STRATEGIES, raw.strategy, 'choice')
  const selection = raw.ordinal !== undefined || raw.time !== undefined || raw.doctor !== undefined
    ? parseSelection({ ordinal: raw.ordinal, time: raw.time, doctor: raw.doctor })
    : {}
  const choice: PlanChoice = { strategy, ...selection }
  if (strategy === 'number' && choice.ordinal === undefined) reject('Say which result number you mean.')
  if (strategy === 'time' && choice.time === undefined) reject('Say which time you mean.')
  if (strategy === 'doctor' && choice.doctor === undefined) reject('Say which doctor you mean.')
  return choice
}

/** The durable steps a plan asks for, in execution order. */
export function planStepNames(plan: TaskPlan): PlanStepName[] {
  const steps: PlanStepName[] = []
  if (plan.search) steps.push('search')
  if (plan.refine) steps.push('refine')
  if (plan.choose) steps.push('choose')
  if (plan.prepare) steps.push('prepare')
  if (plan.showForApproval) steps.push('show_for_approval')
  return steps
}

export function parseTaskPlan(value: unknown): TaskPlan {
  const raw = closedRecord(value, ['search', 'refine', 'choose', 'prepare', 'showForApproval'], 'plan')
  const plan: TaskPlan = {}
  if (raw.search !== undefined) plan.search = parseConstraintFields(closedRecord(raw.search, CONSTRAINT_KEYS, 'search'))
  if (raw.refine !== undefined) plan.refine = parseRefinement(raw.refine)
  if (raw.choose !== undefined) plan.choose = parseChoice(raw.choose)
  for (const key of ['prepare', 'showForApproval'] as const) {
    if (raw[key] === undefined) continue
    if (typeof raw[key] !== 'boolean') reject('The plan is invalid.')
    if (raw[key]) plan[key] = true
  }
  const steps = planStepNames(plan)
  if (steps.length === 0) reject('The plan has no steps.')
  if (steps.length > MAX_PLAN_STEPS) reject('That request has too many steps. Ask for fewer things at once.')
  if (plan.search && plan.refine) reject('A plan either starts a new search or changes the current one.')
  if (plan.prepare && !plan.choose) reject('Say which result to prepare.')
  return plan
}

function parseSelection(value: unknown): VoiceSelection {
  const raw = closedRecord(value, ['ordinal', 'time', 'doctor'], 'selection')
  const selection: VoiceSelection = {}
  if (raw.ordinal !== undefined) {
    const ordinal = raw.ordinal
    if (typeof ordinal !== 'number' || !Number.isSafeInteger(ordinal) || ordinal < 1 || ordinal > MAX_VOICE_ORDINAL) {
      reject('The result number is invalid.')
    }
    selection.ordinal = ordinal
  }
  if (raw.time !== undefined) selection.time = clock(raw.time, 'time')
  if (raw.doctor !== undefined) {
    const doctor = typeof raw.doctor === 'string' ? raw.doctor.normalize('NFKC').trim() : ''
    if (!SELECTION_DOCTOR.test(doctor)) reject('The doctor name is invalid.')
    selection.doctor = doctor
  }
  if (Object.keys(selection).length === 0) reject('Say which result you mean.')
  return selection
}

export function parseVoiceTaskCommand(value: unknown): VoiceTaskCommand {
  const raw = closedRecord(value, ['kind', 'turn', 'constraints', 'changes', 'selection', 'plan', 'query', 'preference'], 'voice command')
  const kind = raw.kind
  const turn = parseTurn(raw.turn)
  const only = (allowed: string[]): void => {
    closedRecord(raw, ['kind', 'turn', ...allowed], 'voice command')
  }
  switch (kind) {
    case 'start_search': {
      only(['constraints'])
      const constraints = parseConstraintFields(closedRecord(raw.constraints, CONSTRAINT_KEYS, 'search'))
      return { kind, turn, constraints }
    }
    case 'refine_search':
      only(['changes'])
      return { kind, turn, changes: parseRefinement(raw.changes) }
    case 'run_plan':
      only(['plan'])
      return { kind, turn, plan: parseTaskPlan(raw.plan) }
    case 'clinic_info':
      only(['query'])
      return { kind, turn, query: parseClinicInfoQuery(raw.query) }
    case 'remember_preference': {
      only(['preference'])
      let preference: PreferenceValue
      try {
        preference = parsePreferenceValue(raw.preference)
      } catch {
        return reject('That preference is not supported.')
      }
      return { kind, turn, preference }
    }
    case 'select_result':
      only(['selection'])
      return { kind, turn, selection: parseSelection(raw.selection) }
    case 'proceed_with_booking':
    case 'task_status':
    case 'check_booking':
    case 'cancel_task':
      only([])
      return { kind, turn }
    default:
      return reject('That voice command is not supported.')
  }
}

// ---- constraints ---------------------------------------------------------------

export function criteriaFromConstraints(constraints: VoiceSearchConstraints, dates?: ResolvedDateWindow): AgentBookingCriteria {
  return mergeCriteria({ specialty: '', day: '' }, constraints, dates)
}

/**
 * Apply spoken changes on top of the task's current durable constraints.
 * A resolved date window replaces the day; a bare weekday (M5 `day`) replaces
 * any date window with a weekday-only constraint.
 */
export function mergeCriteria(current: AgentBookingCriteria, changes: VoiceRefinement, dates?: ResolvedDateWindow): AgentBookingCriteria {
  const next: AgentBookingCriteria = { ...current }
  const clear = new Set<VoiceClearableField>(changes.clear ?? [])
  const clearDates = (): void => {
    delete next.dateFrom
    delete next.dateTo
  }
  if (clear.has('specialty')) next.specialty = ''
  if (clear.has('day')) {
    next.day = ''
    clearDates()
  }
  if (clear.has('time')) {
    delete next.earliestTime
    delete next.latestTime
  }
  if (clear.has('price')) {
    delete next.maxPrice
    delete next.maxPriceCurrency
  }
  if (changes.specialty !== undefined) next.specialty = changes.specialty
  if (changes.day !== undefined) {
    next.day = changes.day
    clearDates()
  }
  if (dates) {
    next.dateFrom = dates.dateFrom
    next.dateTo = dates.dateTo
    next.day = dates.day ?? ''
  }
  if (changes.partOfDay !== undefined) {
    delete next.earliestTime
    delete next.latestTime
    if (changes.partOfDay !== 'any') {
      next.earliestTime = PART_OF_DAY_WINDOWS[changes.partOfDay].earliest
      next.latestTime = PART_OF_DAY_WINDOWS[changes.partOfDay].latest
    }
  }
  if (changes.earliestTime !== undefined) next.earliestTime = changes.earliestTime
  if (changes.latestTime !== undefined) next.latestTime = changes.latestTime
  // "Only after 8 PM" inside an evening window that ends at 22:00 is fine;
  // a bound that crosses the other one means the other one no longer applies.
  if (next.earliestTime && next.latestTime && next.earliestTime > next.latestTime) {
    if (changes.earliestTime !== undefined) delete next.latestTime
    else delete next.earliestTime
  }
  if (changes.maxPriceInr !== undefined) {
    next.maxPrice = changes.maxPriceInr
    next.maxPriceCurrency = VOICE_PRICE_CURRENCY
  }
  return next
}

// ---- durable state → narration ------------------------------------------------

/** HH:MM and weekday as the clinic stated them, from the offset-bearing ISO text. */
function clinicClock(iso: string): { day: AgentBookingDay; time: string } {
  const [year, month, date] = iso.slice(0, 10).split('-').map(Number)
  const weekday = new Date(Date.UTC(year, month - 1, date)).getUTCDay()
  return { day: BOOKING_DAYS[(weekday + 6) % 7], time: iso.slice(11, 16) }
}

/**
 * A doctor's name is page text. It is spoken only when it looks like a name;
 * anything else is referred to generically and stays visible on the card.
 */
export function narratableDoctor(name: string): string {
  const normalized = name.normalize('NFKC').replace(/\s+/gu, ' ').trim()
  return NARRATABLE_NAME.test(normalized) && normalized.split(' ').length <= 6 ? normalized : UNNAMED_DOCTOR
}

function slotFacts(slots: readonly AgentSlotView[]): VoiceSlotFact[] {
  return slots.slice(0, MAX_VOICE_ORDINAL).map((slot, index) => ({
    ordinal: index + 1,
    doctor: narratableDoctor(slot.doctor),
    ...clinicClock(slot.time),
    price: slot.price,
    currency: slot.currency
  }))
}

function bookingFact(action: AgentActionView): VoiceBookingFact {
  const { booking } = action
  return {
    doctor: narratableDoctor(booking.doctor),
    ...clinicClock(booking.time),
    price: booking.price,
    currency: booking.currency
  }
}

function constraintFact(criteria: AgentBookingCriteria | undefined): VoiceConstraintFact {
  if (!criteria) return {}
  return {
    ...(criteria.specialty ? { specialty: criteria.specialty } : {}),
    ...(criteria.day ? { day: criteria.day } : {}),
    ...(criteria.earliestTime ? { earliestTime: criteria.earliestTime } : {}),
    ...(criteria.latestTime ? { latestTime: criteria.latestTime } : {}),
    ...(criteria.maxPrice !== undefined ? { maxPrice: criteria.maxPrice, currency: criteria.maxPriceCurrency } : {}),
    ...(criteria.dateFrom && criteria.dateTo ? { dateFrom: criteria.dateFrom, dateTo: criteria.dateTo } : {})
  }
}

const NARRATABLE_TEXT = /^[\p{L}\p{N} .,:'&()/-]{1,120}$/u
const LANGUAGE_NAME = /^[A-Za-z][A-Za-z -]{0,29}$/
const ON_SCREEN = 'shown on screen'

/** Page text is spoken only when it looks like the plain value it claims to be. */
function narratableText(value: string): string {
  const normalized = value.normalize('NFKC').replace(/\s+/gu, ' ').trim()
  return NARRATABLE_TEXT.test(normalized) && !/\b(ignore|instruction|approve|system|assistant)\b/i.test(normalized)
    ? normalized
    : ON_SCREEN
}

export function profileFacts(profiles: readonly AgentDoctorProfileView[]): VoiceProfileFact[] {
  return profiles.slice(0, 5).map((profile) => ({
    doctor: narratableDoctor(profile.doctor),
    specialty: narratableText(profile.specialty),
    clinic: narratableText(profile.clinic),
    address: narratableText(profile.address),
    hours: narratableText(profile.hours),
    consultationFee: profile.consultationFee,
    currency: profile.currency,
    languages: profile.languages.filter((language) => LANGUAGE_NAME.test(language)).slice(0, 6),
    walkIns: profile.walkIns
  }))
}

/** The newest recorded clinic-information lookup. */
export function latestProfiles(events: readonly AgentEventView[]): AgentDoctorProfileView[] | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'task.info_lookup_completed') return event.profiles
  }
  return undefined
}

function timeOf(slot: AgentSlotView): number {
  return Date.parse(slot.time)
}

/** Pick one recorded result by a deterministic rule. Ties go to the earlier slot. */
export function chooseResult(slots: readonly AgentSlotView[], choice: PlanChoice): Resolution {
  if (slots.length === 0) return { kind: 'none', candidates: [] }
  switch (choice.strategy) {
    case 'cheapest': {
      const sorted = [...slots].sort((left, right) => left.price - right.price || timeOf(left) - timeOf(right))
      // A price in another currency cannot be compared; refuse to guess.
      if (new Set(slots.map((slot) => slot.currency)).size > 1) return { kind: 'many', candidates: sorted }
      return { kind: 'one', slot: sorted[0] }
    }
    case 'earliest':
      return { kind: 'one', slot: [...slots].sort((left, right) => timeOf(left) - timeOf(right))[0] }
    case 'latest':
      return { kind: 'one', slot: [...slots].sort((left, right) => timeOf(right) - timeOf(left))[0] }
    case 'number':
    case 'time':
    case 'doctor':
      return resolveSelection(slots, {
        ...(choice.ordinal !== undefined ? { ordinal: choice.ordinal } : {}),
        ...(choice.time !== undefined ? { time: choice.time } : {}),
        ...(choice.doctor !== undefined ? { doctor: choice.doctor } : {})
      })
  }
}

function describeSlotForMemory(slot: VoiceSlotFact): string {
  return `${slot.doctor} ${slot.day} ${slot.time} ${slot.price} ${slot.currency}`
}

interface RecordedResults {
  slots: AgentSlotView[]
  criteria?: AgentBookingCriteria
}

/** The newest recorded search, unless the constraints changed after it. */
export function latestResults(events: readonly AgentEventView[]): RecordedResults | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'task.criteria_updated') return undefined
    if (event.type === 'task.search_completed') {
      // An unreadable recorded list is treated as no list: search again.
      return event.searchResults ? { slots: event.searchResults, criteria: event.criteria } : undefined
    }
  }
  return undefined
}

/**
 * What voice may say about a page inspection: the host and a closed state.
 * `proceed` is the user saying "yes", "approve" or "go ahead": the answer is
 * always to point at the card, because speech cannot approve.
 */
export function describeInspectionForVoice(snapshot: AgentTaskSnapshot, intent: 'status' | 'proceed'): Described {
  const host = snapshot.task.inspection?.host ?? ''
  const inspection = snapshot.inspection
  const card = (state: VoiceInspectionState): Described => ({ narration: { kind: 'inspection', host, state }, focus: 'approval_card' })
  if (!inspection) return { narration: { kind: 'inspection', host, state: 'no_card' }, focus: 'task' }
  switch (inspection.status) {
    case 'PROPOSED':
    case 'WAITING_APPROVAL':
    // An inspection is authorised by an exact approval, never by a scope, so
    // AUTHORIZED cannot occur here; it is listed so the compiler keeps this
    // switch exhaustive as the ledger grows.
    case 'AUTHORIZED':
      return card(intent === 'proceed' ? 'approval_required' : 'awaiting_approval')
    case 'APPROVED':
      return card(intent === 'proceed' ? 'approval_required' : 'approved_not_opened')
    case 'EXECUTING':
      return { narration: { kind: 'inspection', host, state: 'reading' }, focus: 'task' }
    case 'SUCCEEDED':
      return card(!inspection.answer ? 'read_not_answered' : inspection.answer.status === 'answered' ? 'answered' : 'not_verified')
    case 'FAILED':
      return card('not_read')
    case 'REJECTED':
      return card('rejected')
    case 'OUTCOME_UNKNOWN':
    case 'RECONCILING':
      return card('unknown')
  }
}

function currentBooking(snapshot: AgentTaskSnapshot): AgentActionView | undefined {
  return snapshot.actions.length > 0 ? snapshot.actions[snapshot.actions.length - 1] : undefined
}

function isTerminal(snapshot: AgentTaskSnapshot): boolean {
  return TERMINAL_TASK_STATUSES.includes(snapshot.task.status)
}

function latestVerdict(action: AgentActionView, events: readonly AgentEventView[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'action.reconciled' && event.actionId === action.actionId) return event.reconciliation
  }
  return undefined
}

function approvalExpired(action: AgentActionView, now: number): boolean {
  const expiresAt = action.approval?.expiresAt
  return expiresAt !== undefined && Date.parse(expiresAt) <= now
}

interface Described {
  narration: VoiceNarration
  focus: VoiceTaskFocus
}

/**
 * What durable state says about the current booking. `proceed` is the user
 * trying to go ahead by voice: the answer is always "review and press".
 */
export function describeBooking(action: AgentActionView, events: readonly AgentEventView[], now: number, intent: 'status' | 'proceed'): Described {
  const booking = bookingFact(action)
  const card = (narration: VoiceNarration): Described => ({ narration, focus: 'approval_card' })
  switch (action.status) {
    case 'PROPOSED':
    // A booking is always an exact approval. AUTHORIZED belongs to scoped
    // research steps and cannot appear on a booking action.
    case 'AUTHORIZED':
      return card({ kind: intent === 'proceed' ? 'approval_required' : 'approval_ready', booking })
    case 'WAITING_APPROVAL':
      if (!action.approval || action.approval.status !== 'PENDING' || approvalExpired(action, now)) {
        return card({ kind: 'needs_clarification', reason: 'approval_expired', booking })
      }
      return card({ kind: intent === 'proceed' ? 'approval_required' : 'approval_ready', booking })
    case 'APPROVED':
      if (!action.approval || action.approval.status !== 'APPROVED' || approvalExpired(action, now)) {
        return card({ kind: 'needs_clarification', reason: 'approval_expired', booking })
      }
      return card({ kind: 'approved_not_booked', booking })
    case 'EXECUTING':
      return card({ kind: 'booking_in_progress', booking })
    case 'OUTCOME_UNKNOWN':
      return card({
        kind: 'outcome_unknown',
        booking,
        lastCheckInconclusive: latestVerdict(action, events)?.result === 'OUTCOME_UNKNOWN'
      })
    case 'RECONCILING':
      return card({ kind: 'checking', booking })
    case 'SUCCEEDED': {
      const verdict = latestVerdict(action, events)
      const attempt = action.attempts[action.attempts.length - 1]
      const bookingId = verdict?.result === 'SUCCEEDED'
        ? verdict.bookingId ?? verdict.booking?.bookingId
        : attempt?.result?.receipt?.bookingId ?? attempt?.result?.bookingId
      return card({
        kind: 'booking_confirmed',
        booking,
        ...(bookingId ? { bookingId } : {}),
        confirmedByLookup: verdict?.result === 'SUCCEEDED'
      })
    }
    case 'FAILED': {
      const verdict = latestVerdict(action, events)
      const result = action.attempts[action.attempts.length - 1]?.result
      const reason = verdict?.result === 'FAILED'
        ? 'lookup_found_none'
        : result?.changedFacts?.length
          ? 'changed'
          : result?.dispatchStatus === 'RESOURCE_UNAVAILABLE' ? 'unavailable' : 'failed'
      return card({ kind: 'booking_not_made', booking, reason })
    }
    case 'REJECTED':
      return { narration: { kind: 'booking_not_made', booking, reason: 'rejected' }, focus: 'task' }
  }
}

type Resolution =
  | { kind: 'one'; slot: AgentSlotView }
  | { kind: 'none'; candidates: AgentSlotView[] }
  | { kind: 'many'; candidates: AgentSlotView[] }

function normalizeDoctor(value: string): string {
  return value.normalize('NFKC').toLocaleLowerCase().replace(/^dr\.?\s+/u, '').replace(/\s+/gu, ' ').trim()
}

/** Resolve a spoken reference against the recorded, ordered results only. */
export function resolveSelection(slots: readonly AgentSlotView[], selection: VoiceSelection): Resolution {
  let candidates = slots.map((slot, index) => ({ slot, ordinal: index + 1 }))
  if (selection.ordinal !== undefined) candidates = candidates.filter((entry) => entry.ordinal === selection.ordinal)
  if (selection.time !== undefined) {
    const wanted = selection.time
    const exact = candidates.filter((entry) => clinicClock(entry.slot.time).time === wanted)
    // "the 6:30 one" transcribed as 06:30 when only an evening 18:30 exists.
    const hour = Number(wanted.slice(0, 2))
    const afternoon = hour < 12 ? `${String(hour + 12).padStart(2, '0')}${wanted.slice(2)}` : undefined
    candidates = exact.length > 0 || !afternoon
      ? exact
      : candidates.filter((entry) => clinicClock(entry.slot.time).time === afternoon)
  }
  if (selection.doctor !== undefined) {
    const wanted = normalizeDoctor(selection.doctor)
    candidates = candidates.filter((entry) => normalizeDoctor(entry.slot.doctor) === wanted)
  }
  if (candidates.length === 1) return { kind: 'one', slot: candidates[0].slot }
  if (candidates.length === 0) return { kind: 'none', candidates: [...slots] }
  return { kind: 'many', candidates: candidates.map((entry) => entry.slot) }
}

// ---- controller -------------------------------------------------------------------

const CLARIFY_BY_ERROR: Partial<Record<AgentError['code'], VoiceClarification>> = {
  busy: 'busy',
  no_active_task: 'no_task',
  not_found: 'no_task',
  not_accepting_actions: 'task_closed',
  active_task_unresolved: 'unresolved_booking',
  action_already_open: 'open_booking_exists',
  already_booked: 'already_booked',
  slot_unavailable: 'slot_unavailable',
  criteria_mismatch: 'criteria_mismatch',
  approval_not_usable: 'approval_expired'
}

interface Remembered {
  kind: VoiceCommandKind
  result: AgentResult<VoiceTaskOutcome>
}

/** Where a command came from. Only main decides this, never the caller. */
export type CommandSource = 'voice' | 'text'

export interface VoiceTaskControllerOptions {
  now?: () => number
  /** The clock used only to turn "tomorrow" into a date (defaults to `now`). */
  calendarNow?: () => number
  /** The user's IANA time zone, from trusted main configuration. */
  timeZone?: () => string
  memory?: PreferenceMemory
  diagnostics?: DiagnosticsSink
}

interface PreparedConstraints {
  criteria: AgentBookingCriteria
  appliedPreferences: PreferenceKey[]
}

type ConstraintResult = PreparedConstraints | { clarification: VoiceNarration }

export class VoiceTaskController {
  private queue: Promise<unknown> = Promise.resolve()
  private readonly turns = new Map<string, Remembered>()
  private readonly now: () => number
  private readonly calendarNow: () => number
  private readonly timeZone: () => string
  private readonly memory?: PreferenceMemory
  private readonly diagnostics: DiagnosticsSink

  constructor(
    private readonly tasks: VoiceTaskBackend,
    clock: (() => number) | VoiceTaskControllerOptions = {}
  ) {
    const options: VoiceTaskControllerOptions = typeof clock === 'function' ? { now: clock } : clock
    this.now = options.now ?? (() => Date.now())
    this.calendarNow = options.calendarNow ?? this.now
    this.timeZone = options.timeZone ?? (() => Intl.DateTimeFormat().resolvedOptions().timeZone)
    this.memory = options.memory
    this.diagnostics = options.diagnostics ?? NO_DIAGNOSTICS
  }

  /**
   * The IPC entry point (voice) and the typed-request entry point (text).
   * Validates, then runs one command at a time. `source` is set by main.
   */
  async handle(value: unknown, source: CommandSource = 'voice'): Promise<AgentResult<VoiceTaskOutcome>> {
    let command: VoiceTaskCommand
    try {
      command = parseVoiceTaskCommand(value)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    const run = this.queue.then(() => this.dispatch(command, source))
    this.queue = run.catch(() => undefined)
    return run
  }

  private async dispatch(command: VoiceTaskCommand, source: CommandSource): Promise<AgentResult<VoiceTaskOutcome>> {
    const started = this.now()
    const result = await this.dispatchOnce(command, source)
    this.diagnostics.record({
      kind: 'plan',
      command: command.kind,
      latencyMs: Math.max(0, this.now() - started),
      ...(result.ok && result.value.taskId ? { taskId: result.value.taskId } : {}),
      result: result.ok ? `${result.value.replayed ? 'replayed:' : ''}${result.value.narration.kind}` : result.error.code
    })
    return result
  }

  private async dispatchOnce(command: VoiceTaskCommand, source: CommandSource): Promise<AgentResult<VoiceTaskOutcome>> {
    const progressing = PROGRESSING_VOICE_COMMANDS.includes(command.kind)
    // One durable step per completed utterance; reads may repeat freely but
    // are still answered once per turn so a replayed event stays silent work.
    const key = progressing ? `turn:${command.turn.turnId}` : `${command.kind}:${command.turn.turnId}`
    const previous = this.turns.get(key)
    if (previous) {
      if (previous.kind !== command.kind) {
        return { ok: true, value: this.outcome(command.kind, undefined, 'none', { kind: 'needs_clarification', reason: 'one_step_per_request' }) }
      }
      return previous.result.ok ? { ok: true, value: { ...previous.result.value, replayed: true } } : previous.result
    }
    let result: AgentResult<VoiceTaskOutcome>
    try {
      result = { ok: true, value: await this.execute(command, source) }
    } catch (error) {
      const agentError = toAgentError(error)
      const reason = CLARIFY_BY_ERROR[agentError.code]
      result = {
        ok: true,
        value: this.outcome(
          command.kind,
          undefined,
          reason === 'unresolved_booking' || reason === 'open_booking_exists' ? 'approval_card' : 'task',
          reason ? { kind: 'needs_clarification', reason } : { kind: 'refused', code: agentError.code }
        )
      }
    }
    // Every handled turn is remembered, including refusals: a turn whose
    // create or prepare was not confirmed must never run a second time.
    this.turns.set(key, { kind: command.kind, result })
    while (this.turns.size > MAX_REMEMBERED_TURNS) {
      const oldest = this.turns.keys().next().value
      if (oldest === undefined) break
      this.turns.delete(oldest)
    }
    return result
  }

  private outcome(kind: VoiceCommandKind, snapshot: AgentTaskSnapshot | null | undefined, focus: VoiceTaskFocus, narration: VoiceNarration, replayed = false): VoiceTaskOutcome {
    return {
      kind,
      ...(snapshot ? { taskId: snapshot.task.taskId, taskStatus: snapshot.task.status, taskKind: snapshot.task.kind } : {}),
      focus,
      narration,
      replayed
    }
  }

  private async load(): Promise<AgentTaskSnapshot | null> {
    return unwrap(await this.tasks.loadActiveTask(0))
  }

  private async openTask(kind: VoiceCommandKind, taskKind: AgentTaskSnapshot['task']['kind'] = 'appointment_booking'): Promise<AgentTaskSnapshot | VoiceTaskOutcome> {
    const snapshot = await this.load()
    if (!snapshot) return this.outcome(kind, null, 'task', { kind: 'needs_clarification', reason: 'no_task' })
    if (snapshot.task.kind !== taskKind) {
      return this.outcome(kind, snapshot, 'task', { kind: 'needs_clarification', reason: 'wrong_task_kind' })
    }
    if (isTerminal(snapshot)) {
      return this.outcome(kind, snapshot, 'task', snapshot.task.status === 'CANCELLED'
        ? { kind: 'task_cancelled', rejectedBooking: false }
        : { kind: 'needs_clarification', reason: 'task_closed' })
    }
    return snapshot
  }

  private execute(command: VoiceTaskCommand, source: CommandSource): Promise<VoiceTaskOutcome> {
    switch (command.kind) {
      case 'start_search': return this.startSearch(command.turn, command.constraints, source)
      case 'refine_search': return this.refineSearch(command.changes)
      case 'select_result': return this.selectResult(command.selection)
      case 'proceed_with_booking': return this.proceed()
      case 'task_status': return this.status()
      case 'check_booking': return this.checkBooking()
      case 'cancel_task': return this.cancel()
      case 'run_plan': return this.runPlan(command.turn, command.plan, source)
      case 'clinic_info': return this.clinicInfo(command.turn, command.query, source)
      case 'remember_preference': return this.rememberPreference(command.turn, command.preference)
    }
  }

  // ---- constraints: dates and preferences ---------------------------------------

  private resolveDates(constraints: VoiceSearchConstraints): ResolvedDateWindow | { clarification: VoiceNarration } | undefined {
    if (!constraints.when) return undefined
    const resolved = resolveRelativeDay(constraints.when, new Date(this.calendarNow()), this.timeZone())
    if (resolved.kind === 'resolved') return resolved
    if (resolved.kind === 'ambiguous') {
      const dateOptions: VoiceDateOption[] = resolved.candidates.map((option) => ({ ...option }))
      return { clarification: { kind: 'needs_clarification', reason: 'date_ambiguous', dateOptions } }
    }
    return { clarification: { kind: 'needs_clarification', reason: 'date_invalid' } }
  }

  /**
   * Criteria for a *new* search. What the user just said always wins; a
   * remembered preference only fills a constraint the request left open.
   */
  private async newSearchCriteria(constraints: VoiceSearchConstraints): Promise<ConstraintResult> {
    const dates = this.resolveDates(constraints)
    if (dates && 'clarification' in dates) return dates
    const effective: VoiceSearchConstraints = { ...constraints }
    delete effective.when
    const appliedPreferences: PreferenceKey[] = []
    const preferences = this.memory ? await this.memory.preferences().catch(() => []) : []
    for (const preference of preferences) {
      if (preference.key === 'preferred_part_of_day' &&
          effective.partOfDay === undefined && effective.earliestTime === undefined && effective.latestTime === undefined) {
        effective.partOfDay = preference.value
        appliedPreferences.push(preference.key)
      }
      if (preference.key === 'max_price_inr' && effective.maxPriceInr === undefined) {
        effective.maxPriceInr = preference.value
        appliedPreferences.push(preference.key)
      }
    }
    return { criteria: criteriaFromConstraints(effective, dates), appliedPreferences }
  }

  private async recordEpisode(snapshot: AgentTaskSnapshot | null, kind: 'booking_search' | 'booking_outcome' | 'clinic_info', summary: string): Promise<void> {
    if (!this.memory || !snapshot) return
    try {
      await this.memory.recordEpisode({
        taskId: snapshot.task.taskId, kind, summary, sequence: snapshot.task.lastEventSequence,
        // Milestone 8a S3: nothing read through a signed-in account is summarised.
        classification: snapshot.task.kind === 'authenticated_read' ? 'account_private' : 'public'
      })
    } catch {
      // Memory is a convenience; the durable timeline is the record.
    }
  }

  private resultsOutcome(
    kind: VoiceCommandKind,
    snapshot: AgentTaskSnapshot,
    invalidatedBooking: boolean,
    replayed = false,
    appliedPreferences: PreferenceKey[] = []
  ): VoiceTaskOutcome {
    const results = latestResults(snapshot.events)
    if (!results) return this.outcome(kind, snapshot, 'task', { kind: 'needs_clarification', reason: 'no_results_yet' }, replayed)
    return this.outcome(kind, snapshot, 'task', {
      kind: 'results',
      constraints: constraintFact(results.criteria ?? snapshot.task.criteria),
      slots: slotFacts(results.slots),
      totalCount: results.slots.length,
      invalidatedBooking,
      ...(appliedPreferences.length > 0 ? { appliedPreferences } : {})
    }, replayed)
  }

  /** Search (read-only), then narrate the durable record the search wrote. */
  private async searchAndDescribe(kind: VoiceCommandKind, invalidatedBooking: boolean, appliedPreferences: PreferenceKey[] = []): Promise<VoiceTaskOutcome> {
    unwrap(await this.tasks.searchAppointments())
    const snapshot = await this.load()
    if (!snapshot) return this.outcome(kind, null, 'task', { kind: 'needs_clarification', reason: 'no_task' })
    const outcome = this.resultsOutcome(kind, snapshot, invalidatedBooking, false, appliedPreferences)
    if (outcome.narration.kind === 'results') {
      const { constraints, slots, totalCount } = outcome.narration
      const when = constraints.dateFrom ? `${constraints.dateFrom}${constraints.dateTo !== constraints.dateFrom ? ` to ${constraints.dateTo}` : ''}` : constraints.day ?? 'any day'
      void this.recordEpisode(snapshot, 'booking_search',
        `Searched ${constraints.specialty ?? 'any specialty'} ${when}: ${totalCount} results. ${slots.slice(0, 3).map(describeSlotForMemory).join('; ')}`)
    }
    return outcome
  }

  /** Whether the active task was already created by this exact turn or request. */
  private createdBy(snapshot: AgentTaskSnapshot | null, turn: VoiceTurn): boolean {
    return snapshot !== null && (snapshot.task.voiceTurnId === turn.turnId || snapshot.task.requestId === turn.turnId)
  }

  private async startSearch(turn: VoiceTurn, constraints: VoiceSearchConstraints, source: CommandSource = 'voice'): Promise<VoiceTaskOutcome> {
    const active = await this.load()
    if (active && this.createdBy(active, turn)) {
      // This utterance already created the active task (for example before a
      // main-process restart). Never create a second one for it.
      return active.task.kind === 'appointment_booking'
        ? this.resultsOutcome('start_search', active, false, true)
        : this.outcome('start_search', active, 'task', { kind: 'needs_clarification', reason: 'wrong_task_kind' }, true)
    }
    if (active && !isTerminal(active) && active.task.kind === 'appointment_booking') {
      const booking = currentBooking(active)
      if (booking && UNRESOLVED_ACTION_STATUSES.includes(booking.status)) {
        return this.outcome('start_search', active, 'approval_card', { kind: 'needs_clarification', reason: 'unresolved_booking', booking: bookingFact(booking) })
      }
      if (booking && OPEN_ACTION_STATUSES.includes(booking.status)) {
        return this.outcome('start_search', active, 'approval_card', { kind: 'needs_clarification', reason: 'open_booking_exists', booking: bookingFact(booking) })
      }
    }
    const prepared = await this.newSearchCriteria(constraints)
    if ('clarification' in prepared) return this.outcome('start_search', active, 'none', prepared.clarification)
    unwrap(await this.tasks.createBookingTask(prepared.criteria, {
      source, turnId: turn.turnId, utterance: turn.utterance
    }))
    return this.searchAndDescribe('start_search', false, prepared.appliedPreferences)
  }

  private async refineSearch(changes: VoiceRefinement): Promise<VoiceTaskOutcome> {
    const snapshot = await this.openTask('refine_search')
    if (!('task' in snapshot)) return snapshot
    const dates = this.resolveDates(changes)
    if (dates && 'clarification' in dates) return this.outcome('refine_search', snapshot, 'none', dates.clarification)
    const effective: VoiceRefinement = { ...changes }
    delete effective.when
    const merged = mergeCriteria(snapshot.task.criteria, effective, dates)
    const revision = unwrap(await this.tasks.reviseCriteria(merged, snapshot.task.revision))
    return this.searchAndDescribe('refine_search', revision.invalidatedActionIds.length > 0)
  }

  /**
   * The recorded results a selection may point at, searching once (read-only)
   * when nothing current is recorded. Refuses while a booking is unresolved or
   * already confirmed.
   */
  private async selectableResults(kind: VoiceCommandKind): Promise<{ snapshot: AgentTaskSnapshot; slots: AgentSlotView[] } | VoiceTaskOutcome> {
    let snapshot = await this.openTask(kind)
    if (!('task' in snapshot)) return snapshot
    const existing = currentBooking(snapshot)
    if (existing && UNRESOLVED_ACTION_STATUSES.includes(existing.status)) {
      return this.outcome(kind, snapshot, 'approval_card', { kind: 'needs_clarification', reason: 'unresolved_booking', booking: bookingFact(existing) })
    }
    if (existing?.status === 'SUCCEEDED') {
      return this.outcome(kind, snapshot, 'approval_card', { kind: 'needs_clarification', reason: 'already_booked', booking: bookingFact(existing) })
    }
    let results = latestResults(snapshot.events)
    if (!results) {
      unwrap(await this.tasks.searchAppointments())
      const refreshed = await this.load()
      if (!refreshed) return this.outcome(kind, null, 'task', { kind: 'needs_clarification', reason: 'no_task' })
      snapshot = refreshed
      results = latestResults(snapshot.events)
    }
    if (!results || results.slots.length === 0) {
      return this.outcome(kind, snapshot, 'task', { kind: 'needs_clarification', reason: 'no_results_yet' })
    }
    return { snapshot, slots: results.slots }
  }

  /** Prepare exactly one resolved slot, replacing an unexecuted different choice. */
  private async prepareChosen(kind: VoiceCommandKind, snapshot: AgentTaskSnapshot, slot: AgentSlotView): Promise<VoiceTaskOutcome> {
    const existing = currentBooking(snapshot)
    if (existing && OPEN_ACTION_STATUSES.includes(existing.status)) {
      const fresh = existing.status === 'WAITING_APPROVAL' && existing.approval?.status === 'PENDING' && !approvalExpired(existing, this.now())
      if (existing.booking.slotId === slot.slotId && fresh) {
        return this.outcome(kind, snapshot, 'approval_card', { kind: 'approval_ready', booking: bookingFact(existing) })
      }
      // A different choice replaces the unexecuted one. Rejecting can only
      // prevent a booking; it never makes one.
      unwrap(await this.tasks.rejectAction(existing.actionId, existing.revision))
    }
    const action = unwrap(await this.tasks.prepareBooking(slot.slotId))
    const after = await this.load()
    return this.outcome(kind, after, 'approval_card', describeBooking(action, after?.events ?? [], this.now(), 'status').narration)
  }

  private async selectResult(selection: VoiceSelection): Promise<VoiceTaskOutcome> {
    const selectable = await this.selectableResults('select_result')
    if (!('slots' in selectable)) return selectable
    const { snapshot } = selectable
    const resolution = resolveSelection(selectable.slots, selection)
    if (resolution.kind !== 'one') {
      return this.outcome('select_result', snapshot, 'task', {
        kind: 'needs_clarification',
        reason: resolution.kind === 'many' ? 'ambiguous_selection' : 'no_matching_result',
        candidates: slotFacts(resolution.candidates)
      })
    }
    return this.prepareChosen('select_result', snapshot, resolution.slot)
  }

  /**
   * A bounded compound request. Each step runs only if the one before it
   * produced what it needs, and the plan always stops before approval: the
   * last thing it can do is surface the trusted card.
   */
  private async runPlan(turn: VoiceTurn, plan: TaskPlan, source: CommandSource): Promise<VoiceTaskOutcome> {
    const names = planStepNames(plan)
    const status = new Map<PlanStepName, PlanStepReport['status']>(names.map((name) => [name, 'not_run']))
    const finish = (outcome: VoiceTaskOutcome, stoppedAt?: PlanStepName): VoiceTaskOutcome => {
      if (stoppedAt) status.set(stoppedAt, 'stopped')
      return { ...outcome, kind: 'run_plan', plan: names.map((step) => ({ step, status: status.get(step) ?? 'not_run' })) }
    }
    let last: VoiceTaskOutcome | undefined
    if (plan.search) {
      last = await this.startSearch(turn, plan.search, source)
      if (last.narration.kind !== 'results' || last.replayed) return finish(last, last.replayed ? undefined : 'search')
      status.set('search', 'done')
      if (last.narration.totalCount === 0 && plan.choose) return finish(last, 'choose')
    }
    if (plan.refine) {
      last = await this.refineSearch(plan.refine)
      if (last.narration.kind !== 'results') return finish(last, 'refine')
      status.set('refine', 'done')
      if (last.narration.totalCount === 0 && plan.choose) return finish(last, 'choose')
    }
    let chosen: { snapshot: AgentTaskSnapshot; slot: AgentSlotView } | undefined
    if (plan.choose) {
      const selectable = await this.selectableResults('run_plan')
      if (!('slots' in selectable)) return finish(selectable, 'choose')
      const resolution = chooseResult(selectable.slots, plan.choose)
      if (resolution.kind !== 'one') {
        return finish(this.outcome('run_plan', selectable.snapshot, 'task', {
          kind: 'needs_clarification',
          reason: resolution.kind === 'many' ? 'ambiguous_selection' : 'no_matching_result',
          candidates: slotFacts(resolution.candidates)
        }), 'choose')
      }
      chosen = { snapshot: selectable.snapshot, slot: resolution.slot }
      status.set('choose', 'done')
      const index = selectable.slots.findIndex((slot) => slot.slotId === resolution.slot.slotId)
      const fact = slotFacts(selectable.slots)[index] ?? slotFacts([resolution.slot])[0]
      last = this.outcome('run_plan', selectable.snapshot, 'task', {
        kind: 'chosen', slot: fact, strategy: plan.choose.strategy, totalCount: selectable.slots.length
      })
    }
    if (plan.prepare && chosen) {
      last = await this.prepareChosen('run_plan', chosen.snapshot, chosen.slot)
      if (last.narration.kind !== 'approval_ready') return finish(last, 'prepare')
      status.set('prepare', 'done')
    }
    if (plan.showForApproval) {
      // Surfaces the trusted card. It never approves: speech has no approval.
      last = await this.proceed()
      const surfaced = last.narration.kind === 'approval_required' ||
        (last.narration.kind === 'inspection' && last.narration.state === 'approval_required')
      status.set('show_for_approval', surfaced ? 'done' : 'stopped')
    }
    return finish(last ?? this.outcome('run_plan', null, 'none', { kind: 'needs_clarification', reason: 'not_understood' }))
  }

  private async clinicInfo(turn: VoiceTurn, query: AgentClinicInfoQuery, source: CommandSource): Promise<VoiceTaskOutcome> {
    const active = await this.load()
    if (active && this.createdBy(active, turn)) {
      const profiles = latestProfiles(active.events)
      return this.outcome('clinic_info', active, 'task', profiles
        ? { kind: 'clinic_info', topic: active.task.infoQuery?.topic ?? query.topic, profiles: profileFacts(profiles) }
        : { kind: 'needs_clarification', reason: 'no_results_yet' }, true)
    }
    if (active && !isTerminal(active)) {
      const booking = currentBooking(active)
      if (booking && UNRESOLVED_ACTION_STATUSES.includes(booking.status)) {
        return this.outcome('clinic_info', active, 'approval_card', { kind: 'needs_clarification', reason: 'unresolved_booking', booking: bookingFact(booking) })
      }
      if (booking && OPEN_ACTION_STATUSES.includes(booking.status)) {
        return this.outcome('clinic_info', active, 'approval_card', { kind: 'needs_clarification', reason: 'open_booking_exists', booking: bookingFact(booking) })
      }
    }
    unwrap(await this.tasks.createClinicInfoTask(query, { source, turnId: turn.turnId, utterance: turn.utterance }))
    const profiles = unwrap(await this.tasks.lookupClinicInfo())
    const after = await this.load()
    const facts = profileFacts(profiles)
    void this.recordEpisode(after, 'clinic_info',
      `Clinic information (${query.topic}) for ${query.doctor || query.specialty}: ${facts.map((fact) => `${fact.doctor} fee ${fact.consultationFee} ${fact.currency}`).join('; ') || 'no profiles'}`)
    return this.outcome('clinic_info', after, 'task', { kind: 'clinic_info', topic: query.topic, profiles: facts })
  }

  private async rememberPreference(turn: VoiceTurn, preference: PreferenceValue): Promise<VoiceTaskOutcome> {
    if (!this.memory) return this.outcome('remember_preference', null, 'none', { kind: 'refused', code: 'request_failed' })
    const saved = await this.memory.remember(preference, turn.turnId)
    return this.outcome('remember_preference', null, 'none', {
      kind: 'preference_saved', preference: { key: saved.key, value: saved.value } as PreferenceValue
    })
  }

  private async proceed(): Promise<VoiceTaskOutcome> {
    const snapshot = await this.load()
    if (!snapshot) return this.outcome('proceed_with_booking', null, 'task', { kind: 'needs_clarification', reason: 'no_task' })
    if (snapshot.task.kind === 'page_inspection') {
      // Never approves and never opens the page: it only points at the card.
      const described = describeInspectionForVoice(snapshot, 'proceed')
      return this.outcome('proceed_with_booking', snapshot, described.focus, described.narration)
    }
    const booking = currentBooking(snapshot)
    if (!booking || ((booking.status === 'REJECTED' || booking.status === 'FAILED') && !isTerminal(snapshot))) {
      return this.outcome('proceed_with_booking', snapshot, 'task', { kind: 'needs_clarification', reason: 'nothing_prepared' })
    }
    if (booking.status === 'PROPOSED' && !isTerminal(snapshot)) {
      // Opening the approval request grants nothing; it makes the card reviewable.
      const requested = unwrap(await this.tasks.requestApproval(booking.actionId, booking.revision))
      return this.outcome('proceed_with_booking', snapshot, 'approval_card', { kind: 'approval_required', booking: bookingFact(requested) })
    }
    const described = describeBooking(booking, snapshot.events, this.now(), 'proceed')
    return this.outcome('proceed_with_booking', snapshot, described.focus, described.narration)
  }

  private async status(): Promise<VoiceTaskOutcome> {
    const snapshot = await this.load()
    if (!snapshot) return this.outcome('task_status', null, 'none', { kind: 'needs_clarification', reason: 'no_task' })
    if (snapshot.task.status === 'CANCELLED') {
      return this.outcome('task_status', snapshot, 'task', { kind: 'task_cancelled', rejectedBooking: false })
    }
    if (snapshot.task.kind === 'page_inspection') {
      const described = describeInspectionForVoice(snapshot, 'status')
      return this.outcome('task_status', snapshot, described.focus, described.narration)
    }
    if (snapshot.task.kind === 'clinic_info') {
      const profiles = latestProfiles(snapshot.events)
      return this.outcome('task_status', snapshot, 'task', profiles && snapshot.task.infoQuery
        ? { kind: 'clinic_info', topic: snapshot.task.infoQuery.topic, profiles: profileFacts(profiles) }
        : { kind: 'needs_clarification', reason: 'no_results_yet' })
    }
    const booking = currentBooking(snapshot)
    if (booking && !(booking.status === 'REJECTED' && latestResults(snapshot.events))) {
      const described = describeBooking(booking, snapshot.events, this.now(), 'status')
      return this.outcome('task_status', snapshot, described.focus, described.narration)
    }
    if (latestResults(snapshot.events)) return this.resultsOutcome('task_status', snapshot, false)
    return this.outcome('task_status', snapshot, 'task', {
      kind: 'task_open', constraints: constraintFact(snapshot.task.criteria), taskStatus: snapshot.task.status
    })
  }

  private async checkBooking(): Promise<VoiceTaskOutcome> {
    const snapshot = await this.load()
    if (!snapshot) return this.outcome('check_booking', null, 'none', { kind: 'needs_clarification', reason: 'no_task' })
    const booking = currentBooking(snapshot)
    if (!booking) return this.outcome('check_booking', snapshot, 'task', { kind: 'needs_clarification', reason: 'nothing_prepared' })
    if (booking.status !== 'OUTCOME_UNKNOWN' && booking.status !== 'RECONCILING') {
      const described = describeBooking(booking, snapshot.events, this.now(), 'status')
      return this.outcome('check_booking', snapshot, described.focus, described.narration)
    }
    // The existing read-only lookup. It never submits and never retries.
    const checked = unwrap(await this.tasks.reconcileAction(booking.actionId, booking.revision))
    const after = await this.load()
    const described = describeBooking(checked, after?.events ?? snapshot.events, this.now(), 'status')
    return this.outcome('check_booking', after ?? snapshot, described.focus, described.narration)
  }

  private async cancel(): Promise<VoiceTaskOutcome> {
    const snapshot = await this.openTask('cancel_task')
    if (!('task' in snapshot)) return snapshot
    const cancellation = unwrap(await this.tasks.cancelActiveTask(snapshot.task.revision))
    const after = await this.load()
    return this.outcome('cancel_task', after, 'task', {
      kind: 'task_cancelled',
      rejectedBooking: cancellation.rejectedActionIds.length > 0
    })
  }
}

function unwrap<T>(result: AgentResult<T>): T {
  if (!result.ok) throw new AgentRequestError(result.error)
  return result.value
}
