import type {
  AgentActionView,
  AgentBookingCriteria,
  AgentChangedFact,
  AgentDisclosureRecipient,
  AgentAuthenticatedPauseReason,
  AgentAuthenticatedView,
  AgentDisclosureCardView,
  AgentEventView,
  AgentFormPlanView,
  AgentProtectedDataKind,
  AgentInspectionView,
  AgentReconciliationView,
  AgentResearchSourceView,
  AgentResearchStopReason,
  AgentResearchView,
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
    // A booking always needs an exact approval; AUTHORIZED belongs to scoped
    // research steps and never appears on a booking action.
    case 'AUTHORIZED':
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

/**
 * The disclosure and hand-over approvals' events, worded truthfully. Nothing is booked. Since
 * Milestone 8b S6 the disclosure approval fills the form in the browser window with the network
 * frozen; a historical S5 approval (`approval_settled`) only ever recorded that nothing was done.
 */
function describeDisclosureEvent(event: AgentEventView): string | undefined {
  if (event.toolName === 'handover_form') {
    switch (event.type) {
      case 'action.proposed': return 'Hand-over requested — the network is still frozen'
      case 'action.approval_requested': return 'Waiting for your approval to hand the page over'
      case 'action.approved': return 'You approved handing the page over'
      case 'action.rejected': return event.reason === 'superseded' ? 'Hand-over replaced by a newer one' : 'You cancelled the hand-over'
      case 'action.execution_started': return 'Checking the page is still the draft you approved'
      case 'action.succeeded': return 'You took over in the browser window — Lumi did not submit anything'
      case 'action.failed': return 'The hand-over was refused — the network stayed frozen'
      case 'action.outcome_unknown': return 'Lumi lost the answer while handing over and does not know whether the network came back'
      default: return undefined
    }
  }
  if (event.toolName !== 'prepare_form') return undefined
  const historical = event.reason === 'approval_settled'
  switch (event.type) {
    case 'action.proposed': return 'Form plan prepared — nothing has been changed'
    case 'action.approval_requested': return 'Waiting for your approval of this form plan'
    case 'action.approved': return 'You approved this form plan'
    case 'action.rejected': return event.reason === 'superseded' || event.reason === 'preparation_mode' ? 'Form plan replaced by a newer one' : 'You declined this form plan'
    case 'action.execution_started': return historical ? 'Approval recorded — no page action exists yet' : 'Approval recorded — freezing the network, then filling the fields'
    case 'action.succeeded':
      return historical
        ? 'Form plan approved — nothing was prepared in the page'
        : 'Lumi filled the form in the browser window with the network frozen and checked every field'
    case 'action.failed': return 'Lumi could not finish filling the form — nothing was sent'
    case 'action.outcome_unknown': return 'Lumi lost track of the browser window — any draft is lost, and nothing was sent while it was frozen'
    default: return undefined
  }
}

export function describeEvent(event: AgentEventView): string {
  const disclosure = describeDisclosureEvent(event)
  if (disclosure) return disclosure
  switch (event.type) {
    case 'task.created': return 'Task created'
    // Milestone 9 S2. A desktop read has its own trusted view and is never shown in this timeline; the
    // words exist so the switch stays exhaustive. They carry no desktop text: the events do not either.
    case 'task.desktop_disclosure_requested': return 'Desktop snapshot ready for your approval — nothing was sent'
    case 'task.desktop_disclosure_granted': return 'You allowed this one snapshot to be sent once'
    case 'task.desktop_disclosure_revoked': return 'Desktop disclosure cancelled'
    case 'task.desktop_disclosure_started': return 'The approved snapshot was released to one AI provider'
    case 'task.desktop_answer_recorded': return 'A grounded answer about the snapshot was recorded'
    case 'task.desktop_disclosure_failed': return 'The AI provider could not answer — nothing was retried'
    case 'task.desktop_disclosure_outcome_unknown': return 'Lumi cannot tell whether the snapshot reached the AI provider — it was not repeated'
    // Milestone 9 S4. A desktop action plan has its own trusted view (DesktopPlanningPanel) and is
    // never shown in this timeline either, for the same reason as the S2 disclosure events above: the
    // words exist so the switch stays exhaustive. They carry no desktop text or raw value.
    case 'task.desktop_plan_requested': return 'Desktop action plan ready for your approval — nothing was sent'
    case 'task.desktop_plan_granted': return 'You allowed this one snapshot to be sent once, to propose a step'
    case 'task.desktop_plan_revoked': return 'Desktop action plan cancelled'
    case 'task.desktop_plan_started': return 'The approved snapshot was released to one AI provider'
    case 'task.desktop_plan_action_recorded': return 'The AI provider proposed one step — nothing has run'
    case 'task.desktop_plan_failed': return 'The AI provider could not propose a step — nothing was retried'
    case 'task.desktop_plan_outcome_unknown': return 'Lumi cannot tell whether the snapshot reached the AI provider — it was not repeated'
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
    case 'task.page_answer_recorded':
      return event.answerStatus === 'answered'
        ? 'Answer recorded from the inspected page'
        : 'Recorded: could not verify an answer from the inspected page'
    case 'task.info_lookup_completed': {
      const count = event.profiles?.length
      return count === undefined
        ? 'Read clinic information (read-only)'
        : `Read clinic information (read-only): ${count} doctor profile${count === 1 ? '' : 's'}`
    }
    case 'task.research_scope_requested': return 'Research permission prepared — nothing searched yet'
    case 'task.research_scope_granted': return 'You allowed public research for this task'
    case 'task.research_scope_revoked': return 'Research permission withdrawn'
    case 'task.research_answer_recorded':
      return event.answerStatus === 'answered'
        ? 'Answer recorded from the pages Lumi read'
        : 'Recorded: could not verify an answer from the pages Lumi read'
    case 'task.authenticated_scope_requested': return 'Account-reading permission prepared — nothing opened yet'
    case 'task.authenticated_scope_granted': return 'You allowed account reading for this question'
    case 'task.authenticated_scope_revoked': return 'Account-reading permission withdrawn'
    case 'task.authenticated_answer_recorded':
      return event.answerStatus === 'answered'
        ? 'Answer recorded from your account pages'
        : 'Recorded: could not verify an answer from your account pages'
    case 'task.authenticated_paused': return 'Paused — Lumi stopped and sent nothing further to an AI'
    case 'task.authenticated_resumed': return 'Resumed'
    case 'task.form_prepare_scope_requested': return 'Form-planning permission prepared — nothing sent yet'
    case 'task.form_prepare_scope_granted': return 'You allowed form planning'
    case 'task.form_prepare_scope_revoked': return 'Form-planning permission withdrawn'
    case 'task.form_planning_context_built': return 'Form structure and masked previews prepared for the one approved AI'
    case 'task.form_preparation_started': return 'The preparation window opened and the form was looked at again'
    case 'task.form_draft_recorded': return 'Lumi filled the form in the browser window with the network frozen and checked it'
    case 'task.form_draft_discarded': return 'You discarded the draft; the page was destroyed while frozen'
    case 'task.form_draft_handed_over': return 'You took over in the browser window; Lumi did not submit anything'
    case 'task.form_draft_lost': return 'The browser window with the draft is gone; the draft is lost'
    case 'action.proposed': return 'Booking prepared from the clinic site'
    case 'action.approval_requested': return 'Waiting for your approval'
    case 'action.approved': return 'You approved the booking'
    case 'action.authorized': return 'Research step authorised by the permission you gave'
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
    case 'research':
      switch (narration.state) {
        case 'permission_required':
          return 'Nothing was allowed. Review the research card and press Allow research yourself.'
        case 'awaiting_permission':
          return 'Prepared a public research task. Nothing is searched or opened until you press Allow research.'
        case 'researching':
          return 'Lumi is reading public pages. Progress and sources are on the research card.'
        case 'answered':
          return 'The answer and the public pages it came from are on the research card.'
        case 'not_verified':
          return 'Lumi could not verify that from the public pages it read. See the research card.'
        case 'stopped':
          return 'The research is stopped. Nothing else will be opened.'
        default:
          return 'See the research card.'
      }
    case 'inspection':
      return narration.state === 'approval_required'
        ? `Nothing was approved. Review the card for ${narration.host} and press Approve and inspect yourself.`
        : narration.state === 'awaiting_approval'
          ? `Prepared an inspection of ${narration.host}. Nothing is opened until you press Approve and inspect.`
          : `See the inspection card for ${narration.host}.`
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


// ---- Milestone 7b: the public research card ------------------------------------------

/**
 * The research card is built from the persisted scope, the durable progress
 * and the recorded answer only. Page-controlled text appears in exactly three
 * places, always as plain text in a labelled field: the quoted evidence of a
 * verified answer, a source's title, and a source's address. It never supplies
 * a label, a control, a line of instructions or a clickable link.
 */

export type ResearchControl = 'allow_research' | 'decline_research' | 'run_research' | 'stop_research'

export interface ResearchCardModel {
  tone: BookingTone
  eyebrow: string
  title: string
  lines: string[]
  /** Show the scope: what is allowed, what is not, and the limits. */
  showScope: boolean
  showProgress: boolean
  controls: ResearchControl[]
}

/** Lumi's own words for each machine-readable scope entry. Never a page's. */
export const RESEARCH_ALLOWED_LABELS: Record<string, string> = {
  public_search: 'search the public web',
  public_https_navigation: 'open public https pages',
  follow_public_links: 'follow links on those pages',
  read_page_text: 'read the visible text of those pages',
  task_owned_tabs: 'open and close its own research tabs'
}

export const RESEARCH_FORBIDDEN_LABELS: Record<string, string> = {
  login: 'sign in anywhere',
  forms_and_typing: 'type into or submit any form',
  uploads_and_downloads: 'upload or download anything',
  purchases_and_payments: 'buy anything or make a payment',
  messages: 'send messages',
  files: 'read or change your files',
  private_network: 'reach private or local network addresses',
  non_get_requests: 'send anything but read requests'
}

export function describeScopeEntry(entry: string, allowed: boolean): string {
  const labels = allowed ? RESEARCH_ALLOWED_LABELS : RESEARCH_FORBIDDEN_LABELS
  return labels[entry] ?? entry.replaceAll('_', ' ')
}

const RESEARCH_STOP_LINES: Record<AgentResearchStopReason, string> = {
  goal_reached: 'Lumi found what you asked for.',
  no_evidence: 'The public pages Lumi could read did not show it.',
  budget_exhausted: 'Lumi reached the limit for this task and stopped.',
  blocked: 'Lumi was blocked before it could finish.',
  planner_failed: 'Lumi could not decide a safe next step.',
  user_stopped: 'You stopped the research.',
  outside_scope: 'Finishing this would have needed something research is not allowed to do.'
}

export function describeResearch(research: AgentResearchView, now: number): ResearchCardModel {
  const grant = research.grant
  const answer = research.answer
  const base = { showScope: false, showProgress: false }
  if (!grant) {
    return {
      ...base, tone: 'neutral', eyebrow: 'RESEARCH', title: 'No research permission yet',
      lines: ['Nothing has been searched or opened.'], controls: []
    }
  }
  if (answer) {
    const stopLine = RESEARCH_STOP_LINES[answer.stopReason]
    if (answer.status === 'answered' || answer.status === 'partial') {
      return {
        ...base, showProgress: true, tone: 'success',
        eyebrow: answer.status === 'answered' ? 'ANSWER FROM PUBLIC PAGES' : 'PARTIAL ANSWER',
        title: answer.status === 'answered' ? 'Answer from the pages Lumi read' : 'Part of the answer',
        lines: answer.status === 'answered' ? [] : [stopLine],
        controls: []
      }
    }
    return {
      ...base, showProgress: true, tone: 'neutral', eyebrow: 'NOT VERIFIED',
      title: 'Lumi could not verify that from public pages',
      lines: [stopLine], controls: []
    }
  }
  switch (grant.status) {
    case 'PENDING':
      return {
        ...base, showScope: true, tone: 'approval', eyebrow: 'NEEDS YOUR PERMISSION',
        title: 'Allow Lumi to research public websites for this task?',
        lines: [
          'Lumi searches and reads public pages in an isolated browser, and stops there.',
          'It is not signed in to anything, and this permission lasts for this task only.'
        ],
        controls: ['decline_research', 'allow_research']
      }
    case 'ACTIVE': {
      const expired = grant.expiresAt !== undefined && Date.parse(grant.expiresAt) <= now
      if (expired) {
        return {
          ...base, showProgress: true, tone: 'neutral', eyebrow: 'PERMISSION EXPIRED',
          title: 'The research permission expired',
          lines: ['Nothing else will be opened. Start the request again to continue.'],
          controls: ['stop_research']
        }
      }
      const running = research.usage.steps > 0
      if (research.unresolvedStep) {
        // A step is still in flight, or ended without a result Lumi can stand
        // behind. Say that plainly rather than showing confident progress.
        return {
          ...base, showProgress: research.observations.length > 0, tone: 'neutral',
          eyebrow: 'STEP UNRESOLVED', title: 'Lumi does not know what its last step did',
          lines: [
            'The browser step is still running, or it ended without a result Lumi can stand behind.',
            'Nothing new will be opened until that is settled. Anything already read is kept below.'
          ],
          controls: ['stop_research']
        }
      }
      return {
        ...base, showScope: true, showProgress: true, tone: running ? 'progress' : 'approval',
        eyebrow: 'RESEARCHING', title: running ? 'Reading public pages' : 'Ready to research',
        lines: running
          ? []
          : ['Lumi will search, open public pages and read them. It will stop at its limits and tell you what it found.'],
        controls: running ? ['stop_research'] : ['run_research', 'stop_research']
      }
    }
    case 'REVOKED':
      return {
        ...base, showProgress: research.observations.length > 0, tone: 'neutral', eyebrow: 'STOPPED',
        title: 'Research stopped',
        lines: ['Nothing else will be opened. Anything Lumi already read is kept below.'],
        controls: []
      }
    case 'EXPIRED':
      return {
        ...base, showProgress: research.observations.length > 0, tone: 'neutral',
        eyebrow: 'PERMISSION EXPIRED', title: 'The research permission expired',
        lines: ['Nothing else will be opened.'], controls: []
      }
    case 'COMPLETED':
      return {
        ...base, showProgress: true, tone: 'neutral', eyebrow: 'FINISHED',
        title: 'Research finished', lines: [], controls: []
      }
  }
}

/** One short progress line, in Lumi's words, from durable counters only. */
export function describeResearchProgress(research: AgentResearchView): string {
  const pages = researchPageCount(research)
  const searches = research.observations.filter((observation) => observation.kind === 'search_results').length
  const parts: string[] = []
  if (searches > 0) parts.push(`${searches} search${searches === 1 ? '' : 'es'}`)
  parts.push(`${pages} page${pages === 1 ? '' : 's'} read`)
  const budgets = research.grant?.scope.budgets
  parts.push(`${research.usage.steps}${budgets ? ` of ${budgets.maxSteps}` : ''} step${research.usage.steps === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

export function researchPageCount(research: AgentResearchView): number {
  return new Set(
    research.observations
      .filter((observation) => observation.kind === 'page' && observation.finalUrl)
      .map((observation) => observation.finalUrl)
  ).size
}

/** The pages Lumi actually opened, in order: the answer's sources. */
export function researchSources(research: AgentResearchView): AgentResearchSourceView[] {
  const sources: AgentResearchSourceView[] = []
  const seen = new Set<string>()
  for (const observation of research.observations) {
    if (observation.kind !== 'page' || !observation.finalUrl || seen.has(observation.finalUrl)) continue
    seen.add(observation.finalUrl)
    sources.push({
      ref: observation.ref,
      url: observation.finalUrl,
      host: observation.finalHost ?? '',
      title: observation.title,
      observedAt: observation.observedAt
    })
  }
  return sources
}

// ---- Milestone 8a S3: the authenticated account-reading card -------------------------

/**
 * The disclosure card for reading a signed-in account. Every word on it is
 * Lumi's own, written here, and it is built only from the persisted scope and
 * the trusted profile record: never from a page, an observation, a model or a
 * renderer payload. The profile *label* is the user's own text and the site is
 * the profile's bound registrable domain; neither is website content.
 *
 * The honest name for what this does is `account_scoped_read`. It is never
 * described as read-only, invisible or free of effects: the card says, before
 * the Allow button, that a visit can change website state.
 */

export type AuthenticatedControl =
  | 'allow_account_reading'
  | 'decline_account_reading'
  | 'run_account_reading'
  | 'stop_account_reading'

export interface AuthenticatedCardModel {
  tone: BookingTone
  eyebrow: string
  title: string
  lines: string[]
  /** Show the full disclosure: only while the permission is waiting for a click. */
  showDisclosure: boolean
  showProgress: boolean
  controls: AuthenticatedControl[]
}

/** The disclosure, section by section, for the trusted card. Pure text. */
export interface AuthenticatedDisclosure {
  heading: string
  profileLabel: string
  site: string
  mayDo: string[]
  mayNot: string[]
  /** Shown before Allow, never after: a page visit is not invisible. */
  sideEffectNotice: string
  sideEffectExamples: string
  sent: string[]
  provider: string
  failoverNotice: string
}

const AUTH_MAY_DO = (site: string): string[] => [
  `read pages on ${site}`,
  `follow links within ${site}`,
  'open and close its own account-reading tabs'
]

const AUTH_MAY_NOT = (site: string): string[] => [
  'sign in for you',
  'ask for your password or one-time code',
  'type into forms',
  'submit anything',
  `leave ${site}`,
  'upload or download files',
  'buy, send, post or message anything'
]

/** `github.com` -> `GITHUB`. The site name, never a page's title. */
export function siteHeadline(site: string): string {
  return (site.split('.')[0] || site).toUpperCase()
}

export function describeAuthenticatedDisclosure(view: AgentAuthenticatedView): AuthenticatedDisclosure | undefined {
  const scope = view.grant?.scope
  const profile = view.profile
  if (!scope || !profile) return undefined
  return {
    heading: `READ YOUR ${siteHeadline(scope.site)} ACCOUNT`,
    profileLabel: profile.label,
    site: scope.site,
    mayDo: AUTH_MAY_DO(scope.site),
    mayNot: AUTH_MAY_NOT(scope.site),
    sideEffectNotice:
      'Reading an account page may change website state, such as marking something as read, updating "last active", extending your session, or recording the visit.',
    sideEffectExamples:
      'Reading an account page is not invisible to the website. A page visit may mark something as read, update "last active", extend your session, or appear in account activity. Lumi cannot prevent that.',
    sent: [
      'your question',
      `up to ${scope.maxTextChars.toLocaleString('en-US')} characters from account pages`,
      'with email addresses, phone numbers and long numbers hidden first. That reduces what is sent; it does not make it anonymous.'
    ],
    provider: RECIPIENT_LABELS[scope.recipient],
    failoverNotice:
      'If that provider is unavailable, Lumi stops. It does not send the private page to another provider.'
  }
}

const AUTH_PAUSE_LINES: Record<AgentAuthenticatedPauseReason, { title: string; lines: string[] }> = {
  login_required: {
    title: 'The website asked you to sign in again',
    lines: [
      'Lumi paused and sent nothing from that page to an AI.',
      'Use "Sign in manually" for this profile, then ask again.'
    ]
  },
  account_changed: {
    title: 'A different account is signed in',
    lines: [
      'Lumi paused and sent nothing from that page to an AI. Your earlier permission no longer applies.',
      'Sign in again with "Sign in manually", then review a new permission card.'
    ]
  },
  account_identity_unknown: {
    title: 'Lumi cannot tell which account this is',
    lines: [
      'Lumi paused and sent nothing from that page to an AI.',
      'It only reads a page when it can confirm the account is the one you allowed.'
    ]
  },
  left_site_scope: {
    title: 'The page tried to leave the site',
    lines: [
      'Lumi stopped. It did not open the other address, and it did not hand it to anything else.'
    ]
  },
  form_draft: {
    title: 'A form is prepared in the browser window',
    lines: [
      'Lumi is waiting for you. Discard the draft, or hand the page over to yourself.',
      'Lumi has not submitted anything.'
    ]
  },
  user_takeover: {
    title: 'You took over in the browser window',
    lines: [
      'Lumi did not submit anything and cannot tell you whether the site accepted or saved it.'
    ]
  },
  browser_lost: {
    title: 'The browser window with your draft is gone',
    lines: [
      'The values were only in that window, so they are lost. Nothing was restored or filled again.',
      'To try again, prepare the form again and approve it again.'
    ]
  }
}

const AUTH_STOP_LINES: Record<AgentResearchStopReason, string> = {
  goal_reached: 'Lumi found what you asked for.',
  no_evidence: 'The account pages Lumi could read did not show it.',
  budget_exhausted: 'Lumi reached the limit for this task and stopped.',
  blocked: 'Lumi was blocked before it could finish.',
  planner_failed: 'Lumi could not decide a safe next step.',
  user_stopped: 'You stopped the account reading.',
  outside_scope: 'Finishing this would have needed something account reading is not allowed to do.'
}

export function describeAuthenticated(view: AgentAuthenticatedView, now: number): AuthenticatedCardModel {
  const grant = view.grant
  const answer = view.answer
  const base = { showDisclosure: false, showProgress: false }
  if (!grant) {
    return {
      ...base, tone: 'neutral', eyebrow: 'ACCOUNT', title: 'No account-reading permission yet',
      lines: ['Nothing has been opened.'], controls: []
    }
  }
  if (answer) {
    const stopLine = AUTH_STOP_LINES[answer.stopReason]
    if (answer.status === 'answered' || answer.status === 'partial') {
      return {
        ...base, showProgress: true, tone: 'success',
        eyebrow: answer.status === 'answered' ? 'ANSWER FROM YOUR ACCOUNT' : 'PARTIAL ANSWER',
        title: answer.status === 'answered' ? 'Answer from the account pages Lumi read' : 'Part of the answer',
        lines: answer.status === 'answered' ? [] : [stopLine], controls: []
      }
    }
    return {
      ...base, showProgress: true, tone: 'neutral', eyebrow: 'NOT VERIFIED',
      title: 'Lumi could not verify that from your account pages', lines: [stopLine], controls: []
    }
  }
  if (view.pauseReason) {
    const pause = AUTH_PAUSE_LINES[view.pauseReason]
    return {
      ...base, showProgress: view.observations.length > 0, tone: 'uncertain', eyebrow: 'PAUSED',
      title: pause.title, lines: pause.lines, controls: ['stop_account_reading']
    }
  }
  switch (grant.status) {
    case 'PENDING':
      return {
        ...base, showDisclosure: true, tone: 'approval', eyebrow: 'NEEDS YOUR PERMISSION',
        title: 'Allow Lumi to read this account for this question?',
        lines: [], controls: ['decline_account_reading', 'allow_account_reading']
      }
    case 'ACTIVE': {
      if (grant.expiresAt !== undefined && Date.parse(grant.expiresAt) <= now) {
        return {
          ...base, showProgress: true, tone: 'neutral', eyebrow: 'PERMISSION EXPIRED',
          title: 'The account-reading permission expired',
          lines: ['Nothing else will be opened. Ask again to review a new permission card.'],
          controls: ['stop_account_reading']
        }
      }
      if (view.unresolvedStep) {
        return {
          ...base, showProgress: view.observations.length > 0, tone: 'neutral',
          eyebrow: 'STEP UNRESOLVED', title: 'Lumi does not know what its last step did',
          lines: [
            'A page visit may have reached the website, and Lumi cannot tell whether it recorded it.',
            'Lumi will look at the page again before doing anything else. It does not repeat the step.'
          ],
          controls: ['run_account_reading', 'stop_account_reading']
        }
      }
      const running = view.usage.steps > 0
      return {
        ...base, showProgress: true, tone: running ? 'progress' : 'approval',
        eyebrow: 'READING', title: running ? 'Reading your account pages' : 'Ready to read',
        lines: running ? [] : ['Lumi will open pages on the site you allowed, one at a time, and answer from what they show.'],
        controls: running ? ['run_account_reading', 'stop_account_reading'] : ['run_account_reading', 'stop_account_reading']
      }
    }
    case 'REVOKED':
      return {
        ...base, showProgress: view.observations.length > 0, tone: 'neutral', eyebrow: 'STOPPED',
        title: 'Account reading stopped',
        lines: ['Nothing else will be opened. Nothing was sent to an AI after you stopped.'], controls: []
      }
    case 'EXPIRED':
      return {
        ...base, showProgress: view.observations.length > 0, tone: 'neutral',
        eyebrow: 'PERMISSION EXPIRED', title: 'The account-reading permission expired',
        lines: ['Nothing else will be opened.'], controls: []
      }
    case 'COMPLETED':
      return {
        ...base, showProgress: true, tone: 'neutral', eyebrow: 'FINISHED',
        title: 'Account reading finished', lines: [], controls: []
      }
  }
}

/** One short progress line, in Lumi's words, from durable counters only. */
export function describeAuthenticatedProgress(view: AgentAuthenticatedView): string {
  const pages = view.observations.filter((observation) => observation.kind === 'page').length
  const budgets = view.grant?.scope.budgets
  return `${pages} page${pages === 1 ? '' : 's'} read · ${view.usage.steps}${budgets ? ` of ${budgets.maxSteps}` : ''} step${view.usage.steps === 1 ? '' : 's'}`
}

/** How many identifiers were hidden before sending, across everything read. */
export function authenticatedRedactionCount(view: AgentAuthenticatedView): number {
  return view.observations.reduce(
    (total, observation) => total + Object.values(observation.redactions).reduce((sum, count) => sum + count, 0), 0
  )
}

export function topicLabel(topic: string): string {
  return topic === 'walk_ins' ? 'Walk-ins' : topic[0].toUpperCase() + topic.slice(1)
}

// ---- Milestone 7a: page inspection card ---------------------------------------------

/**
 * The inspection card is built from the persisted proposal and runtime state
 * only. Page-controlled text (verified answer quotes, page title, final URL)
 * is rendered only as plain text in labelled fields. Page text never supplies a
 * label, a button, or a line of instructions on this card.
 */

export type InspectionControl =
  | 'approve_and_inspect'
  | 'inspect_now'
  | 'reject'
  | 'answer_from_observation'
  | 'inspect_again'

export interface InspectionCardModel {
  tone: BookingTone
  eyebrow: string
  title: string
  lines: string[]
  showProposal: boolean
  controls: InspectionControl[]
}

export const RECIPIENT_LABELS: Record<AgentDisclosureRecipient, string> = {
  openai: 'OpenAI',
  gemini: 'Google Gemini',
  deepseek: 'DeepSeek',
  scripted: 'Lumi offline test model'
}

export function describeRecipients(recipients: readonly AgentDisclosureRecipient[]): string {
  return recipients.map((recipient) => RECIPIENT_LABELS[recipient]).join(', ')
}

const FAILURE_REASONS: Record<string, string> = {
  redirect_blocked: 'The page redirected to an address Lumi may not open. That address was not contacted.',
  too_many_redirects: 'The page redirected too many times.',
  navigation_blocked: 'The page tried to move itself to an address Lumi may not open. That address was not contacted.',
  final_destination_not_allowed: 'The page ended up at an address Lumi may not read.',
  download_blocked: 'The address is a file download. Lumi does not download files.',
  unsupported_content_type: 'The address is not a web page Lumi can read.',
  document_unstable: 'The page kept changing while Lumi was reading it.',
  observation_invalid: 'What the browser returned could not be used as evidence.',
  non_public_address: 'The site resolved to a private or local network address.',
  dns_failed: 'The site could not be found.',
  destination_not_allowed: 'The site is no longer on the list of sites Lumi may inspect.',
  timeout_before_submission: 'The page did not load in time.',
  browser_error_before_submission: 'The browser could not load the page.',
  browser_closed_before_submission: 'The browser closed before the page loaded.',
  connection_refused_before_submission: 'The site refused the connection.',
  browser_worker_unavailable: 'The isolated browser was not available.',
  browser_worker_rejected: 'The isolated browser refused the request.',
  dispatch_not_recorded: 'Lumi could not record the read before starting it, so it did not start it.'
}

export function describeInspectionFailure(errorCode: string | undefined, httpStatus?: number): string {
  if (errorCode === 'page_http_error') return `The site answered with an error${httpStatus ? ` (HTTP ${httpStatus})` : ''}.`
  return (errorCode && FAILURE_REASONS[errorCode]) || 'The page could not be read.'
}

const NOT_ANSWERED_REASONS: Record<string, string> = {
  not_found: 'The inspected page did not show it.',
  ambiguous: 'The inspected page showed conflicting or unclear values.',
  not_verified: 'A text model replied, but its answer could not be matched to the page, so it is not shown.'
}

export function describeInspection(inspection: AgentInspectionView, now: number): InspectionCardModel {
  const attempt = inspection.attempts.length > 0 ? inspection.attempts[inspection.attempts.length - 1] : undefined
  const base = { showProposal: true }
  switch (inspection.status) {
    case 'PROPOSED':
    case 'AUTHORIZED':
      return {
        ...base, tone: 'approval', eyebrow: 'PREPARED', title: 'Inspection prepared',
        lines: ['Lumi has not asked for approval yet. Nothing has been opened.'], controls: ['inspect_again', 'reject']
      }
    case 'WAITING_APPROVAL':
      if (!inspection.approval || inspection.approval.status !== 'PENDING' || isExpired(inspection.approval.expiresAt, now)) {
        return {
          ...base, tone: 'neutral', eyebrow: 'APPROVAL EXPIRED', title: 'This approval request expired',
          lines: ['Nothing was opened.'], controls: ['inspect_again']
        }
      }
      return {
        ...base, tone: 'approval', eyebrow: 'NEEDS YOUR APPROVAL', title: 'Open and read this page?',
        lines: ['Lumi will open exactly this address once, in an isolated browser, only if you approve.'],
        controls: ['reject', 'approve_and_inspect']
      }
    case 'APPROVED':
      if (!inspection.approval || inspection.approval.status !== 'APPROVED' || isExpired(inspection.approval.expiresAt, now)) {
        return {
          ...base, tone: 'neutral', eyebrow: 'APPROVAL EXPIRED', title: 'Your approval expired before the page was opened',
          lines: ['Nothing was opened.'], controls: ['inspect_again']
        }
      }
      return {
        ...base, tone: 'approval', eyebrow: 'APPROVED', title: 'Approved, not opened yet',
        lines: ['Your approval covers exactly this address and can be used once.'], controls: ['inspect_now', 'reject']
      }
    case 'REJECTED':
      return {
        ...base, tone: 'neutral', eyebrow: 'REJECTED', title: 'You rejected this inspection',
        lines: ['Nothing was opened.'], controls: ['inspect_again']
      }
    case 'EXECUTING':
      return {
        ...base, tone: 'progress', eyebrow: 'READING', title: 'Reading the page',
        lines: ['Lumi is inspecting this page once in an isolated browser — no clicks, form submissions, uploads, downloads, or non-GET requests.'], controls: []
      }
    case 'OUTCOME_UNKNOWN':
    case 'RECONCILING':
      return {
        ...base, tone: 'uncertain', eyebrow: 'RESULT UNKNOWN', title: 'Lumi does not know what was read',
        lines: [
          'The page may have been opened, but Lumi did not receive or save what it read. Nothing was answered.',
          'Inspecting a page sends no form submissions or other non-GET requests, so there is no action for Lumi to check. Lumi will not retry by itself; a new inspection needs a new approval.'
        ],
        controls: ['inspect_again']
      }
    case 'FAILED':
      return {
        ...base, tone: 'failure', eyebrow: 'NOT READ', title: 'The page was not read',
        lines: [describeInspectionFailure(attempt?.errorCode, attempt?.httpStatus), 'Nothing was answered.'],
        controls: ['inspect_again']
      }
    case 'SUCCEEDED': {
      const answer = inspection.answer
      if (!answer) {
        return {
          ...base, tone: 'neutral', eyebrow: 'PAGE READ', title: 'Page read — no answer yet',
          lines: ['Lumi saved what it read. Answering uses that saved copy and does not open the page again.'],
          controls: ['answer_from_observation', 'inspect_again']
        }
      }
      if (answer.status === 'answered') {
        return {
          ...base, tone: 'success', eyebrow: 'ANSWER FROM THE PAGE', title: 'Answer from the inspected page',
          lines: [], controls: ['inspect_again']
        }
      }
      return {
        ...base, tone: 'neutral', eyebrow: 'NOT VERIFIED', title: 'Could not verify this from the inspected page.',
        lines: [NOT_ANSWERED_REASONS[answer.status] ?? 'Lumi could not verify an answer.'], controls: ['inspect_again']
      }
    }
  }
}


// ---- Milestone 8b S5: form planning and the exact disclosure card --------------------
//
// **S5 changes no website.** Both cards are written here, by Lumi, from the runtime's
// persisted state -- never from page text, a provider's words or a renderer-side
// value. A masked preview arrives already masked: this module never masks, never
// sees a raw saved value and never builds a manifest.

export type FormPlanControl =
  | 'plan_form'
  | 'allow_form_planning'
  | 'decline_form_planning'
  | 'approve_disclosure'
  | 'decline_disclosure'
  // Milestone 8b S6
  | 'start_preparation'
  | 'discard_draft'
  | 'request_handover'
  | 'approve_handover'
  | 'decline_handover'

export const DATA_KIND_LABELS: Record<AgentProtectedDataKind, string> = {
  legal_name: 'legal name',
  preferred_name: 'preferred name',
  email: 'email',
  phone: 'phone',
  city: 'city',
  country: 'country',
  linkedin_url: 'LinkedIn link',
  portfolio_url: 'portfolio link'
}

export type FormPlanStage =
  | 'hidden' | 'prepare_offer' | 'offer' | 'permission' | 'ready' | 'approval' | 'prepared' | 'declined'
  // Milestone 8b S6: a local draft waits frozen; the second approval; after it; not written.
  | 'draft' | 'handover' | 'handed_over' | 'handover_unknown' | 'not_written' | 'superseded'

export interface FormPlanModel {
  stage: FormPlanStage
  eyebrow: string
  title: string
  /** Plain lines, all Lumi's own. */
  lines: string[]
  /** `permission` stage: what is sent, what is not, who receives it. */
  permission?: {
    site: string
    provider: string
    sent: string[]
    notSent: string[]
    countryNotice?: string
    savedDetails: Array<{ kind: AgentProtectedDataKind; label: string }>
    cannotAct: string
  }
  /** `offer` stage: the saved details the user may choose to plan with. */
  offerable: Array<{ kind: AgentProtectedDataKind; label: string; preview: string }>
  controls: FormPlanControl[]
}

const FORM_PLAN_HIDDEN: FormPlanModel = { stage: 'hidden', eyebrow: '', title: '', lines: [], offerable: [], controls: [] }

/**
 * Which stage of form planning applies. `accountReadingActive` is whether the
 * account-reading permission is currently usable: planning only starts from it.
 */
export function describeFormPlan(
  plan: AgentFormPlanView | undefined, accountReadingActive: boolean, taskClosed: boolean, now: number
): FormPlanModel {
  if (!plan) return FORM_PLAN_HIDDEN
  const disclosure = plan.disclosure
  const grant = plan.grant
  const draft = plan.draft
  const handover = plan.handover
  const expired = grant?.expiresAt !== undefined && Date.parse(grant.expiresAt) <= now
  const draftLive = draft !== undefined && (draft.status === 'PREPARED' || draft.status === 'STALE')
  // ---- the local draft and its handover (Milestone 8b S6) ----
  if (handover && handover.actionStatus === 'WAITING_APPROVAL' && draftLive && !taskClosed) {
    return {
      stage: 'handover', eyebrow: 'HAND THIS PAGE OVER TO YOU', title: 'Let the page send what is in the form?',
      lines: [
        'Lumi filled these fields while the browser could not send anything.',
        'If you hand the page back to yourself, network access will resume. The site may immediately autosave or otherwise receive what is in the form.',
        'Lumi will not submit the form.'
      ],
      offerable: [], controls: ['decline_handover', 'approve_handover']
    }
  }
  if (handover && handover.resultCode === 'handover_unknown') {
    return {
      stage: 'handover_unknown', eyebrow: 'FORM HAND-OVER', title: 'Lumi lost the answer while handing over',
      lines: [
        'Lumi does not know whether network access was restored, and will not guess or try again.',
        'Lumi did not submit anything and cannot tell you whether the site accepted or saved anything.'
      ],
      offerable: [], controls: []
    }
  }
  if ((handover && handover.resultCode === 'handed_over') || draft?.status === 'HANDED_OVER') {
    return {
      stage: 'handed_over', eyebrow: 'FORM HANDED OVER', title: 'You took over in the browser window',
      lines: [
        'Lumi did not submit anything and cannot tell you whether the site accepted or saved it.'
      ],
      offerable: [], controls: []
    }
  }
  if (draftLive && draft && !taskClosed) {
    const partial = draft.partial
    return {
      stage: 'draft',
      eyebrow: partial ? 'FORM PARTLY PREPARED' : 'FORM PREPARED LOCALLY',
      title: partial ? 'Lumi could not finish this form' : `Lumi filled and checked ${draft.fieldCount} field${draft.fieldCount === 1 ? '' : 's'}`,
      lines: partial
        ? [
            'Lumi stopped because this form needs behavior that is unavailable while the network is frozen.',
            `Some fields may already be filled locally (${draft.fieldCount} checked). The form needs manual review.`,
            'Nothing was sent. Lumi has not submitted anything.',
            'These values exist only in this browser window. If Lumi or your computer restarts, they are lost.'
          ]
        : [
            'Nothing was sent while Lumi filled them.',
            'Network access is still blocked for this browser page.',
            'Lumi has not submitted anything.',
            'These values exist only in this browser window. If Lumi or your computer restarts, they are lost.'
          ],
      offerable: [], controls: ['discard_draft', 'request_handover']
    }
  }
  if (disclosure && disclosure.actionStatus === 'WAITING_APPROVAL' && !taskClosed) {
    if (!disclosure.executable) {
      return {
        stage: 'superseded', eyebrow: 'PREPARE THIS FORM', title: 'This plan was made before form filling existed',
        lines: [
          'It cannot be used. Prepare the form again to get a new plan you can approve.',
          'Nothing was changed.'
        ],
        offerable: [], controls: ['decline_disclosure']
      }
    }
    return {
      stage: 'approval', eyebrow: 'PREPARE THIS FORM', title: 'Fill these fields?',
      lines: [
        "When you click Fill these fields, Lumi will freeze this browser page's network access, then put exactly these values into exactly these fields and check that each one is there.",
        'No request can be sent while Lumi is filling. Lumi will not submit the form and cannot submit it.',
        'Forms that need the network to accept a value may fail; Lumi will stop and tell you, and will not turn the network on.',
        'The draft stays frozen until you discard it or hand the page over to yourself.',
        'These values are only in the browser window. If Lumi or your computer restarts, they are gone and you will need to prepare the form again.'
      ],
      offerable: [], controls: ['approve_disclosure', 'decline_disclosure']
    }
  }
  if (disclosure && disclosure.resultCode === 'local_draft_not_written' && !taskClosed) {
    return {
      stage: 'not_written', eyebrow: 'FORM NOT FILLED', title: 'Lumi could not fill this form',
      lines: [
        'Nothing was written to the page and nothing was sent.',
        'This approval was used once. To try again, prepare the form again and approve it again.'
      ],
      offerable: [], controls: []
    }
  }
  if (disclosure && disclosure.resultCode === 'prepared_nothing') {
    return {
      stage: 'prepared', eyebrow: 'FORM PLAN APPROVED', title: 'Approved — nothing was changed',
      lines: [
        'You approved exactly this plan. Lumi did not type into, choose in, or submit anything on the page.',
        'This approval was used once and cannot be used again.'
      ],
      offerable: [], controls: []
    }
  }
  if (!plan.preparing && !taskClosed && accountReadingActive && !grant?.status && plan.formCount > 0) {
    return {
      stage: 'prepare_offer', eyebrow: 'FORMS ON THIS SITE', title: 'Open a preparation window for this form?',
      lines: [
        'Lumi will reopen this profile in a visible window, go back to the same page and look at the form again. Nothing is filled yet.',
        'A form can only be filled in a window you can see. Lumi cannot fill it in the hidden reading window.'
      ],
      offerable: [], controls: ['start_preparation']
    }
  }
  if (grant?.status === 'PENDING' && !taskClosed) {
    return {
      stage: 'permission', eyebrow: 'PLAN FORM PREPARATION', title: 'Let Lumi plan this form?',
      lines: [],
      permission: {
        site: plan.site ?? 'this site',
        provider: RECIPIENT_LABELS[grant.planningRecipient],
        sent: [
          "this form's field labels, types and options",
          'masked previews of the saved details selected below'
        ],
        notSent: ['Raw saved values will NOT be sent to the AI.'],
        ...(grant.allowedDataRefs.includes('country')
          ? { countryNotice: 'The one exception: a saved country is sent as written, because a country cannot be masked and is needed to choose the right option.' }
          : {}),
        savedDetails: grant.allowedDataRefs.map((kind) => ({ kind, label: DATA_KIND_LABELS[kind] })),
        cannotAct: 'The AI cannot type into the form or submit it.'
      },
      offerable: [], controls: ['decline_form_planning', 'allow_form_planning']
    }
  }
  if (grant?.status === 'ACTIVE' && !expired && !taskClosed && !disclosure) {
    return {
      stage: 'ready', eyebrow: 'FORM PLANNING ALLOWED', title: 'Planning is allowed for this form',
      lines: [`Only ${RECIPIENT_LABELS[grant.planningRecipient]} may see the form structure and masked previews. It cannot type into the form or submit it.`],
      offerable: [], controls: ['allow_form_planning']
    }
  }
  if (disclosure && (disclosure.actionStatus === 'REJECTED')) {
    return { stage: 'declined', eyebrow: 'FORM PLAN', title: 'You declined that plan', lines: ['Nothing was changed.'], offerable: [], controls: [] }
  }
  if (!taskClosed && accountReadingActive && plan.preparing && plan.savedDetails.length > 0 && (!grant || grant.status !== 'ACTIVE')) {
    return {
      stage: 'offer', eyebrow: 'FORMS ON THIS SITE', title: 'Plan a form with your saved details',
      lines: ['Choose which saved details a planner may see (masked). Nothing is sent until you allow it on the next card.'],
      offerable: plan.savedDetails.map((item) => ({ kind: item.kind, label: DATA_KIND_LABELS[item.kind], preview: item.preview })),
      controls: ['plan_form']
    }
  }
  return FORM_PLAN_HIDDEN
}

export interface DisclosureLine {
  savedLabel?: string
  detail: string
  fieldLabel: string
}

/** The manifest card's rows, in manifest order: what would go into which field. */
export function describeDisclosureCard(card: AgentDisclosureCardView): {
  site: string
  formLabel?: string
  rows: DisclosureLine[]
  countryNotice?: string
} {
  return {
    site: card.site,
    ...(card.formLabel ? { formLabel: card.formLabel } : {}),
    rows: card.fields.map((field): DisclosureLine => {
      switch (field.kind) {
        case 'saved_detail':
          return { savedLabel: `Saved ${DATA_KIND_LABELS[field.dataRef]}`, detail: field.preview, fieldLabel: field.fieldLabel }
        case 'option':
          return { savedLabel: 'Option', detail: field.optionLabel, fieldLabel: field.fieldLabel }
        case 'checkbox':
          return { savedLabel: field.checked ? 'Checked' : 'Unchecked', detail: field.checked ? 'tick' : 'leave empty', fieldLabel: field.fieldLabel }
      }
    }),
    ...(card.revealsCountry
      ? { countryNotice: 'A saved country is shown exactly as saved, because a country cannot be masked.' }
      : {})
  }
}
