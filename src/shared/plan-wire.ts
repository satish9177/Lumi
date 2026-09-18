import { BOOKING_DAYS, CLINIC_INFO_TOPICS, type AgentClinicInfoQuery } from './agent-contracts'
import {
  PREFERENCE_KEYS,
  PREFERENCE_LANGUAGES,
  PREFERENCE_PARTS_OF_DAY,
  type PreferenceValue
} from './model-contracts'
import { parseRelativeDayPhrase, RELATIVE_DAY_KINDS, type RelativeDayPhrase } from './relative-dates'
import {
  MAX_PLAN_STEPS,
  MAX_VOICE_ORDINAL,
  MAX_VOICE_PRICE,
  PLAN_CHOICE_STRATEGIES,
  VOICE_CLEARABLE_FIELDS,
  VOICE_PARTS_OF_DAY,
  VOICE_SPECIALTIES,
  type PlanChoice,
  type TaskPlan,
  type VoiceRefinement,
  type VoiceSearchConstraints
} from './voice-task-contracts'

/**
 * The one strict mapping from model-produced JSON (snake_case, from a realtime
 * tool call or a text model's structured output) to Lumi's typed plan.
 *
 * Everything a model may say is an enum, a bounded integer, an HH:MM string,
 * a relative-day *kind* or a short name. There is no URL, selector, script,
 * price-to-book, booking id, approval or execution field, and unknown keys are
 * refused rather than ignored.
 */

export class PlanWireError extends Error {
  constructor(message = 'Lumi received request details it does not support.') {
    super(message)
    this.name = 'PlanWireError'
  }
}

type Json = Record<string, unknown>

const CLOCK_PATTERN = '^([01][0-9]|2[0-3]):[0-5][0-9]$'
const CLOCK = new RegExp(CLOCK_PATTERN)
const DATE_PATTERN = '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
const NAME = /^[\p{L}\p{M}][\p{L}\p{M} .'-]{0,59}$/u

function record(value: unknown): Json {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new PlanWireError()
  return value as Json
}

function onlyKeys(value: Json, allowed: readonly string[]): void {
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new PlanWireError()
  }
}

function oneOf<T extends string>(values: readonly T[], value: unknown): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new PlanWireError()
  return value as T
}

function clock(value: unknown): string {
  if (typeof value !== 'string' || !CLOCK.test(value)) throw new PlanWireError('Times must be 24-hour HH:MM.')
  return value
}

function integer(value: unknown, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new PlanWireError('Lumi received an out-of-range detail.')
  }
  return value
}

function name(value: unknown): string {
  if (typeof value !== 'string') throw new PlanWireError()
  const trimmed = value.normalize('NFKC').trim()
  if (!NAME.test(trimmed)) throw new PlanWireError('Lumi received an invalid name.')
  return trimmed
}

/** Models may send `null` for "not said"; treat it exactly like absent. */
function present(value: Json, key: string): boolean {
  return value[key] !== undefined && value[key] !== null
}

export const CONSTRAINT_WIRE_KEYS = ['specialty', 'day', 'part_of_day', 'earliest_time', 'latest_time', 'max_price_inr', 'when'] as const

export function whenFromWire(value: unknown): RelativeDayPhrase {
  try {
    return parseRelativeDayPhrase(value)
  } catch {
    throw new PlanWireError('Lumi received a day it does not support.')
  }
}

export function constraintsFromWire(value: unknown): VoiceSearchConstraints {
  const args = record(value)
  onlyKeys(args, CONSTRAINT_WIRE_KEYS)
  const result: VoiceSearchConstraints = {}
  if (present(args, 'specialty')) result.specialty = oneOf(VOICE_SPECIALTIES, args.specialty)
  if (present(args, 'day')) result.day = oneOf(BOOKING_DAYS, args.day)
  if (present(args, 'part_of_day')) result.partOfDay = oneOf(VOICE_PARTS_OF_DAY, args.part_of_day)
  if (present(args, 'earliest_time')) result.earliestTime = clock(args.earliest_time)
  if (present(args, 'latest_time')) result.latestTime = clock(args.latest_time)
  if (present(args, 'max_price_inr')) result.maxPriceInr = integer(args.max_price_inr, 0, MAX_VOICE_PRICE)
  if (present(args, 'when')) result.when = whenFromWire(args.when)
  return result
}

export function refinementFromWire(value: unknown): VoiceRefinement {
  const args = record(value)
  onlyKeys(args, [...CONSTRAINT_WIRE_KEYS, 'clear'])
  const { clear, ...rest } = args
  const changes: VoiceRefinement = constraintsFromWire(rest)
  if (clear !== undefined && clear !== null) {
    if (!Array.isArray(clear) || clear.length > VOICE_CLEARABLE_FIELDS.length) throw new PlanWireError()
    const fields = [...new Set(clear.map((field) => oneOf(VOICE_CLEARABLE_FIELDS, field)))]
    if (fields.length > 0) changes.clear = fields
  }
  if (Object.keys(changes).length === 0) throw new PlanWireError('Ask the user what should change about the search.')
  return changes
}

export function choiceFromWire(value: unknown): PlanChoice {
  const args = record(value)
  onlyKeys(args, ['strategy', 'result_number', 'time', 'doctor'])
  const choice: PlanChoice = { strategy: oneOf(PLAN_CHOICE_STRATEGIES, args.strategy) }
  if (present(args, 'result_number')) choice.ordinal = integer(args.result_number, 1, MAX_VOICE_ORDINAL)
  if (present(args, 'time')) choice.time = clock(args.time)
  if (present(args, 'doctor')) choice.doctor = name(args.doctor)
  if (choice.strategy === 'number' && choice.ordinal === undefined) throw new PlanWireError('Ask the user which result number they mean.')
  if (choice.strategy === 'time' && choice.time === undefined) throw new PlanWireError('Ask the user which time they mean.')
  if (choice.strategy === 'doctor' && choice.doctor === undefined) throw new PlanWireError('Ask the user which doctor they mean.')
  return choice
}

export const PLAN_WIRE_KEYS = ['search', 'refine', 'choose', 'prepare', 'show_for_approval'] as const

export function planFromWire(value: unknown): TaskPlan {
  const args = record(value)
  onlyKeys(args, PLAN_WIRE_KEYS)
  const plan: TaskPlan = {}
  if (present(args, 'search')) plan.search = constraintsFromWire(args.search)
  if (present(args, 'refine')) plan.refine = refinementFromWire(args.refine)
  if (present(args, 'choose')) plan.choose = choiceFromWire(args.choose)
  for (const [wire, key] of [['prepare', 'prepare'], ['show_for_approval', 'showForApproval']] as const) {
    if (!present(args, wire)) continue
    if (typeof args[wire] !== 'boolean') throw new PlanWireError()
    if (args[wire]) plan[key] = true
  }
  const steps = [plan.search, plan.refine, plan.choose, plan.prepare, plan.showForApproval].filter(Boolean).length
  if (steps === 0) throw new PlanWireError('Ask the user what they would like to do.')
  if (steps > MAX_PLAN_STEPS) throw new PlanWireError('That is too many steps at once. Ask the user to split the request.')
  if (plan.search && plan.refine) throw new PlanWireError('A request either starts a new search or changes the current one.')
  if (plan.prepare && !plan.choose) throw new PlanWireError('Ask the user which result to prepare.')
  return plan
}

export function clinicQueryFromWire(value: unknown): AgentClinicInfoQuery {
  const args = record(value)
  onlyKeys(args, ['specialty', 'doctor', 'topic'])
  const query: AgentClinicInfoQuery = { specialty: '', doctor: '', topic: 'overview' }
  if (present(args, 'specialty')) query.specialty = oneOf(VOICE_SPECIALTIES, args.specialty)
  if (present(args, 'doctor')) query.doctor = name(args.doctor)
  if (present(args, 'topic')) query.topic = oneOf(CLINIC_INFO_TOPICS, args.topic)
  if (!query.specialty && !query.doctor) throw new PlanWireError('Ask the user which doctor or specialty they mean.')
  return query
}

/**
 * The objective of a public-research request. One bounded string, and
 * nothing else: there is no field here for an address, a step, a selector or
 * a script, so a model can describe what to find out and nothing more.
 */
export function researchObjectiveFromWire(value: unknown): string {
  const args = record(value)
  onlyKeys(args, ['objective'])
  const objective = typeof args.objective === 'string' ? args.objective.replace(/\s+/g, ' ').trim() : ''
  // eslint-disable-next-line no-control-regex
  if (!objective || objective.length > 500 || /[\x00-\x1f\x7f]/.test(objective)) {
    throw new PlanWireError('Say what to find out from public web pages.')
  }
  return objective
}

export function preferenceFromWire(value: unknown): PreferenceValue {
  const args = record(value)
  onlyKeys(args, ['key', 'value'])
  const key = oneOf(PREFERENCE_KEYS, args.key)
  switch (key) {
    case 'preferred_part_of_day':
      return { key, value: oneOf(PREFERENCE_PARTS_OF_DAY, args.value) }
    case 'max_price_inr': {
      const amount = typeof args.value === 'string' && /^\d{1,7}$/.test(args.value) ? Number(args.value) : args.value
      return { key, value: integer(amount, 0, MAX_VOICE_PRICE) }
    }
    case 'reply_language':
      return { key, value: oneOf(PREFERENCE_LANGUAGES, args.value) }
  }
}

// ---- JSON schemas shared by realtime tools and text structured output --------

export const WHEN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  description: 'The kind of day the user said. Lumi computes the calendar date. today / tomorrow / day_after_tomorrow; weekday for "Saturday" or "this Saturday"; next_weekday for "next Saturday"; this_weekend; next_weekend; date only for an explicit calendar date the user said (YYYY-MM-DD).',
  properties: {
    kind: { type: 'string', enum: [...RELATIVE_DAY_KINDS] },
    weekday: { type: 'string', enum: [...BOOKING_DAYS] },
    date: { type: 'string', pattern: DATE_PATTERN }
  },
  required: ['kind']
} as const

export const CONSTRAINT_SCHEMA_PROPERTIES = {
  specialty: {
    type: 'string',
    enum: [...VOICE_SPECIALTIES],
    description: 'The kind of doctor, as a canonical specialty. "dermatologist" or "skin doctor" is Dermatology; "dentist" is Dentistry. Map any language onto these values.'
  },
  when: WHEN_SCHEMA,
  part_of_day: {
    type: 'string',
    enum: [...VOICE_PARTS_OF_DAY],
    description: 'morning, afternoon or evening when the user said so ("this evening", "sayantram"); any to drop a time-of-day limit.'
  },
  earliest_time: { type: 'string', pattern: CLOCK_PATTERN, description: 'Clinic-local 24-hour HH:MM lower bound. "after 6 PM" is 18:00.' },
  latest_time: { type: 'string', pattern: CLOCK_PATTERN, description: 'Clinic-local 24-hour HH:MM upper bound. "before 8 PM" is 20:00.' },
  max_price_inr: {
    type: 'integer',
    minimum: 0,
    maximum: MAX_VOICE_PRICE,
    description: 'Highest acceptable price in Indian rupees. "under 1000" or "1000 lopala" is 1000.'
  }
} as const

export const CHOICE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  description: 'Which recorded result to pick: cheapest, earliest, latest, or by number, time or doctor the user said.',
  properties: {
    strategy: { type: 'string', enum: [...PLAN_CHOICE_STRATEGIES] },
    result_number: { type: 'integer', minimum: 1, maximum: MAX_VOICE_ORDINAL },
    time: { type: 'string', pattern: CLOCK_PATTERN },
    doctor: { type: 'string', maxLength: 60 }
  },
  required: ['strategy']
} as const

export const PLAN_SCHEMA_PROPERTIES = {
  search: {
    type: 'object',
    additionalProperties: false,
    description: 'Start a NEW appointment search with these constraints.',
    properties: CONSTRAINT_SCHEMA_PROPERTIES
  },
  refine: {
    type: 'object',
    additionalProperties: false,
    description: 'Change the CURRENT search.',
    properties: {
      ...CONSTRAINT_SCHEMA_PROPERTIES,
      clear: { type: 'array', maxItems: VOICE_CLEARABLE_FIELDS.length, items: { type: 'string', enum: [...VOICE_CLEARABLE_FIELDS] } }
    }
  },
  choose: CHOICE_SCHEMA,
  prepare: { type: 'boolean', description: 'Prepare the chosen appointment for the user to review. Never books.' },
  show_for_approval: {
    type: 'boolean',
    description: 'The user also said book it / confirm. This only shows the booking card; the user must press Approve and book themselves.'
  }
} as const

export const CLINIC_QUERY_SCHEMA_PROPERTIES = {
  specialty: { type: 'string', enum: [...VOICE_SPECIALTIES] },
  doctor: { type: 'string', maxLength: 60, description: 'A doctor name exactly as the user or Lumi said it, such as "Dr A".' },
  topic: { type: 'string', enum: [...CLINIC_INFO_TOPICS], description: 'What the user wants to know.' }
} as const

export const RESEARCH_SCHEMA_PROPERTIES = {
  objective: {
    type: 'string',
    maxLength: 500,
    description: 'What to find out from public web pages, in the user own words. Never a URL Lumi invented, never a selector or a script.'
  }
} as const

export const PREFERENCE_SCHEMA_PROPERTIES = {
  key: { type: 'string', enum: [...PREFERENCE_KEYS] },
  value: {
    type: 'string',
    maxLength: 16,
    description: 'preferred_part_of_day: morning, afternoon or evening. max_price_inr: whole rupees as digits, such as "800". reply_language: English, Telugu, Hindi or Telugu-English.'
  }
} as const
