import type {
  AgentActionView,
  AgentBookingCriteria,
  AgentChangedFact,
  AgentDisclosureRecipient,
  AgentEventView,
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
