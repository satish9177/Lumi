import { VOICE_TASK_TOOLS, type VoiceNarration, type VoiceSlotFact } from './voice-task-contracts'
import { interpretByRules } from './rule-interpreter'

/**
 * The deterministic "model" behind both scripted realtime harnesses (the
 * OpenAI-protocol one in the renderer and the Gemini-protocol one in main).
 * Test scaffolding only; the tool names are the production ones.
 */


type Json = Record<string, unknown>

export interface ScriptedToolCall {
  name: string
  arguments: Json
}

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function formatPrice(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 0 }).format(amount)
  } catch {
    return `${amount} ${currency}`
  }
}

/** "Sat 19 Sep" for a YYYY-MM-DD calendar date, without shifting time zones. */
export function formatCalendarDate(value: string): string {
  const date = new Date(`${value}T12:00:00Z`)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
}

/**
 * The shared English rule set, mapped onto the realtime tools the way a model
 * would: a single step uses the single-step tool (as in Milestone 5),
 * anything compound uses appointment_plan.
 */
export function scriptedToolCall(utterance: string, lastResults: readonly VoiceSlotFact[]): ScriptedToolCall | undefined {
  const interpretation = interpretByRules(utterance, {
    lastResultTimes: lastResults.map((slot) => slot.time),
    hasOpenTask: lastResults.length > 0
  })
  switch (interpretation.intent) {
    case 'conversation': return undefined
    case 'status': return { name: VOICE_TASK_TOOLS.status, arguments: {} }
    case 'check_booking': return { name: VOICE_TASK_TOOLS.check, arguments: {} }
    case 'cancel_task': return { name: VOICE_TASK_TOOLS.cancel, arguments: {} }
    case 'clinic_info': return { name: VOICE_TASK_TOOLS.clinicInfo, arguments: interpretation.clinic ?? {} }
    case 'remember_preference': return { name: VOICE_TASK_TOOLS.rememberPreference, arguments: interpretation.preference ?? {} }
    case 'appointment_plan': break
  }
  const plan = interpretation.plan ?? {}
  const keys = Object.keys(plan)
  const only = (...wanted: string[]): boolean => keys.length === wanted.length && wanted.every((key) => keys.includes(key))
  if (only('search') && isRecord(plan.search) && plan.search.specialty && !plan.search.when) {
    return { name: VOICE_TASK_TOOLS.search, arguments: plan.search }
  }
  if (only('refine') && isRecord(plan.refine) && !plan.refine.when) return { name: VOICE_TASK_TOOLS.refine, arguments: plan.refine }
  if (only('show_for_approval')) return { name: VOICE_TASK_TOOLS.showForApproval, arguments: {} }
  if (only('choose', 'prepare') && isRecord(plan.choose) && ['number', 'time', 'doctor'].includes(String(plan.choose.strategy))) {
    const { strategy: _strategy, ...selection } = plan.choose
    return { name: VOICE_TASK_TOOLS.select, arguments: selection }
  }
  return { name: VOICE_TASK_TOOLS.plan, arguments: plan }
}

export interface ScriptedNarration {
  text: string
  /** Set when the output carried a result list the harness should remember. */
  results?: VoiceSlotFact[]
}

/** What the scripted voice says, built only from a function output's typed facts. */
export function narrateScripted(output: Json): ScriptedNarration {
  const facts = isRecord(output.facts) ? output.facts as unknown as VoiceNarration : undefined
  if (!facts) return { text: typeof output.message === 'string' ? output.message : 'Something went wrong.' }
  const booking = (fact: { doctor: string; day: string; time: string; price: number; currency: string }): string =>
    `${fact.doctor} on ${fact.day} at ${fact.time} for ${formatPrice(fact.price, fact.currency)}`
  switch (facts.kind) {
    case 'results': {
      if (facts.totalCount === 0) return { text: 'I could not find any appointments that match.', results: facts.slots }
      const list = facts.slots
        .map((slot) => `${slot.ordinal}. ${slot.doctor}, ${slot.day} ${slot.time}, ${formatPrice(slot.price, slot.currency)}`)
        .join('; ')
      const withdrawn = facts.invalidatedBooking ? ' The booking I had prepared no longer fits, so it was withdrawn.' : ''
      const remembered = facts.appliedPreferences?.length
        ? ` I used your saved ${facts.appliedPreferences.map((key) => key.replaceAll('_', ' ')).join(' and ')}.`
        : ''
      return {
        text: `I found ${facts.totalCount} appointment${facts.totalCount === 1 ? '' : 's'}: ${list}.${withdrawn}${remembered} Which one would you like?`,
        results: facts.slots
      }
    }
    case 'approval_ready':
      return { text: `${booking(facts.booking)} is ready but not booked. Please review it and press Approve and book.` }
    case 'approval_required':
      return { text: `I cannot approve bookings by voice. Please review ${booking(facts.booking)} on the booking card and press Approve and book yourself.` }
    case 'approved_not_booked':
      return { text: 'You approved it, but it is not booked yet. Press Book now on the card.' }
    case 'booking_in_progress':
      return { text: 'The booking is being submitted. I am waiting for the clinic site to confirm it.' }
    case 'booking_confirmed':
      return { text: `Your booking${facts.bookingId ? ` ${facts.bookingId}` : ''} is confirmed${facts.confirmedByLookup ? '. I found it on the clinic site and did not book again' : ''}.` }
    case 'booking_not_made':
      return { text: 'That booking was not made.' }
    case 'outcome_unknown':
      return { text: 'I do not know yet whether that booking went through, and I will not book again. Say check it, or press Check existing booking.' }
    case 'checking':
      return { text: 'I am checking the clinic site for the existing booking.' }
    case 'task_open':
      return { text: 'Your appointment task is open.' }
    case 'task_cancelled':
      return { text: 'I cancelled the appointment task. Nothing was booked.' }
    case 'needs_clarification':
      if (facts.reason === 'date_ambiguous' && facts.dateOptions) {
        return { text: `Did you mean ${facts.dateOptions.map((option) => formatCalendarDate(option.dateFrom)).join(' or ')}?` }
      }
      return { text: `I could not do that yet (${facts.reason.replaceAll('_', ' ')}).` }
    case 'refused':
      return { text: `I could not complete that (${facts.code.replaceAll('_', ' ')}).` }
    case 'chosen':
      return { text: `The ${facts.strategy} one is ${facts.slot.ordinal}. ${facts.slot.doctor}, ${facts.slot.day} ${facts.slot.time}, ${formatPrice(facts.slot.price, facts.slot.currency)}. It is not prepared yet.` }
    case 'clinic_info': {
      if (facts.profiles.length === 0) return { text: 'I could not find that doctor on the clinic site.' }
      const lines = facts.profiles.map((profile) => {
        switch (facts.topic) {
          case 'languages': return `${profile.doctor} speaks ${profile.languages.join(', ')}.`
          case 'hours': return `${profile.doctor} is available ${profile.hours}.`
          case 'fee': return `${profile.doctor}'s consultation fee is ${formatPrice(profile.consultationFee, profile.currency)}.`
          case 'address': return `${profile.doctor} is at ${profile.clinic}, ${profile.address}.`
          case 'walk_ins': return `${profile.doctor} ${profile.walkIns ? 'accepts walk-ins' : 'sees patients by appointment only'}.`
          case 'overview': return `${profile.doctor}, ${profile.clinic}, fee ${formatPrice(profile.consultationFee, profile.currency)}.`
        }
      })
      return { text: `${lines.join(' ')} That is from the clinic website.` }
    }
    case 'preference_saved':
      return { text: `I will remember that (${facts.preference.key.replaceAll('_', ' ')}: ${facts.preference.value}).` }
    case 'research':
      switch (facts.state) {
        case 'permission_required':
          return { text: 'I cannot allow public web research by voice. Please review the research card and press Allow research yourself.' }
        case 'awaiting_permission':
          return { text: 'The research card is waiting for you to allow public web research.' }
        case 'researching':
          return { text: 'I am reading public pages for that. The progress and the sources are on the research card.' }
        case 'answered':
          return { text: 'The answer is on the research card, with the public pages it came from.' }
        case 'not_verified':
          return { text: 'I could not verify that from the public pages I was able to read. The details are on the card.' }
        case 'stopped':
          return { text: 'The research is stopped. Nothing else will be opened.' }
        default:
          return { text: 'The research task is shown on the card.' }
      }
    case 'orchestration':
      if (facts.status === 'PAUSED') {
        return { text: 'The general task Lumi is working on needs your attention. Please check the task cockpit.' }
      }
      return { text: 'The general task Lumi is working on is shown in the task cockpit.' }
    case 'inspection':
      switch (facts.state) {
        case 'approval_required':
          return { text: `I cannot approve opening ${facts.host} by voice. Please review the inspection card and press Approve and inspect yourself.` }
        case 'awaiting_approval':
          return { text: `The inspection of ${facts.host} is waiting for you to approve it on the card.` }
        case 'answered':
          return { text: `The answer from ${facts.host} is on the inspection card, with the text it was based on.` }
        case 'not_verified':
          return { text: `I could not verify that from the page on ${facts.host}. The details are on the card.` }
        case 'unknown':
          return { text: `I do not know what was read from ${facts.host}, and I will not open it again by myself.` }
        default:
          return { text: `The inspection of ${facts.host} is shown on the card.` }
      }
  }
}
