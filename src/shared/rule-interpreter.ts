/**
 * A small, deterministic English rule set that turns an utterance into the
 * same interpretation JSON a language model is asked to produce.
 *
 * It is test scaffolding and a last-resort fallback, not Lumi's language
 * understanding: the scripted realtime harness and the scripted text provider
 * use it so acceptance tests are reproducible, and the typed-request path uses
 * it only when every configured model failed. It knows English only; Telugu
 * and code-switched speech are understood by the live models (see
 * docs/PROVIDERS.md for what was actually validated).
 *
 * Its output goes through exactly the same strict parser as model output.
 */

export type InterpretationIntent =
  | 'appointment_plan'
  | 'clinic_info'
  | 'status'
  | 'check_booking'
  | 'cancel_task'
  | 'remember_preference'
  | 'conversation'

export type Json = Record<string, unknown>

export interface InterpretationWire {
  intent: InterpretationIntent
  plan?: Json
  clinic?: Json
  preference?: Json
}

export interface RuleContext {
  /** Clinic-local HH:MM of the results the user was last told about. */
  lastResultTimes?: readonly string[]
  /** Whether an appointment task is currently open. */
  hasOpenTask?: boolean
}

const WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
const WORD_ORDINALS: Record<string, number> = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5, '1st': 1, '2nd': 2, '3rd': 3
}

function titleCase(word: string): string {
  return word[0].toUpperCase() + word.slice(1)
}

function clock(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

function hourIn(match: RegExpMatchArray, hourIndex: number, minuteIndex: number, meridiemIndex: number, assumeEvening: boolean): string {
  let hour = Number(match[hourIndex])
  const minute = match[minuteIndex] ? Number(match[minuteIndex]) : 0
  const meridiem = match[meridiemIndex]
  if (meridiem === 'pm' && hour < 12) hour += 12
  if (meridiem === 'am' && hour === 12) hour = 0
  // "after 6" for an appointment means the evening.
  if (!meridiem && assumeEvening && hour >= 1 && hour <= 8) hour += 12
  return clock(hour, minute)
}

function specialtyOf(text: string): string | undefined {
  if (/\b(dermatolog\w*|skin doctor)\b/.test(text)) return 'Dermatology'
  if (/\b(dentist\w*|dental)\b/.test(text)) return 'Dentistry'
  if (/\b(cardiolog\w*|heart doctor)\b/.test(text)) return 'Cardiology'
  if (/\b(pediatric\w*|paediatric\w*|child doctor)\b/.test(text)) return 'Pediatrics'
  return undefined
}

function doctorOf(text: string): string | undefined {
  const doctor = /\b(dr\.?\s+[a-z]+)\b/.exec(text)
  if (!doctor) return undefined
  return doctor[1].replace(/^dr\.?\s+/, 'Dr ').replace(/ ([a-z])/, (_, letter: string) => ` ${letter.toUpperCase()}`)
}

/** The kind of day, never a calendar date (unless one was spoken). */
function whenOf(text: string, legacyWeekday: boolean): { when?: Json; day?: string; partOfDay?: string } {
  if (/\b(tonight|this evening)\b/.test(text)) return { when: { kind: 'today' }, partOfDay: 'evening' }
  if (/\bday after tomorrow\b/.test(text)) return { when: { kind: 'day_after_tomorrow' } }
  if (/\btomorrow\b/.test(text)) return { when: { kind: 'tomorrow' } }
  if (/\btoday\b/.test(text)) return { when: { kind: 'today' } }
  if (/\bnext weekend\b/.test(text)) return { when: { kind: 'next_weekend' } }
  if (/\b(this weekend|the weekend)\b/.test(text)) return { when: { kind: 'this_weekend' } }
  const explicit = /\b(\d{4}-\d{2}-\d{2})\b/.exec(text)
  if (explicit) return { when: { kind: 'date', date: explicit[1] } }
  const next = new RegExp(`\\bnext (${WEEKDAYS.join('|')})\\b`).exec(text)
  if (next) return { when: { kind: 'next_weekday', weekday: titleCase(next[1]) } }
  const weekday = WEEKDAYS.find((name) => new RegExp(`\\b${name}\\b`).test(text))
  if (weekday) {
    return legacyWeekday ? { day: titleCase(weekday) } : { when: { kind: 'weekday', weekday: titleCase(weekday) } }
  }
  return {}
}

function constraintsOf(text: string, legacyWeekday: boolean): Json {
  const fields: Json = {}
  const specialty = specialtyOf(text)
  if (specialty) fields.specialty = specialty
  const day = whenOf(text, legacyWeekday)
  if (day.when) fields.when = day.when
  if (day.day) fields.day = day.day
  const part = /\b(morning|afternoon|evening)\b/.exec(text)
  if (part) fields.part_of_day = part[1]
  else if (day.partOfDay) fields.part_of_day = day.partOfDay
  const after = /\bafter (\d{1,2})(?::(\d{2}))?\s*(am|pm)?/.exec(text)
  if (after) fields.earliest_time = hourIn(after, 1, 2, 3, true)
  const before = /\bbefore (\d{1,2})(?::(\d{2}))?\s*(am|pm)?/.exec(text)
  if (before) fields.latest_time = hourIn(before, 1, 2, 3, true)
  const under = /\b(?:under|below|less than|within|up to|budget(?: is)?)\s*(?:rs\.?|₹|inr)?\s*(\d{2,7})/.exec(text)
  if (under) fields.max_price_inr = Number(under[1])
  return fields
}

function choiceOf(text: string, context: RuleContext): Json | undefined {
  if (/\b(cheapest|lowest price|least expensive)\b/.test(text)) return { strategy: 'cheapest' }
  if (/\b(earliest|soonest)\b/.test(text)) return { strategy: 'earliest' }
  if (/\b(latest one|last one)\b/.test(text)) return { strategy: 'latest' }
  const ordinal = /\b(first|second|third|fourth|fifth|1st|2nd|3rd)\b/.exec(text)
  if (ordinal) return { strategy: 'number', result_number: WORD_ORDINALS[ordinal[1]] }
  const time = /\b(\d{1,2}):(\d{2})\s*(am|pm)?/.exec(text)
  if (time) {
    let value = hourIn(time, 1, 2, 3, false)
    // Like a model with the conversation in context: "the 6:30 one" after
    // evening results means 18:30.
    const hour = Number(value.slice(0, 2))
    const evening = clock(hour + 12, Number(value.slice(3)))
    if (!time[3] && hour < 12 && (context.lastResultTimes ?? []).includes(evening)) value = evening
    return { strategy: 'time', time: value }
  }
  const doctor = doctorOf(text)
  if (doctor) return { strategy: 'doctor', doctor }
  return undefined
}

const INFO_TOPICS: Array<[RegExp, string]> = [
  [/\b(language|languages|speak|speaks)\b/, 'languages'],
  [/\b(hours|open|opening|timings|close|closing)\b/, 'hours'],
  [/\b(fee|fees|charge|charges|consultation cost)\b/, 'fee'],
  [/\b(address|where is|located|location)\b/, 'address'],
  [/\b(walk-?ins?|without (?:an )?appointment)\b/, 'walk_ins']
]

export function interpretByRules(raw: string, context: RuleContext = {}): InterpretationWire {
  const text = raw.toLowerCase().replace(/\s+/g, ' ').trim()
  if (!text) return { intent: 'conversation' }

  if (/\bremember\b/.test(text)) {
    const part = /\b(morning|afternoon|evening)\b/.exec(text)
    if (part) return { intent: 'remember_preference', preference: { key: 'preferred_part_of_day', value: part[1] } }
    const budget = /(\d{2,7})/.exec(text)
    if (budget && /\b(budget|price|under|rupees|₹|inr)\b|₹/.test(text)) {
      return { intent: 'remember_preference', preference: { key: 'max_price_inr', value: budget[1] } }
    }
    const language = /\b(telugu|hindi|english)\b/.exec(text)
    if (language) return { intent: 'remember_preference', preference: { key: 'reply_language', value: titleCase(language[1]) } }
  }
  if (/\b(cancel|stop)\b/.test(text) && /\b(task|search|searching|booking|this|it)\b/.test(text)) return { intent: 'cancel_task' }
  if (/\bcheck\b/.test(text) && !/\bcheck (?:the )?(?:fee|hours|languages?)\b/.test(text)) return { intent: 'check_booking' }
  if (/\b(status|what happened|did it go through)\b/.test(text)) return { intent: 'status' }

  const infoTopic = INFO_TOPICS.find(([pattern]) => pattern.test(text))
  const doctor = doctorOf(text)
  const specialty = specialtyOf(text)
  if (infoTopic && (doctor || specialty) && !/\b(find|search|appointment|slot|book)\b/.test(text)) {
    return {
      intent: 'clinic_info',
      clinic: { ...(doctor ? { doctor } : {}), ...(specialty && !doctor ? { specialty } : {}), topic: infoTopic[1] }
    }
  }

  const wantsSearch = /\b(find|search|look for|looking for|need|get me)\b/.test(text)
  const bookIt = /\b(book it|book that|book this|and book|yes|go ahead|confirm|approve|do it)\b/.test(text)
  const prepare = /\b(prepare|get it ready|set it up|reserve)\b/.test(text) || (bookIt && /\b(and|then)\b/.test(text))
  const pickCue = /\b(take|choose|pick|select|want|go with|fine|one|show me|cheapest|earliest|first|second|third)\b/.test(text)
  const choice = pickCue ? choiceOf(text, context) : undefined

  const plan: Json = {}
  const constraints = constraintsOf(text, !(choice || prepare))
  const hasConstraints = Object.keys(constraints).length > 0
  if (wantsSearch && (constraints.specialty || (hasConstraints && /\bappointments?\b/.test(text)))) {
    plan.search = constraints
  } else if (hasConstraints && !choice && (context.hasOpenTask || /\b(appointments?|only|actually|instead|change)\b/.test(text))) {
    plan.refine = constraints
  } else if (hasConstraints && choice && context.hasOpenTask && !wantsSearch) {
    plan.refine = constraints
  }
  if (choice) plan.choose = choice
  if (prepare && choice) plan.prepare = true
  if (bookIt) plan.show_for_approval = true
  // "take the 6:30 one": choosing is preparing, as in Milestone 5.
  if (choice && !plan.search && !plan.refine && !/\bshow me\b/.test(text) && choice.strategy !== 'cheapest' &&
      choice.strategy !== 'earliest' && choice.strategy !== 'latest') {
    plan.prepare = true
  }
  if (plan.show_for_approval && !plan.choose && (plan.search || plan.refine)) delete plan.show_for_approval
  if (Object.keys(plan).length === 0) return { intent: 'conversation' }
  return { intent: 'appointment_plan', plan }
}
