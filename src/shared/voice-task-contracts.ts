import type {
  AgentBookingDay,
  AgentClinicInfoQuery,
  AgentClinicInfoTopic,
  AgentErrorCode,
  AgentTaskKind,
  AgentTaskStatus
} from './agent-contracts'
import type { PreferenceKey, PreferenceValue } from './model-contracts'
import type { AgentOrchestrationPauseReason, AgentOrchestrationStatus } from './orchestration-contracts'
import type { RelativeDayPhrase } from './relative-dates'

/**
 * Voice → durable task controller contract.
 *
 * A realtime model turns what the user said (in any language it supports)
 * into one of these closed, language-neutral commands. Electron main decides
 * whether the command is legal in the current durable state and runs it
 * through the same M4 task controller the booking panel uses.
 *
 * What is deliberately absent: approving, executing, retrying, choosing a URL,
 * a selector, a price, or any other booked value. "Book it" / "yes" is
 * `proceed_with_booking`, which can only surface the trusted approval card.
 *
 * Every value that describes a booking in an outcome comes from durable,
 * browser-observed task state, never from the model or the renderer.
 */

/** Canonical specialties the voice layer may ask for. The model maps
 * "dermatologist", "skin doctor", "చర్మ వైద్యుడు", ... onto these. */
export const VOICE_SPECIALTIES = [
  'Dermatology', 'Dentistry', 'Cardiology', 'Pediatrics', 'General Medicine',
  'Orthopedics', 'Gynecology', 'Ophthalmology', 'ENT', 'Psychiatry'
] as const
export type VoiceSpecialty = typeof VOICE_SPECIALTIES[number]

export const VOICE_PARTS_OF_DAY = ['morning', 'afternoon', 'evening', 'any'] as const
export type VoicePartOfDay = typeof VOICE_PARTS_OF_DAY[number]

/** Clinic-local wall-clock windows for a spoken part of day. */
export const PART_OF_DAY_WINDOWS: Record<Exclude<VoicePartOfDay, 'any'>, { earliest: string; latest: string }> = {
  morning: { earliest: '06:00', latest: '11:59' },
  afternoon: { earliest: '12:00', latest: '16:59' },
  evening: { earliest: '17:00', latest: '22:00' }
}

/** A voice price ceiling is always in rupees; the fixture and model speak INR. */
export const VOICE_PRICE_CURRENCY = 'INR'
export const MAX_VOICE_PRICE = 1_000_000
export const MAX_VOICE_ORDINAL = 10

export const VOICE_COMMAND_KINDS = [
  'start_search', 'refine_search', 'select_result', 'proceed_with_booking',
  'task_status', 'check_booking', 'cancel_task',
  'run_plan', 'clinic_info', 'remember_preference'
] as const
export type VoiceCommandKind = typeof VOICE_COMMAND_KINDS[number]

/** The realtime tool names for the voice task commands. */
export const VOICE_TASK_TOOLS = {
  search: 'appointment_search',
  refine: 'appointment_refine',
  select: 'appointment_select',
  status: 'appointment_status',
  showForApproval: 'appointment_show_booking_for_approval',
  check: 'appointment_check_booking',
  cancel: 'appointment_cancel_task',
  plan: 'appointment_plan',
  clinicInfo: 'clinic_info_lookup',
  rememberPreference: 'remember_preference'
} as const
export type VoiceTaskToolName = typeof VOICE_TASK_TOOLS[keyof typeof VOICE_TASK_TOOLS]

/** Commands that change durable state or start browser work. One per turn. */
export const PROGRESSING_VOICE_COMMANDS: readonly VoiceCommandKind[] = [
  'start_search', 'refine_search', 'select_result', 'check_booking', 'cancel_task',
  'run_plan', 'clinic_info', 'remember_preference'
]

/** The completed user utterance a command is bound to. */
export interface VoiceTurn {
  /** Realtime conversation item id of the completed user turn, or a typed request id. */
  turnId: string
  /** The completed transcript (or typed text), kept for traceability only. */
  utterance: string
}

export interface VoiceSearchConstraints {
  specialty?: VoiceSpecialty
  day?: AgentBookingDay
  partOfDay?: VoicePartOfDay
  /** 24-hour HH:MM, clinic-local. Overrides the part-of-day bound. */
  earliestTime?: string
  latestTime?: string
  maxPriceInr?: number
  /**
   * The kind of day the user said ("tomorrow", "next Saturday"). Main resolves
   * it into calendar dates with the trusted clock and time zone.
   */
  when?: RelativeDayPhrase
}

export const VOICE_CLEARABLE_FIELDS = ['specialty', 'day', 'time', 'price'] as const
export type VoiceClearableField = typeof VOICE_CLEARABLE_FIELDS[number]

export interface VoiceRefinement extends VoiceSearchConstraints {
  /** Constraints the user dropped ("any price", "any day"). */
  clear?: VoiceClearableField[]
}

/** How the user pointed at a result. Resolved only against recorded results. */
export interface VoiceSelection {
  /** 1-based position in the most recently presented results. */
  ordinal?: number
  /** 24-hour HH:MM clinic-local time the user said. */
  time?: string
  doctor?: string
}

/** How a compound request picks one of the recorded results. */
export const PLAN_CHOICE_STRATEGIES = ['cheapest', 'earliest', 'latest', 'number', 'time', 'doctor'] as const
export type PlanChoiceStrategy = typeof PLAN_CHOICE_STRATEGIES[number]

export interface PlanChoice {
  strategy: PlanChoiceStrategy
  ordinal?: number
  time?: string
  doctor?: string
}

/**
 * A bounded compound request. Steps always run in this order and each is
 * optional: search *or* refine, then choose, then prepare, then show the
 * approval card. There is no approve or execute step, and there is no way to
 * express one: "book it" at the end of a sentence only surfaces the card.
 */
export interface TaskPlan {
  search?: VoiceSearchConstraints
  refine?: VoiceRefinement
  choose?: PlanChoice
  prepare?: boolean
  showForApproval?: boolean
}

export const PLAN_STEP_NAMES = ['search', 'refine', 'choose', 'prepare', 'show_for_approval'] as const
export type PlanStepName = typeof PLAN_STEP_NAMES[number]
/** The most durable steps one utterance may run. */
export const MAX_PLAN_STEPS = 4

export interface PlanStepReport {
  step: PlanStepName
  status: 'done' | 'stopped' | 'not_run'
}

export type VoiceTaskCommand =
  | { kind: 'start_search'; turn: VoiceTurn; constraints: VoiceSearchConstraints }
  | { kind: 'refine_search'; turn: VoiceTurn; changes: VoiceRefinement }
  | { kind: 'select_result'; turn: VoiceTurn; selection: VoiceSelection }
  | { kind: 'proceed_with_booking'; turn: VoiceTurn }
  | { kind: 'task_status'; turn: VoiceTurn }
  | { kind: 'check_booking'; turn: VoiceTurn }
  | { kind: 'cancel_task'; turn: VoiceTurn }
  | { kind: 'run_plan'; turn: VoiceTurn; plan: TaskPlan }
  | { kind: 'clinic_info'; turn: VoiceTurn; query: AgentClinicInfoQuery }
  | { kind: 'remember_preference'; turn: VoiceTurn; preference: PreferenceValue }

/** Where the trusted UI should draw the user's attention. */
export type VoiceTaskFocus = 'none' | 'task' | 'approval_card'

export interface VoiceSlotFact {
  ordinal: number
  doctor: string
  day: AgentBookingDay
  /** HH:MM in the clinic's own offset. */
  time: string
  price: number
  currency: string
}

export interface VoiceBookingFact {
  doctor: string
  day: AgentBookingDay
  time: string
  price: number
  currency: string
}

export interface VoiceConstraintFact {
  specialty?: string
  day?: AgentBookingDay
  earliestTime?: string
  latestTime?: string
  maxPrice?: number
  currency?: string
  dateFrom?: string
  dateTo?: string
}

/** Typed public doctor facts a voice answer may speak. */
export interface VoiceProfileFact {
  doctor: string
  specialty: string
  clinic: string
  address: string
  hours: string
  consultationFee: number
  currency: string
  languages: string[]
  walkIns: boolean
}

export interface VoiceDateOption {
  dateFrom: string
  dateTo: string
  day?: AgentBookingDay
}

export const VOICE_INSPECTION_STATES = [
  /** A card is waiting for the trusted click. */
  'awaiting_approval',
  /** The user tried to approve by voice or text. Nothing was approved. */
  'approval_required',
  'approved_not_opened',
  'reading',
  'answered',
  'not_verified',
  'read_not_answered',
  'not_read',
  'unknown',
  'rejected',
  'no_card'
] as const
export type VoiceInspectionState = typeof VOICE_INSPECTION_STATES[number]

/**
 * Milestone 7b. What voice may say about a public-research task: a closed
 * state and nothing from any page. Speech can create the task and point at
 * the permission card; it can never grant the scope.
 */
export const VOICE_RESEARCH_STATES = [
  'awaiting_permission',
  'permission_required',
  'researching',
  'answered',
  'not_verified',
  'stopped',
  'no_card'
] as const
export type VoiceResearchState = typeof VOICE_RESEARCH_STATES[number]

export const VOICE_CLARIFICATIONS = [
  'no_task',
  'task_closed',
  'no_results_yet',
  'no_matching_result',
  'ambiguous_selection',
  'nothing_prepared',
  'open_booking_exists',
  'unresolved_booking',
  'already_booked',
  'one_step_per_request',
  'slot_unavailable',
  'criteria_mismatch',
  'approval_expired',
  'busy',
  'date_ambiguous',
  'date_invalid',
  'too_many_steps',
  'wrong_task_kind',
  'not_understood'
] as const
export type VoiceClarification = typeof VOICE_CLARIFICATIONS[number]

export type VoiceNarration =
  | {
    kind: 'results'
    constraints: VoiceConstraintFact
    slots: VoiceSlotFact[]
    totalCount: number
    invalidatedBooking: boolean
    /** Constraints filled from remembered preferences, not from this request. */
    appliedPreferences?: PreferenceKey[]
  }
  /** One recorded result picked by rule ("the cheapest"); nothing was prepared. */
  | { kind: 'chosen'; slot: VoiceSlotFact; strategy: PlanChoiceStrategy; totalCount: number }
  /** A booking is prepared from observed facts and waits for the trusted click. */
  | { kind: 'approval_ready'; booking: VoiceBookingFact }
  /** The user tried to approve by voice. Nothing was approved. */
  | { kind: 'approval_required'; booking: VoiceBookingFact }
  | { kind: 'approved_not_booked'; booking: VoiceBookingFact }
  | { kind: 'booking_in_progress'; booking: VoiceBookingFact }
  | { kind: 'booking_confirmed'; booking: VoiceBookingFact; bookingId?: string; confirmedByLookup: boolean }
  | { kind: 'booking_not_made'; booking: VoiceBookingFact; reason: 'changed' | 'unavailable' | 'failed' | 'lookup_found_none' | 'rejected' }
  /** Lumi does not know whether the booking exists. Never success, never failure. */
  | { kind: 'outcome_unknown'; booking: VoiceBookingFact; lastCheckInconclusive: boolean }
  | { kind: 'checking'; booking: VoiceBookingFact }
  | { kind: 'task_open'; constraints: VoiceConstraintFact; taskStatus: AgentTaskStatus }
  | { kind: 'task_cancelled'; rejectedBooking: boolean }
  | {
    kind: 'needs_clarification'
    reason: VoiceClarification
    booking?: VoiceBookingFact
    candidates?: VoiceSlotFact[]
    dateOptions?: VoiceDateOption[]
  }
  | { kind: 'clinic_info'; topic: AgentClinicInfoTopic; profiles: VoiceProfileFact[] }
  /**
   * Milestone 7a page inspection. Only the host and a closed state: the page
   * text and the answer stay on the trusted card and are never spoken from
   * here, and no state means "approved".
   */
  | { kind: 'inspection'; host: string; state: VoiceInspectionState }
  /**
   * Milestone 7b public research. A closed state only: the objective is the
   * user's own words and the answer stays on the trusted card, so no page
   * text is ever spoken from here.
   */
  | { kind: 'research'; state: VoiceResearchState }
  /**
   * Milestone 11 S4. An orchestration is not a `tasks` row (`taskId`/`taskStatus`/`taskKind` on the
   * outcome do not apply to it), so its own id and closed state travel here instead. `pauseReason` is
   * present only while `status` is `'PAUSED'`. The step list and result summaries stay on the trusted
   * cockpit; nothing here is spoken or shown that a capability's own card does not already show.
   */
  | { kind: 'orchestration'; orchestrationId: string; status: AgentOrchestrationStatus; pauseReason?: AgentOrchestrationPauseReason }
  | { kind: 'preference_saved'; preference: PreferenceValue }
  | { kind: 'refused'; code: AgentErrorCode }

export interface VoiceTaskOutcome {
  kind: VoiceCommandKind
  taskId?: string
  taskStatus?: AgentTaskStatus
  focus: VoiceTaskFocus
  narration: VoiceNarration
  /** True when this turn was already handled and nothing ran again. */
  replayed: boolean
  taskKind?: AgentTaskKind
  /** For a compound request: what each step did. */
  plan?: PlanStepReport[]
}
