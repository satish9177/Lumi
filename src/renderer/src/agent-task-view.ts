import type {
  AgentActionView,
  AgentBookingCriteria,
  AgentChangedFact,
  AgentEventView,
  AgentReconciliationView,
  AgentSlotView
} from '../../shared/agent-contracts'
import { formatCalendarDate, formatPrice } from '../../shared/scripted-voice'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'

/**
 * What the booking card says and which controls it offers, derived only from
 * persisted runtime state. Kept pure so the semantics are tested directly:
 *
 * - OUTCOME_UNKNOWN is never shown as a failure and never offers a retry.
 * - "Booking confirmed" / "No booking was created" appear only when the
 *   execution verified it or authoritative reconciliation established it.
 * - A changed price or missing slot shows the facts and requires a new review.
 */

export type BookingControl =
  | 'approve_and_book'
  | 'book_now'
  | 'reject'
  | 'request_approval'
  | 'check_booking'
  | 'discard_and_review'
  | 'review_updated'
  | 'search_again'

export type BookingTone = 'approval' | 'progress' | 'success' | 'failure' | 'uncertain' | 'neutral'

export interface BookingCardModel {
  tone: BookingTone
  eyebrow: string
  title: string
  lines: string[]
  changedFacts?: Array<{ label: string; approved: string; observed: string }>
  showBookingDetails: boolean
  controls: BookingControl[]
}

const FIELD_LABELS: Record<AgentChangedFact['field'], string> = {
  slot_id: 'Appointment',
  doctor: 'Doctor',
  time: 'Time',
  price: 'Price',
  currency: 'Currency'
}

export { formatPrice } from '../../shared/scripted-voice'

export function formatAppointmentTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString(undefined, {
    weekday: 'long', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit'
  })
}

function formatFact(fact: AgentChangedFact, currency: string): { label: string; approved: string; observed: string } {
  const format = (value: string): string => {
    if (fact.field === 'price' && /^\d+$/.test(value)) return formatPrice(Number(value), currency)
    if (fact.field === 'time') return formatAppointmentTime(value)
    return value
  }
  return { label: FIELD_LABELS[fact.field], approved: format(fact.approved), observed: format(fact.observed) }
}

export function isExpired(expiresAt: string | undefined, now: number): boolean {
  return expiresAt !== undefined && Date.parse(expiresAt) <= now
}

/** The latest reconciliation verdict recorded for this action, if any. */
export function latestReconciliation(action: AgentActionView, events: readonly AgentEventView[]): AgentReconciliationView | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'action.reconciled' && event.actionId === action.actionId) return event.reconciliation
  }
  return undefined
}

function lastAttempt(action: AgentActionView) {
  return action.attempts.length > 0 ? action.attempts[action.attempts.length - 1] : undefined
}

export function describeBooking(action: AgentActionView, events: readonly AgentEventView[], now: number): BookingCardModel {
  const reconciliation = latestReconciliation(action, events)
  const attempt = lastAttempt(action)
  const base = { showBookingDetails: true }
  switch (action.status) {
    case 'PROPOSED':
      return {
        ...base, tone: 'approval', eyebrow: 'PREPARED', title: 'Booking prepared',
        lines: ['Lumi has not asked for approval yet. Nothing has been booked.'],
        controls: ['request_approval', 'reject']
      }
    case 'WAITING_APPROVAL':
      if (!action.approval || action.approval.status !== 'PENDING' || isExpired(action.approval.expiresAt, now)) {
        return {
          ...base, tone: 'neutral', eyebrow: 'APPROVAL EXPIRED', title: 'This approval request expired',
          lines: ['Nothing was booked. Review the current details before booking.'],
          controls: ['discard_and_review']
        }
      }
      return {
        ...base, tone: 'approval', eyebrow: 'NEEDS YOUR APPROVAL', title: 'Book appointment',
        lines: ['Lumi will book exactly this appointment, once, only if you approve. If anything on the site changes, nothing is booked.'],
        controls: ['reject', 'approve_and_book']
      }
    case 'APPROVED':
      if (!action.approval || action.approval.status !== 'APPROVED' || isExpired(action.approval.expiresAt, now)) {
        return {
          ...base, tone: 'neutral', eyebrow: 'APPROVAL EXPIRED', title: 'Your approval expired before booking',
          lines: ['Nothing was booked. Review the current details before booking.'],
          controls: ['discard_and_review']
        }
      }
      return {
        ...base, tone: 'approval', eyebrow: 'APPROVED', title: 'Approved, not booked yet',
        lines: ['Your approval covers exactly this appointment and can be used once.'],
        controls: ['book_now', 'reject']
      }
    case 'REJECTED':
      return {
        ...base, tone: 'neutral', eyebrow: 'REJECTED', title: 'You rejected this booking',
        lines: ['Nothing was booked.'], controls: ['search_again']
      }
    case 'EXECUTING':
      return {
        ...base, tone: 'progress', eyebrow: 'BOOKING', title: 'Booking in progress',
        lines: ['Lumi is submitting this approved booking once and waiting for the clinic site to confirm it.'],
        controls: []
      }
    case 'OUTCOME_UNKNOWN': {
      const lines = [
        'The website may have accepted the booking, but Lumi did not receive reliable confirmation.',
        'Lumi will check for the existing booking before attempting anything else. It will not book again.'
      ]
      if (reconciliation?.result === 'OUTCOME_UNKNOWN') lines.push('The last check could not get a definite answer from the site.')
      return { ...base, tone: 'uncertain', eyebrow: 'OUTCOME UNKNOWN', title: 'Booking status uncertain', lines, controls: ['check_booking'] }
    }
    case 'RECONCILING':
      return {
        ...base, tone: 'uncertain', eyebrow: 'CHECKING', title: 'Checking the existing booking',
        lines: ['Lumi is looking up this booking on the clinic site. It is checking, not retrying — nothing will be booked by this step.'],
        controls: ['check_booking']
      }
    case 'SUCCEEDED': {
      if (reconciliation?.result === 'SUCCEEDED') {
        const id = reconciliation.bookingId ?? reconciliation.booking?.bookingId
        return {
          ...base, tone: 'success', eyebrow: 'CONFIRMED BY LOOKUP', title: 'Booking confirmed',
          lines: [
            id ? `Lumi found the existing booking ${id} on the clinic site.` : 'Lumi found the existing booking on the clinic site.',
            'No second booking was made.'
          ],
          controls: []
        }
      }
      const id = attempt?.result?.receipt?.bookingId ?? attempt?.result?.bookingId
      return {
        ...base, tone: 'success', eyebrow: 'CONFIRMED', title: 'Booking confirmed',
        lines: [id ? `The clinic site confirmed booking ${id}.` : 'The clinic site confirmed the booking.'],
        controls: []
      }
    }
    case 'FAILED': {
      if (reconciliation?.result === 'FAILED') {
        return {
          ...base, tone: 'failure', eyebrow: 'CONFIRMED BY LOOKUP', title: 'No booking was created',
          lines: ['Lumi checked the clinic site and found no booking for this request.'],
          controls: ['search_again']
        }
      }
      const changed = attempt?.result?.changedFacts
      if (changed && changed.length > 0) {
        return {
          ...base, tone: 'failure', eyebrow: 'CHANGED', title: 'The appointment changed after approval',
          lines: ['Nothing was booked.', 'Review the updated details before trying again.'],
          changedFacts: changed.map((fact) => formatFact(fact, action.booking.currency)),
          controls: ['review_updated', 'search_again']
        }
      }
      if (attempt?.result?.dispatchStatus === 'RESOURCE_UNAVAILABLE') {
        return {
          ...base, tone: 'failure', eyebrow: 'UNAVAILABLE', title: 'This appointment is no longer available',
          lines: ['Nothing was booked.'], controls: ['search_again']
        }
      }
      return {
        ...base, tone: 'failure', eyebrow: 'NOT BOOKED', title: 'The booking did not go through',
        lines: ['Lumi established that nothing was booked.'], controls: ['review_updated', 'search_again']
      }
    }
  }
}

export function describeEvent(event: AgentEventView): string {
  switch (event.type) {
    case 'task.created': return 'Task created'
    case 'task.cancelled': return 'Task cancelled'
    case 'task.criteria_updated':
      return event.invalidatedActionIds?.length
        ? 'Search changed — the prepared booking was withdrawn'
        : 'Search changed'
    case 'task.search_completed': {
      const count = event.searchResults?.length
      return count === undefined
        ? 'Searched the clinic site (read-only)'
        : `Searched the clinic site (read-only): ${count} matching appointment${count === 1 ? '' : 's'}`
    }
    case 'task.info_lookup_completed': {
      const count = event.profiles?.length
      return count === undefined
        ? 'Read clinic information (read-only)'
        : `Read clinic information (read-only): ${count} doctor profile${count === 1 ? '' : 's'}`
    }
    case 'action.proposed': return 'Booking prepared from the clinic site'
    case 'action.approval_requested': return 'Waiting for your approval'
    case 'action.approved': return 'You approved the booking'
    case 'action.rejected':
      if (event.reason === 'criteria_changed') return 'Booking withdrawn: the search changed'
      if (event.reason === 'task_cancelled') return 'Booking withdrawn: task cancelled'
      return 'Booking rejected'
    case 'action.execution_started': return `Booking submitted (attempt ${event.attemptNumber ?? 1})`
    case 'action.succeeded': return 'Booking confirmed by the clinic site'
    case 'action.failed': return 'Booking not made'
    case 'action.outcome_unknown':
      return event.reason === 'runtime_restart' ? 'Outcome uncertain after Lumi restarted' : 'Outcome uncertain'
    case 'action.reconciliation_started': return 'Checking the existing booking (not retrying)'
    case 'action.reconciled':
      switch (event.reconciliation?.result) {
        case 'SUCCEEDED': return 'Check complete: existing booking found'
        case 'FAILED': return 'Check complete: no booking was created'
        default: return 'Check complete: still uncertain'
      }
  }
}

/** Merge a page of events into the ordered, de-duplicated timeline. */
export function mergeEvents(current: readonly AgentEventView[], incoming: readonly AgentEventView[]): AgentEventView[] {
  const bySequence = new Map<number, AgentEventView>()
  for (const event of current) bySequence.set(event.sequence, event)
  for (const event of incoming) bySequence.set(event.sequence, event)
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence)
}

/** The booking the card focuses on: the newest one. */
export function currentBooking(actions: readonly AgentActionView[]): AgentActionView | undefined {
  return actions.length > 0 ? actions[actions.length - 1] : undefined
}

/**
 * The newest recorded search results, unless the constraints changed after
 * them. The same rule main uses to resolve a spoken selection.
 */
export function latestSearchResults(events: readonly AgentEventView[]): AgentSlotView[] | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'task.criteria_updated') return undefined
    if (event.type === 'task.search_completed') return event.searchResults
  }
  return undefined
}

export { formatCalendarDate } from '../../shared/scripted-voice'

export function describeCriteria(criteria: AgentBookingCriteria): string {
  const when = criteria.dateFrom && criteria.dateTo
    ? criteria.dateFrom === criteria.dateTo
      ? formatCalendarDate(criteria.dateFrom)
      : `${formatCalendarDate(criteria.dateFrom)} – ${formatCalendarDate(criteria.dateTo)}`
    : criteria.day || 'any day'
  const parts = [criteria.specialty || 'Any specialty', when]
  if (criteria.earliestTime && criteria.latestTime) parts.push(`${criteria.earliestTime}–${criteria.latestTime}`)
  else if (criteria.earliestTime) parts.push(`from ${criteria.earliestTime}`)
  else if (criteria.latestTime) parts.push(`until ${criteria.latestTime}`)
  if (criteria.maxPrice !== undefined && criteria.maxPriceCurrency) {
    parts.push(`up to ${formatPrice(criteria.maxPrice, criteria.maxPriceCurrency)}`)
  }
  return parts.join(' · ')
}

/** One app-authored sentence for a typed request's outcome. Never page text as instructions. */
export function describeOutcome(outcome: VoiceTaskOutcome): string {
  const narration = outcome.narration
  const stopped = outcome.plan?.find((step) => step.status === 'stopped')
  const suffix = stopped ? ` Stopped at the ${stopped.step.replaceAll('_', ' ')} step.` : ''
  switch (narration.kind) {
    case 'results':
      return `${narration.totalCount === 0 ? 'No appointments matched.' : `Found ${narration.totalCount} matching appointment${narration.totalCount === 1 ? '' : 's'}.`}` +
        `${narration.appliedPreferences?.length ? ' Used your saved preferences for missing details.' : ''}${suffix}`
    case 'chosen':
      return `Picked result ${narration.slot.ordinal} (${narration.strategy}): ${narration.slot.doctor}, ${narration.slot.day} ${narration.slot.time}, ${formatPrice(narration.slot.price, narration.slot.currency)}. Not prepared yet.`
    case 'approval_ready':
    case 'approval_required':
      return `Prepared ${narration.booking.doctor}, ${narration.booking.day} ${narration.booking.time}, ${formatPrice(narration.booking.price, narration.booking.currency)}. Nothing is booked until you press Approve and book.${suffix}`
    case 'clinic_info':
      return narration.profiles.length === 0 ? 'No matching doctor profile on the clinic site.' : `Read ${narration.profiles.length} doctor profile${narration.profiles.length === 1 ? '' : 's'} from the clinic site.`
    case 'preference_saved':
      return `Saved preference: ${narration.preference.key.replaceAll('_', ' ')} = ${narration.preference.value}.`
    case 'needs_clarification':
      if (narration.reason === 'date_ambiguous' && narration.dateOptions) {
        return `Which day did you mean: ${narration.dateOptions.map((option) => formatCalendarDate(option.dateFrom)).join(' or ')}?`
      }
      if (narration.reason === 'not_understood') return 'Lumi handles appointment and clinic questions here. Try "find a dermatologist tomorrow evening".'
      return `Lumi could not do that yet: ${narration.reason.replaceAll('_', ' ')}.${suffix}`
    case 'task_cancelled':
      return 'The task is cancelled. Nothing was booked.'
    case 'outcome_unknown':
      return 'Lumi does not know whether the booking went through. It will not book again; check it from the card.'
    case 'booking_confirmed':
      return 'The booking is confirmed by the clinic site.'
    case 'booking_not_made':
      return 'No booking was made.'
    case 'refused':
      return `Lumi could not complete that (${narration.code.replaceAll('_', ' ')}).`
    default:
      return 'Done. See the task below.'
  }
}

export function topicLabel(topic: string): string {
  return topic === 'walk_ins' ? 'Walk-ins' : topic[0].toUpperCase() + topic.slice(1)
}
