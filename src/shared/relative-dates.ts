import { BOOKING_DAYS, type AgentBookingDay } from './agent-contracts'

/**
 * Deterministic scheduling-phrase resolution.
 *
 * A model (realtime or text) may only say *which kind* of day the user meant:
 * "tomorrow", "Saturday", "next Saturday", "this weekend", or an explicit
 * calendar date the user actually spoke. Turning that into a calendar date is
 * this module's job, using the current instant and the user's time zone, both
 * supplied by Electron main. A model never invents the calendar.
 *
 * Rules (documented because "next Saturday" means different things to
 * different people):
 *
 * - `today` / `tomorrow` / `day_after_tomorrow`: the local calendar date.
 * - `weekday` ("Saturday", "this Saturday"): the next occurrence, today
 *   included.
 * - `next_weekday` ("next Saturday"): if that weekday still falls later in the
 *   current Monday–Sunday week, the phrase is ambiguous (this week's or the
 *   following one) and Lumi asks. Otherwise it is the next occurrence after
 *   today.
 * - `this_weekend`: the coming Saturday–Sunday; on a Sunday, just today.
 * - `next_weekend`: ambiguous on a weekday; on a weekend, the following one.
 * - `date`: an explicit date, accepted only from today up to 90 days ahead.
 */

export const RELATIVE_DAY_KINDS = [
  'today', 'tomorrow', 'day_after_tomorrow', 'weekday', 'next_weekday', 'this_weekend', 'next_weekend', 'date'
] as const
export type RelativeDayKind = typeof RELATIVE_DAY_KINDS[number]

export type RelativeDayPhrase =
  | { kind: 'today' | 'tomorrow' | 'day_after_tomorrow' | 'this_weekend' | 'next_weekend' }
  | { kind: 'weekday' | 'next_weekday'; weekday: AgentBookingDay }
  | { kind: 'date'; date: string }

export interface ResolvedDateWindow {
  kind: 'resolved'
  /** Inclusive local calendar dates, YYYY-MM-DD. */
  dateFrom: string
  dateTo: string
  /** Set when the window is exactly one day. */
  day?: AgentBookingDay
}

export interface AmbiguousDate {
  kind: 'ambiguous'
  phrase: RelativeDayKind
  candidates: Array<{ dateFrom: string; dateTo: string; day?: AgentBookingDay }>
}

export interface InvalidDate {
  kind: 'invalid'
  reason: 'past' | 'too_far' | 'malformed'
}

export type DateResolution = ResolvedDateWindow | AmbiguousDate | InvalidDate

export const MAX_EXPLICIT_DATE_DAYS = 90
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

/** A calendar date with no time zone: days since the epoch, UTC-anchored. */
interface CivilDate {
  ordinal: number
}

function civil(year: number, month: number, day: number): CivilDate {
  return { ordinal: Math.round(Date.UTC(year, month - 1, day) / 86_400_000) }
}

function addDays(date: CivilDate, days: number): CivilDate {
  return { ordinal: date.ordinal + days }
}

function iso(date: CivilDate): string {
  return new Date(date.ordinal * 86_400_000).toISOString().slice(0, 10)
}

/** 0 = Monday … 6 = Sunday. */
function weekdayIndex(date: CivilDate): number {
  return (new Date(date.ordinal * 86_400_000).getUTCDay() + 6) % 7
}

function dayName(date: CivilDate): AgentBookingDay {
  return BOOKING_DAYS[weekdayIndex(date)]
}

export function isValidTimeZone(timeZone: string): boolean {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone })
    return true
  } catch {
    return false
  }
}

/** The user's local calendar date for an instant, in a trusted time zone. */
export function localDate(now: Date, timeZone: string): CivilDate {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(now)
  const value = (type: string): number => Number(parts.find((part) => part.type === type)?.value)
  return civil(value('year'), value('month'), value('day'))
}

export function localDateString(now: Date, timeZone: string): string {
  return iso(localDate(now, timeZone))
}

function single(date: CivilDate): ResolvedDateWindow {
  return { kind: 'resolved', dateFrom: iso(date), dateTo: iso(date), day: dayName(date) }
}

function span(from: CivilDate, to: CivilDate): ResolvedDateWindow {
  return from.ordinal === to.ordinal ? single(from) : { kind: 'resolved', dateFrom: iso(from), dateTo: iso(to) }
}

function nextOccurrence(today: CivilDate, weekday: AgentBookingDay, includeToday: boolean): CivilDate {
  const target = BOOKING_DAYS.indexOf(weekday)
  let offset = (target - weekdayIndex(today) + 7) % 7
  if (offset === 0 && !includeToday) offset = 7
  return addDays(today, offset)
}

export function resolveRelativeDay(phrase: RelativeDayPhrase, now: Date, timeZone: string): DateResolution {
  const today = localDate(now, timeZone)
  const todayIndex = weekdayIndex(today)
  switch (phrase.kind) {
    case 'today':
      return single(today)
    case 'tomorrow':
      return single(addDays(today, 1))
    case 'day_after_tomorrow':
      return single(addDays(today, 2))
    case 'weekday':
      return single(nextOccurrence(today, phrase.weekday, true))
    case 'next_weekday': {
      const upcoming = nextOccurrence(today, phrase.weekday, false)
      const daysLeftInWeek = 6 - todayIndex
      if (upcoming.ordinal - today.ordinal <= daysLeftInWeek) {
        return {
          kind: 'ambiguous',
          phrase: 'next_weekday',
          candidates: [single(upcoming), single(addDays(upcoming, 7))].map(({ dateFrom, dateTo, day }) => ({ dateFrom, dateTo, day }))
        }
      }
      return single(upcoming)
    }
    case 'this_weekend': {
      if (todayIndex === 6) return single(today)
      const saturday = nextOccurrence(today, 'Saturday', true)
      return span(saturday, addDays(saturday, 1))
    }
    case 'next_weekend': {
      if (todayIndex >= 5) {
        const saturday = nextOccurrence(today, 'Saturday', false)
        return span(saturday, addDays(saturday, 1))
      }
      const saturday = nextOccurrence(today, 'Saturday', false)
      const first = span(saturday, addDays(saturday, 1))
      const second = span(addDays(saturday, 7), addDays(saturday, 8))
      return {
        kind: 'ambiguous',
        phrase: 'next_weekend',
        candidates: [first, second].map(({ dateFrom, dateTo }) => ({ dateFrom, dateTo }))
      }
    }
    case 'date': {
      const match = ISO_DATE.exec(phrase.date)
      if (!match) return { kind: 'invalid', reason: 'malformed' }
      const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])]
      const date = civil(year, month, day)
      if (iso(date) !== phrase.date) return { kind: 'invalid', reason: 'malformed' }
      if (date.ordinal < today.ordinal) return { kind: 'invalid', reason: 'past' }
      if (date.ordinal - today.ordinal > MAX_EXPLICIT_DATE_DAYS) return { kind: 'invalid', reason: 'too_far' }
      return single(date)
    }
  }
}

/** Parse an untrusted phrase object from a tool call or model output. */
export function parseRelativeDayPhrase(value: unknown): RelativeDayPhrase {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('The day is invalid.')
  const record = value as Record<string, unknown>
  const kind = record.kind
  const keys = Object.keys(record)
  const only = (allowed: string[]): void => {
    if (keys.some((key) => !allowed.includes(key))) throw new Error('The day is invalid.')
  }
  switch (kind) {
    case 'today': case 'tomorrow': case 'day_after_tomorrow': case 'this_weekend': case 'next_weekend':
      only(['kind'])
      return { kind }
    case 'weekday': case 'next_weekday':
      only(['kind', 'weekday'])
      if (typeof record.weekday !== 'string' || !(BOOKING_DAYS as readonly string[]).includes(record.weekday)) {
        throw new Error('The day is invalid.')
      }
      return { kind, weekday: record.weekday as AgentBookingDay }
    case 'date':
      only(['kind', 'date'])
      if (typeof record.date !== 'string' || !ISO_DATE.test(record.date)) throw new Error('The day is invalid.')
      return { kind, date: record.date }
    default:
      throw new Error('The day is invalid.')
  }
}
