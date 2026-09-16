import { describe, expect, it } from 'vitest'
import {
  isValidTimeZone,
  localDateString,
  parseRelativeDayPhrase,
  resolveRelativeDay,
  type RelativeDayPhrase
} from './relative-dates'

const IST = 'Asia/Kolkata'
// Wednesday 16 September 2026, 10:00 in India.
const WEDNESDAY = new Date('2026-09-16T04:30:00Z')
const SATURDAY = new Date('2026-09-19T06:00:00Z')
const SUNDAY = new Date('2026-09-20T06:00:00Z')

function resolve(phrase: RelativeDayPhrase, now = WEDNESDAY, zone = IST) {
  return resolveRelativeDay(phrase, now, zone)
}

describe('relative day resolution (frozen time)', () => {
  it('resolves today, tomorrow and the day after in the user time zone', () => {
    expect(resolve({ kind: 'today' })).toEqual({ kind: 'resolved', dateFrom: '2026-09-16', dateTo: '2026-09-16', day: 'Wednesday' })
    expect(resolve({ kind: 'tomorrow' })).toMatchObject({ dateFrom: '2026-09-17', day: 'Thursday' })
    expect(resolve({ kind: 'day_after_tomorrow' })).toMatchObject({ dateFrom: '2026-09-18', day: 'Friday' })
  })

  it('uses the local date, not UTC, near midnight', () => {
    // 23:00 UTC on the 16th is already the 17th in India and still the 16th in New York.
    const late = new Date('2026-09-16T23:00:00Z')
    expect(resolve({ kind: 'today' }, late, IST)).toMatchObject({ dateFrom: '2026-09-17' })
    expect(resolve({ kind: 'today' }, late, 'America/New_York')).toMatchObject({ dateFrom: '2026-09-16' })
    expect(localDateString(late, 'Pacific/Auckland')).toBe('2026-09-17')
  })

  it('maps a bare weekday to its next occurrence, today included', () => {
    expect(resolve({ kind: 'weekday', weekday: 'Saturday' })).toMatchObject({ dateFrom: '2026-09-19', dateTo: '2026-09-19', day: 'Saturday' })
    expect(resolve({ kind: 'weekday', weekday: 'Wednesday' })).toMatchObject({ dateFrom: '2026-09-16' })
    expect(resolve({ kind: 'weekday', weekday: 'Monday' })).toMatchObject({ dateFrom: '2026-09-21' })
    expect(resolve({ kind: 'weekday', weekday: 'Saturday' }, SATURDAY)).toMatchObject({ dateFrom: '2026-09-19' })
  })

  it('asks when "next Saturday" could mean this week or the next', () => {
    expect(resolve({ kind: 'next_weekday', weekday: 'Saturday' })).toEqual({
      kind: 'ambiguous',
      phrase: 'next_weekday',
      candidates: [
        { dateFrom: '2026-09-19', dateTo: '2026-09-19', day: 'Saturday' },
        { dateFrom: '2026-09-26', dateTo: '2026-09-26', day: 'Saturday' }
      ]
    })
  })

  it('resolves "next <weekday>" when only one reading exists', () => {
    // Monday already passed this week, so "next Monday" is the 21st.
    expect(resolve({ kind: 'next_weekday', weekday: 'Monday' })).toMatchObject({ kind: 'resolved', dateFrom: '2026-09-21' })
    // Said on a Saturday, "next Saturday" is a week later.
    expect(resolve({ kind: 'next_weekday', weekday: 'Saturday' }, SATURDAY)).toMatchObject({ kind: 'resolved', dateFrom: '2026-09-26' })
    // Said on a Sunday, the coming Saturday is already next week.
    expect(resolve({ kind: 'next_weekday', weekday: 'Saturday' }, SUNDAY)).toMatchObject({ kind: 'resolved', dateFrom: '2026-09-26' })
  })

  it('resolves this weekend to Saturday and Sunday, or what is left of it', () => {
    expect(resolve({ kind: 'this_weekend' })).toEqual({ kind: 'resolved', dateFrom: '2026-09-19', dateTo: '2026-09-20' })
    expect(resolve({ kind: 'this_weekend' }, SATURDAY)).toEqual({ kind: 'resolved', dateFrom: '2026-09-19', dateTo: '2026-09-20' })
    expect(resolve({ kind: 'this_weekend' }, SUNDAY)).toMatchObject({ dateFrom: '2026-09-20', dateTo: '2026-09-20', day: 'Sunday' })
  })

  it('treats "next weekend" on a weekday as ambiguous and on a weekend as the following one', () => {
    expect(resolve({ kind: 'next_weekend' })).toMatchObject({ kind: 'ambiguous', phrase: 'next_weekend' })
    expect(resolve({ kind: 'next_weekend' }, SATURDAY)).toEqual({ kind: 'resolved', dateFrom: '2026-09-26', dateTo: '2026-09-27' })
  })

  it('accepts explicit dates only from today to 90 days ahead', () => {
    expect(resolve({ kind: 'date', date: '2026-09-26' })).toMatchObject({ kind: 'resolved', day: 'Saturday' })
    expect(resolve({ kind: 'date', date: '2026-09-15' })).toEqual({ kind: 'invalid', reason: 'past' })
    expect(resolve({ kind: 'date', date: '2027-09-15' })).toEqual({ kind: 'invalid', reason: 'too_far' })
    expect(resolve({ kind: 'date', date: '2026-02-30' })).toEqual({ kind: 'invalid', reason: 'malformed' })
  })

  it('parses only closed phrase objects', () => {
    expect(parseRelativeDayPhrase({ kind: 'weekday', weekday: 'Saturday' })).toEqual({ kind: 'weekday', weekday: 'Saturday' })
    for (const bad of [
      null, 'tomorrow', { kind: 'yesterday' }, { kind: 'weekday', weekday: 'Caturday' },
      { kind: 'today', date: '2026-09-16' }, { kind: 'date', date: 'soon' }, { kind: 'weekday' }
    ]) {
      expect(() => parseRelativeDayPhrase(bad)).toThrow()
    }
  })

  it('validates time zones', () => {
    expect(isValidTimeZone(IST)).toBe(true)
    expect(isValidTimeZone('Mars/Olympus_Mons')).toBe(false)
  })
})
