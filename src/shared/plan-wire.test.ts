import { describe, expect, it } from 'vitest'
import {
  choiceFromWire,
  clinicQueryFromWire,
  constraintsFromWire,
  planFromWire,
  preferenceFromWire,
  refinementFromWire
} from './plan-wire'
import { interpretByRules } from './rule-interpreter'

describe('plan wire parser', () => {
  it('maps a compound request onto a typed plan', () => {
    expect(planFromWire({
      search: { specialty: 'Dermatology', when: { kind: 'weekday', weekday: 'Saturday' }, part_of_day: 'evening', max_price_inr: 1000 },
      choose: { strategy: 'cheapest' },
      prepare: true,
      show_for_approval: null
    })).toEqual({
      search: { specialty: 'Dermatology', when: { kind: 'weekday', weekday: 'Saturday' }, partOfDay: 'evening', maxPriceInr: 1000 },
      choose: { strategy: 'cheapest' },
      prepare: true
    })
  })

  it('refuses anything outside the closed vocabulary', () => {
    const bad: unknown[] = [
      null, [], 'plan', {},
      { search: { specialty: 'Dermatology', url: 'https://evil.example' } },
      { search: { specialty: 'Astrology' } },
      { search: { earliest_time: '6pm' } },
      { search: { max_price_inr: -1 } },
      { search: { max_price_inr: 1.5 } },
      { search: { when: { kind: 'yesterday' } } },
      { choose: { strategy: 'cheapest' }, approve: true },
      { choose: { strategy: 'cheapest' }, execute: true },
      { choose: { strategy: 'selector', selector: '#confirm' } },
      { prepare: true },
      { search: { specialty: 'Dermatology' }, refine: { specialty: 'Dentistry' } },
      { choose: { strategy: 'cheapest' }, prepare: 'true' }
    ]
    for (const value of bad) expect(() => planFromWire(value), JSON.stringify(value)).toThrow()
  })

  it('validates each part', () => {
    expect(constraintsFromWire({ when: { kind: 'date', date: '2026-09-26' } })).toEqual({ when: { kind: 'date', date: '2026-09-26' } })
    expect(refinementFromWire({ clear: ['price', 'price'] })).toEqual({ clear: ['price'] })
    expect(() => refinementFromWire({})).toThrow()
    expect(choiceFromWire({ strategy: 'doctor', doctor: ' Dr B ' })).toEqual({ strategy: 'doctor', doctor: 'Dr B' })
    expect(() => choiceFromWire({ strategy: 'doctor', doctor: '<img src=x>' })).toThrow()
    expect(() => choiceFromWire({ strategy: 'number' })).toThrow()
    expect(clinicQueryFromWire({ doctor: 'Dr A', topic: 'languages' })).toEqual({ specialty: '', doctor: 'Dr A', topic: 'languages' })
    expect(() => clinicQueryFromWire({ topic: 'fee' })).toThrow()
    expect(preferenceFromWire({ key: 'max_price_inr', value: '800' })).toEqual({ key: 'max_price_inr', value: 800 })
    expect(preferenceFromWire({ key: 'preferred_part_of_day', value: 'evening' })).toEqual({ key: 'preferred_part_of_day', value: 'evening' })
    expect(() => preferenceFromWire({ key: 'home_address', value: 'x' })).toThrow()
    expect(() => preferenceFromWire({ key: 'max_price_inr', value: 'lots' })).toThrow()
  })
})

describe('deterministic English rules (test scaffolding and fallback)', () => {
  it('reads the acceptance scenarios', () => {
    expect(interpretByRules('Find me a dermatologist Saturday evening under ₹1000 and prepare the cheapest available option.')).toEqual({
      intent: 'appointment_plan',
      plan: {
        search: { specialty: 'Dermatology', when: { kind: 'weekday', weekday: 'Saturday' }, part_of_day: 'evening', max_price_inr: 1000 },
        choose: { strategy: 'cheapest' },
        prepare: true
      }
    })
    expect(interpretByRules('Find appointments after 6 and prepare the first available one.')).toEqual({
      intent: 'appointment_plan',
      plan: { search: { earliest_time: '18:00' }, choose: { strategy: 'number', result_number: 1 }, prepare: true }
    })
    expect(interpretByRules('Find the first one and book it.')).toEqual({
      intent: 'appointment_plan',
      plan: { choose: { strategy: 'number', result_number: 1 }, prepare: true, show_for_approval: true }
    })
    expect(interpretByRules('Book it.')).toEqual({ intent: 'appointment_plan', plan: { show_for_approval: true } })
  })

  it('keeps Milestone 5 single-step phrasing', () => {
    expect(interpretByRules('Find me a dermatologist Saturday evening under 1000.')).toEqual({
      intent: 'appointment_plan',
      plan: { search: { specialty: 'Dermatology', day: 'Saturday', part_of_day: 'evening', max_price_inr: 1000 } }
    })
    expect(interpretByRules('Take the 6:30 one.', { lastResultTimes: ['18:30'] })).toEqual({
      intent: 'appointment_plan', plan: { choose: { strategy: 'time', time: '18:30' }, prepare: true }
    })
    expect(interpretByRules('Actually under 900.')).toEqual({ intent: 'appointment_plan', plan: { refine: { max_price_inr: 900 } } })
    expect(interpretByRules('Please check it.')).toEqual({ intent: 'check_booking' })
    expect(interpretByRules('Cancel this task.')).toEqual({ intent: 'cancel_task' })
    expect(interpretByRules('What happened? What is the status?')).toEqual({ intent: 'status' })
  })

  it('reads relative days, clinic questions and preferences', () => {
    expect(interpretByRules('find a dentist tomorrow evening')).toMatchObject({ plan: { search: { specialty: 'Dentistry', when: { kind: 'tomorrow' }, part_of_day: 'evening' } } })
    expect(interpretByRules('find a dermatologist this evening')).toMatchObject({ plan: { search: { when: { kind: 'today' }, part_of_day: 'evening' } } })
    expect(interpretByRules('find a dermatologist next saturday')).toMatchObject({ plan: { search: { when: { kind: 'next_weekday', weekday: 'Saturday' } } } })
    expect(interpretByRules('What languages does Dr A speak?')).toEqual({ intent: 'clinic_info', clinic: { doctor: 'Dr A', topic: 'languages' } })
    expect(interpretByRules('remember I prefer evening appointments')).toEqual({
      intent: 'remember_preference', preference: { key: 'preferred_part_of_day', value: 'evening' }
    })
    expect(interpretByRules('Hello, how are you today?')).toEqual({ intent: 'conversation' })
    expect(interpretByRules('')).toEqual({ intent: 'conversation' })
  })
})
