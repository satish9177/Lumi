/**
 * Desktop contract for durable agent tasks.
 *
 * These are the only shapes that cross preload for the agent bridge. They are
 * closed projections built by Electron main from validated runtime responses:
 * no runtime address, credential, raw proposal JSON, raw event payload, page
 * text beyond typed booking fields, or raw error string ever appears here.
 *
 * Enum values are checked against `agent-runtime-contract.json`, which the
 * Python runtime generates from its own models (see agent-wire.test.ts).
 *
 * Ownership boundary: local tools stay in main's in-memory PendingActionStore.
 * Durable browser actions are owned exclusively by the Python action ledger;
 * nothing here creates or approves a PendingActionStore action, and nothing in
 * PendingActionStore can reach the ledger.
 */

import type { AgentDocumentApi } from './document-contracts'
import type { VoiceTaskCommand, VoiceTaskOutcome } from './voice-task-contracts'
import type { AgentPreferenceView, ModelDiagnosticView, PreferenceKey } from './model-contracts'

export const TASK_STATUSES = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING',
  'OUTCOME_UNKNOWN', 'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
] as const
export type AgentTaskStatus = typeof TASK_STATUSES[number]

export const ACTION_STATUSES = [
  'PROPOSED', 'WAITING_APPROVAL', 'APPROVED',
  /**
   * Milestone 7b. Authorized by a bounded task grant the user confirmed once,
   * not by an exact approval of this step. Deliberately a different word: the
   * timeline must never claim the user reviewed each research step.
   */
  'AUTHORIZED',
  'REJECTED', 'EXECUTING', 'SUCCEEDED',
  'FAILED', 'OUTCOME_UNKNOWN', 'RECONCILING'
] as const
export type AgentActionStatus = typeof ACTION_STATUSES[number]

export const APPROVAL_STATUSES = ['PENDING', 'APPROVED', 'REJECTED', 'CONSUMED'] as const
export type AgentApprovalStatus = typeof APPROVAL_STATUSES[number]

export const ATTEMPT_OUTCOMES = ['SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const
export type AgentAttemptOutcome = typeof ATTEMPT_OUTCOMES[number]

export const RISK_TIERS = ['R0', 'R1', 'R2', 'R3'] as const
export type AgentRiskTier = typeof RISK_TIERS[number]

export const LOOKUP_STATUSES = ['FOUND', 'NOT_FOUND', 'UNKNOWN'] as const
export type AgentLookupStatus = typeof LOOKUP_STATUSES[number]

export const DISPATCH_STATUSES = [
  'DISPATCHED', 'OK', 'CHANGED_RESOURCE', 'RESOURCE_UNAVAILABLE', 'FAILED_BEFORE_EFFECT', 'OUTCOME_UNKNOWN'
] as const
export type AgentDispatchStatus = typeof DISPATCH_STATUSES[number]

export const CHANGED_FACT_FIELDS = ['slot_id', 'doctor', 'time', 'price', 'currency'] as const
export type AgentChangedFactField = typeof CHANGED_FACT_FIELDS[number]

export const TASK_EVENT_TYPES = [
  'task.created', 'task.cancelled', 'task.criteria_updated', 'task.search_completed', 'task.info_lookup_completed', 'task.page_answer_recorded',
  'task.research_scope_requested', 'task.research_scope_granted', 'task.research_scope_revoked', 'task.research_answer_recorded',
  'task.authenticated_scope_requested', 'task.authenticated_scope_granted', 'task.authenticated_scope_revoked',
  'task.authenticated_answer_recorded', 'task.authenticated_paused', 'task.authenticated_resumed',
  'task.form_prepare_scope_requested', 'task.form_prepare_scope_granted', 'task.form_prepare_scope_revoked',
  'task.form_planning_context_built',
  'task.form_preparation_started',
  'task.form_draft_recorded',
  'task.form_draft_discarded',
  'task.form_draft_handed_over',
  'task.form_draft_lost',
  // Milestone 9 S2. Ids, digests, counts and closed codes only -- never desktop text.
  'task.desktop_disclosure_requested',
  'task.desktop_disclosure_granted',
  'task.desktop_disclosure_revoked',
  'task.desktop_disclosure_started',
  'task.desktop_answer_recorded',
  'task.desktop_disclosure_failed',
  'task.desktop_disclosure_outcome_unknown',
  // Milestone 9 S4. Ids, digests, counts and closed codes only -- never desktop text or a raw value.
  'task.desktop_plan_requested',
  'task.desktop_plan_granted',
  'task.desktop_plan_revoked',
  'task.desktop_plan_started',
  'task.desktop_plan_action_recorded',
  'task.desktop_plan_failed',
  'task.desktop_plan_outcome_unknown',
  // Milestone 9 S5. Ids, digests, dimensions, DPI, monitor identity and closed codes only -- never a
  // pixel, a coordinate, a window title, a purpose string or a candidate's label/text.
  'task.desktop_capture_requested',
  'task.desktop_capture_granted',
  'task.desktop_capture_revoked',
  'task.desktop_capture_started',
  'task.desktop_capture_succeeded',
  'task.desktop_capture_failed',
  'task.desktop_capture_outcome_unknown',
  'task.desktop_vision_disclosure_requested',
  'task.desktop_vision_disclosure_granted',
  'task.desktop_vision_disclosure_revoked',
  'task.desktop_vision_disclosure_started',
  'task.desktop_vision_candidates_recorded',
  'task.desktop_vision_disclosure_failed',
  'task.desktop_vision_disclosure_outcome_unknown',
  // Milestone 10 S1: approved documents.
  'task.document_file_added',
  'task.document_extracted',
  'task.document_extraction_refused',
  'task.document_disclosure_requested',
  'task.document_disclosure_granted',
  'task.document_disclosure_revoked',
  'task.document_disclosure_started',
  'task.document_comparison_recorded',
  'task.document_disclosure_failed',
  'task.document_disclosure_outcome_unknown',
  'action.proposed', 'action.approval_requested',
  'action.approved', 'action.authorized', 'action.rejected', 'action.execution_started', 'action.succeeded',
  'action.failed', 'action.outcome_unknown', 'action.reconciliation_started', 'action.reconciled'
] as const
export type AgentTaskEventType = typeof TASK_EVENT_TYPES[number]

export const BOOKING_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] as const
export type AgentBookingDay = typeof BOOKING_DAYS[number]

export type AgentRuntimeState = 'stopped' | 'starting' | 'running' | 'unavailable' | 'failed' | 'stopping' | 'not_installed' | 'not_configured'

export interface AgentRuntimeView {
  state: AgentRuntimeState
  /** Opaque process-generation id. Not a credential; shown in technical details. */
  generation?: string
}

export interface AgentBookingCriteria {
  specialty: string
  day: AgentBookingDay | ''
  /** Clinic-local 24-hour HH:MM bounds, inclusive. */
  earliestTime?: string
  latestTime?: string
  /** Integer price ceiling; always paired with its currency. */
  maxPrice?: number
  maxPriceCurrency?: string
  /** Inclusive local calendar dates (YYYY-MM-DD), resolved by main from a spoken or typed day. */
  dateFrom?: string
  dateTo?: string
}

export const TASK_KINDS = ['appointment_booking', 'clinic_info', 'page_inspection', 'public_research', 'authenticated_read'] as const
export type AgentTaskKind = typeof TASK_KINDS[number]

export const CLINIC_INFO_TOPICS = ['overview', 'hours', 'fee', 'languages', 'address', 'walk_ins'] as const
export type AgentClinicInfoTopic = typeof CLINIC_INFO_TOPICS[number]

/** What a read-only clinic-information task asks about. */
export interface AgentClinicInfoQuery {
  specialty: string
  doctor: string
  topic: AgentClinicInfoTopic
}

/** Public, typed doctor profile facts read by the reviewed adapter. */
export interface AgentDoctorProfileView {
  doctorId: string
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

export interface AgentTaskView {
  taskId: string
  status: AgentTaskStatus
  revision: number
  lastEventSequence: number
  kind: AgentTaskKind
  /** Booking constraints (empty for a clinic-info task). */
  criteria: AgentBookingCriteria
  /** Set for `clinic_info` tasks. */
  infoQuery?: AgentClinicInfoQuery
  /** Set for `page_inspection` tasks: the user's URL and question. */
  inspection?: AgentInspectionRequestView
  /** Set for `public_research` tasks: the objective the user typed. */
  research?: AgentResearchRequestView
  /** Set for `authenticated_read` tasks: the objective and the profile it reads. */
  authenticated?: AgentAuthenticatedRequestView
  /** The completed voice turn that created this task, if any. */
  voiceTurnId?: string
  /** The typed request that created this task, if any. */
  requestId?: string
  createdAt: string
  updatedAt: string
}

export interface AgentSlotView {
  slotId: string
  doctor: string
  specialty: string
  time: string
  price: number
  currency: string
}

/** The trusted, persisted booking a user is asked to approve. */
export interface AgentBookingView {
  site: string
  slotId: string
  doctor: string
  time: string
  price: number
  currency: string
}

export interface AgentApprovalView {
  approvalId: string
  status: AgentApprovalStatus
  actionRevision: number
  proposalDigest: string
  createdAt: string
  expiresAt: string
  approvedAt?: string
}

export interface AgentChangedFact {
  field: AgentChangedFactField
  approved: string
  observed: string
}

export interface AgentReceiptView {
  bookingId: string
  doctor: string
  price: number
  currency: string
}

export interface AgentAttemptResultView {
  dispatchStatus?: AgentDispatchStatus
  submitted?: boolean
  bookingId?: string
  receipt?: AgentReceiptView
  changedFacts?: AgentChangedFact[]
}

export interface AgentAttemptView {
  attemptId: string
  attemptNumber: number
  runtimeGeneration: string
  startedAt: string
  finishedAt?: string
  outcome?: AgentAttemptOutcome
  errorCode?: string
  result?: AgentAttemptResultView
}

export interface AgentActionView {
  actionId: string
  taskId: string
  toolName: 'commit_booking'
  status: AgentActionStatus
  revision: number
  riskTier: AgentRiskTier
  proposalDigest: string
  createdAt: string
  updatedAt: string
  booking: AgentBookingView
  approval?: AgentApprovalView
  attempts: AgentAttemptView[]
}

export interface AgentFoundBookingView {
  bookingId: string
  slotId: string
  doctor: string
  time: string
  price: number
  currency: string
}

export interface AgentReconciliationView {
  result: AgentAttemptOutcome
  lookup: AgentLookupStatus
  bookingId?: string
  bookingCount?: number
  booking?: AgentFoundBookingView
  absenceIsAuthoritative?: boolean
}

export interface AgentEventView {
  sequence: number
  type: AgentTaskEventType
  taskRevision: number
  createdAt: string
  actionId?: string
  /** The tool an action event belongs to (a closed identifier), so the timeline can word it truthfully. */
  toolName?: string
  actionStatus?: AgentActionStatus
  actionRevision?: number
  attemptId?: string
  attemptNumber?: number
  approvalId?: string
  outcome?: AgentAttemptOutcome
  errorCode?: string
  reason?: string
  reconciliation?: AgentReconciliationView
  /** `task.criteria_updated` / `task.search_completed`: the constraints applied. */
  criteria?: AgentBookingCriteria
  /** `task.search_completed`: the admitted, browser-observed slots, in order. */
  searchResults?: AgentSlotView[]
  /** `task.criteria_updated`: prepared bookings the new constraints excluded. */
  invalidatedActionIds?: string[]
  /** `task.info_lookup_completed`: the query and the profiles that were read. */
  infoQuery?: AgentClinicInfoQuery
  profiles?: AgentDoctorProfileView[]
  /** `task.page_answer_recorded`: the recorded status only, never the answer or page text. */
  answerStatus?: AgentPageAnswerStatus
}

// ---- Milestone 7a: one approved URL, one grounded answer ------------------------

export const PAGE_ANSWER_STATUSES = ['answered', 'not_found', 'ambiguous', 'not_verified'] as const
export type AgentPageAnswerStatus = typeof PAGE_ANSWER_STATUSES[number]

/** Providers that may be named as recipients of page text. Never a credential. */
export const DISCLOSURE_RECIPIENTS = ['openai', 'gemini', 'deepseek', 'scripted'] as const
export type AgentDisclosureRecipient = typeof DISCLOSURE_RECIPIENTS[number]

/** What the user asked, as stored on the task. */
export interface AgentInspectionRequestView {
  url: string
  host: string
  question: string
}

/** The persisted proposal the trusted approval card shows, field for field. */
export interface AgentInspectionProposalView {
  url: string
  host: string
  question: string
  policyVersion: string
  recipients: AgentDisclosureRecipient[]
  maxTextChars: number
  maxLinks: number
  maxRedirects: number
}

export interface AgentInspectionAttemptView {
  attemptId: string
  attemptNumber: number
  runtimeGeneration: string
  startedAt: string
  finishedAt?: string
  outcome?: AgentAttemptOutcome
  /** A stable refusal or failure code, never page text. */
  errorCode?: string
  refusal?: string
  httpStatus?: number
}

/**
 * Metadata about what was read. The page's text blocks do not cross preload;
 * the renderer receives only the page title and final URL (shown as labelled
 * plain text) and the bounded evidence quoted by a verified answer.
 */
export interface AgentObservationMetaView {
  observationId: string
  requestedUrl: string
  finalUrl: string
  redirects: string[]
  title: string
  documentEpoch: number
  settled: boolean
  truncated: boolean
  observedAt: string
  contentHash: string
  blockCount: number
  linkCount: number
  workerGeneration: string
}

export interface AgentEvidenceView {
  block: string
  quote: string
}

export interface AgentPageAnswerView {
  observationId: string
  status: AgentPageAnswerStatus
  answer: string
  evidence: AgentEvidenceView[]
  provider: AgentDisclosureRecipient
  model: string
  answeredAt: string
}

export interface AgentInspectionView {
  actionId: string
  taskId: string
  toolName: 'inspect_public_page'
  status: AgentActionStatus
  revision: number
  riskTier: AgentRiskTier
  proposalDigest: string
  createdAt: string
  updatedAt: string
  proposal: AgentInspectionProposalView
  approval?: AgentApprovalView
  attempts: AgentInspectionAttemptView[]
  observation?: AgentObservationMetaView
  answer?: AgentPageAnswerView
}

// ---- Milestone 7b: bounded public web research ------------------------------------

export const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
export type AgentGrantStatus = typeof GRANT_STATUSES[number]

export const RESEARCH_OPERATIONS = [
  'public_search', 'navigate', 'observe', 'scroll', 'history', 'tab'
] as const
export type AgentResearchOperation = typeof RESEARCH_OPERATIONS[number]

export const RESEARCH_ANSWER_STATUSES = ['answered', 'partial', 'not_found', 'not_verified'] as const
export type AgentResearchAnswerStatus = typeof RESEARCH_ANSWER_STATUSES[number]

export const RESEARCH_STOP_REASONS = [
  'goal_reached', 'no_evidence', 'budget_exhausted', 'blocked', 'planner_failed',
  'user_stopped', 'outside_scope'
] as const
export type AgentResearchStopReason = typeof RESEARCH_STOP_REASONS[number]

/** What the user's objective was, as stored on the task. */
export interface AgentResearchRequestView {
  objective: string
}

export interface AgentResearchBudgets {
  maxSteps: number
  maxObservations: number
  maxPlannerCalls: number
  maxTabs: number
  maxActiveSeconds: number
  maxModelInputTokens: number
  maxModelOutputTokens: number
  maxVisionCalls: number
}

/**
 * Exactly what one confirmed scope authorises, as the trusted card shows it.
 * `allowed` and `forbidden` are the runtime's own machine-readable lists; the
 * renderer turns them into its own words and never renders website text here.
 */
export interface AgentResearchScopeView {
  policyVersion: string
  allowedOperations: AgentResearchOperation[]
  allowed: string[]
  forbidden: string[]
  schemes: string[]
  methods: string[]
  /** `any_public`, or the host list configuration narrowed research to. */
  hosts: 'any_public' | string[]
  budgets: AgentResearchBudgets
  recipients: AgentDisclosureRecipient[]
  maxTextChars: number
  /** Addresses the *user* typed in the objective, if any. */
  seeds: string[]
}

export interface AgentResearchGrantView {
  grantId: string
  status: AgentGrantStatus
  revision: number
  scopeDigest: string
  scope: AgentResearchScopeView
  createdAt: string
  confirmedAt?: string
  expiresAt?: string
}

export interface AgentResearchSessionView {
  sessionId: string
  status: string
  createdAt: string
}

/** One observed link: a ref, a label and a host. Never an address. */
export interface AgentResearchLinkView {
  ref: string
  text: string
  host: string
}

export interface AgentResearchResultView {
  ref: string
  title: string
  host: string
  snippet: string
}

/**
 * One bounded observation. Everything here is untrusted page data: the
 * renderer shows the source address and title as labelled plain text, and
 * nothing from a page ever becomes a label, a control or an instruction.
 */
export interface AgentResearchObservationView {
  observationId: string
  /** The model-facing ref, `o<sequence>`. */
  ref: string
  sequence: number
  kind: 'page' | 'search_results' | 'tab_state'
  operation: AgentResearchOperation
  tab?: string
  documentEpoch: number
  query?: string
  finalUrl?: string
  finalHost?: string
  title: string
  settled: boolean
  truncated: boolean
  observedAt: string
  contentHash: string
  blocks: AgentResearchBlockView[]
  links: AgentResearchLinkView[]
  results: AgentResearchResultView[]
  openTabs: string[]
  sessionId?: string
}

export interface AgentResearchBlockView {
  id: string
  text: string
}

export interface AgentResearchEvidenceView {
  observation: string
  block: string
  quote: string
}

export interface AgentResearchAnswerView {
  status: AgentResearchAnswerStatus
  stopReason: AgentResearchStopReason
  answer: string
  evidence: AgentResearchEvidenceView[]
  provider: AgentDisclosureRecipient
  model: string
  stepsUsed: number
  observationsUsed: number
  plannerCalls: number
  createdAt: string
}

export interface AgentResearchUsageView {
  steps: number
  observations: number
  plannerCalls: number
  activeSeconds: number
  tabs: number
}

export interface AgentResearchView {
  taskId: string
  objective: string
  grant?: AgentResearchGrantView
  session?: AgentResearchSessionView
  observations: AgentResearchObservationView[]
  answer?: AgentResearchAnswerView
  usage: AgentResearchUsageView
  searchConfigured: boolean
  /**
   * A step of this task has no outcome Lumi can stand behind: it is executing,
   * or it ended with an unknown outcome. Further steps are refused while this
   * is true, and nothing is repeated on its behalf.
   */
  unresolvedStep: boolean
}

/** One source Lumi actually opened, for the sources list under an answer. */
export interface AgentResearchSourceView {
  ref: string
  url: string
  host: string
  title: string
  observedAt: string
}

export interface AgentTaskSnapshot {
  runtimeGeneration: string
  task: AgentTaskView
  actions: AgentActionView[]
  /** `page_inspection` tasks: the newest inspection action, with its evidence. */
  inspection?: AgentInspectionView
  /** `public_research` tasks: the scope, the observations and the answer. */
  research?: AgentResearchView
  /** `authenticated_read` tasks: the scope, the redacted observations and the answer. */
  authenticated?: AgentAuthenticatedView
  /** `authenticated_read` tasks: the form-planning grant and the exact disclosure card (Milestone 8b S5). */
  formPlan?: AgentFormPlanView
  /** Events strictly after the requested sequence, ordered, without gaps. */
  events: AgentEventView[]
}


// ---- Milestone 8a S3: authenticated account reading --------------------------------
//
// The honest name for the capability is `account_scoped_read`: Lumi issues only
// GET and HEAD requests, to one site, in a browser carrying the user's session
// for that site. Lumi performs no intentional change -- but the website may
// still record the visit, mark something as read, update "last active", extend
// a session or write account activity, and Lumi can neither prevent nor detect
// that. It is never described as read-only, invisible or free of effects.
//
// Nothing here carries a URL, a cookie, a header, a profile path, an account
// identity or an address of any kind. Every string in an observation is the
// *redacted* projection the one approved provider received.

export const AUTHENTICATED_OPERATIONS = ['navigate', 'observe', 'reveal', 'tab', 'history'] as const
export type AgentAuthenticatedOperation = typeof AUTHENTICATED_OPERATIONS[number]

/** Why an authenticated task stopped and needs a human. Set by code, never by a model. */
export const AUTHENTICATED_PAUSE_REASONS = [
  'login_required', 'account_changed', 'account_identity_unknown', 'left_site_scope',
  // Milestone 8b S6: a local form draft waits for the user; the user took the browser over;
  // the browser that held the draft is gone. Set by the controller, never by a model.
  'form_draft', 'user_takeover', 'browser_lost'
] as const
export type AgentAuthenticatedPauseReason = typeof AUTHENTICATED_PAUSE_REASONS[number]

export interface AgentAuthenticatedRequestView {
  objective: string
  profileId: string
  /** Always `account_private`. A task that says otherwise is not parsed. */
  classification: 'account_private'
}

export interface AgentAuthenticatedBudgets {
  maxSteps: number
  maxObservations: number
  maxPlannerCalls: number
  maxAnswerCalls: number
  maxTabs: number
  maxActiveSeconds: number
  /** Structurally zero: no authenticated screenshot ever reaches a provider. */
  maxVisionCalls: 0
}

export interface AgentAuthenticatedScopeView {
  policyVersion: string
  site: string
  allowedOperations: AgentAuthenticatedOperation[]
  allowed: string[]
  forbidden: string[]
  methods: string[]
  /** Always true, and the card says so before the Allow button. */
  websiteSideEffectsPossible: true
  /** Exactly one provider. The card names it; nothing else may receive the text. */
  recipient: AgentDisclosureRecipient
  maxTextChars: number
  maxBlocks: number
  budgets: AgentAuthenticatedBudgets
}

export interface AgentAuthenticatedGrantView {
  grantId: string
  status: AgentGrantStatus
  revision: number
  scopeDigest: string
  scope: AgentAuthenticatedScopeView
  createdAt: string
  confirmedAt?: string
  expiresAt?: string
}

/** Trusted, controller-authored profile facts. The label is the user's own. */
export interface AgentAuthenticatedProfileView {
  profileId: string
  label: string
  site: string
  status: AgentBrowserProfileStatus
}

export interface AgentAuthenticatedBlockView {
  id: string
  text: string
}

export interface AgentAuthenticatedLinkView {
  ref: string
  text: string
  host: string
}

/** One redacted observation. Untrusted data: never a label, a control or an instruction. */
export interface AgentAuthenticatedObservationView {
  observationId: string
  ref: string
  sequence: number
  kind: 'page' | 'tab_state'
  operation: AgentAuthenticatedOperation
  tab?: string
  documentEpoch: number
  host?: string
  title: string
  settled: boolean
  truncated: boolean
  observedAt: string
  contentHash: string
  blocks: AgentAuthenticatedBlockView[]
  links: AgentAuthenticatedLinkView[]
  openTabs: string[]
  /** How many identifiers were reduced before sending, by kind. Counts only. */
  redactions: Record<string, number>
}

export interface AgentAuthenticatedAnswerView {
  classification: 'account_private'
  profileId: string
  status: AgentResearchAnswerStatus
  stopReason: AgentResearchStopReason
  answer: string
  evidence: AgentResearchEvidenceView[]
  provider: AgentDisclosureRecipient
  model: string
  stepsUsed: number
  observationsUsed: number
  plannerCalls: number
  createdAt: string
}

export interface AgentAuthenticatedUsageView {
  steps: number
  observations: number
  plannerCalls: number
  activeSeconds: number
  tabs: number
}

export interface AgentAuthenticatedView {
  taskId: string
  objective: string
  classification: 'account_private'
  profile?: AgentAuthenticatedProfileView
  grant?: AgentAuthenticatedGrantView
  observations: AgentAuthenticatedObservationView[]
  answer?: AgentAuthenticatedAnswerView
  usage: AgentAuthenticatedUsageView
  pauseReason?: AgentAuthenticatedPauseReason
  /** A step has no outcome Lumi can stand behind; only a fresh observation may follow. */
  unresolvedStep: boolean
}

/**
 * The providers the trusted UI may offer for one authenticated task. Built by
 * main from its own configuration and returned as stable ids: the renderer
 * chooses among them and can return nothing else -- not a free-form name, and
 * never a URL.
 */
export interface AgentAuthenticatedOptions {
  recipients: AgentDisclosureRecipient[]
}

// ---- Milestone 8b S5: form planning and exact disclosure approval -----------------
//
// **S5 changes no website.** Lumi builds an exact, reviewable proposal of which saved
// detail would go into which field, the user approves exactly that, and it stops
// with `prepared_nothing`. There is nothing here that types, chooses, checks,
// clicks or submits, and no value a renderer could send.
//
// What these views never carry: a raw saved value, a value digest, an element
// identity hash, a manifest digest, a selector, an account fingerprint, a revoke
// epoch or an origin URL. A masked preview is still private data.

/** The closed set of saved details. There is no ninth kind. */
export const PROTECTED_DATA_KINDS = [
  'legal_name', 'preferred_name', 'email', 'phone', 'city', 'country', 'linkedin_url', 'portfolio_url'
] as const
export type AgentProtectedDataKind = typeof PROTECTED_DATA_KINDS[number]

/** What a terminal S5 approval records: the approval was spent and nothing was prepared. */
export const PREPARED_NOTHING = 'prepared_nothing'

/**
 * What a spent S6 approval ends in. `local_draft_prepared`: every approved field was filled and
 * verified in the browser window, with the network frozen. `local_draft_partial`: some were, and
 * the form needs manual review. `local_draft_not_written`: nothing was.
 */
export const DISCLOSURE_RESULT_CODES = [
  PREPARED_NOTHING, 'local_draft_prepared', 'local_draft_partial', 'local_draft_not_written'
] as const
export type AgentDisclosureResultCode = typeof DISCLOSURE_RESULT_CODES[number]

export interface AgentSavedDetailView {
  dataRef: AgentProtectedDataKind
  kind: AgentProtectedDataKind
  /** Deterministically masked by the runtime. The renderer never masks anything itself. */
  preview: string
  updatedAt: string
}

export interface AgentFormGrantView {
  grantId: string
  status: AgentGrantStatus
  revision: number
  expiresAt?: string
  /** The saved details a planner may see masked previews of. Never widened by a model. */
  allowedDataRefs: AgentProtectedDataKind[]
  /** Exactly one provider, no failover. */
  planningRecipient: AgentDisclosureRecipient
  maxFields: number
}

export type AgentDisclosureFieldView =
  | { kind: 'saved_detail'; fieldLabel: string; controlType: string; dataRef: AgentProtectedDataKind; preview: string }
  | { kind: 'option'; fieldLabel: string; controlType: string; optionLabel: string }
  | { kind: 'checkbox'; fieldLabel: string; controlType: string; checked: boolean }

export interface AgentDisclosureCardView {
  actionId: string
  revision: number
  actionStatus: AgentActionStatus
  approvalStatus?: AgentApprovalStatus
  approvalExpiresAt?: string
  /** The site's display host, never a URL. */
  site: string
  formLabel?: string
  fields: AgentDisclosureFieldView[]
  /** True when a saved country is shown as-is (a country cannot be masked). */
  revealsCountry: boolean
  /** What the spent approval ended in; absent until it was spent. */
  resultCode?: AgentDisclosureResultCode
  /**
   * True for a Milestone 8b S6 manifest, whose approval FILLS the form locally with the network
   * frozen. False for a historical S5 manifest, which is never executable.
   */
  executable: boolean
}

export const DRAFT_STATUSES = ['PREPARED', 'STALE', 'DISCARDED', 'HANDED_OVER'] as const
export type AgentDraftStatus = typeof DRAFT_STATUSES[number]

/** The local draft, as the trusted card shows it. Ids, a status and counts: never a value. */
export interface AgentDraftCardView {
  draftId: string
  revision: number
  status: AgentDraftStatus
  fieldCount: number
  /** Some approved fields were not verified: the form needs manual review. */
  partial: boolean
  site: string
}

export const HANDOVER_RESULT_CODES = ['handed_over', 'handover_refused', 'handover_unknown'] as const
export type AgentHandoverResultCode = typeof HANDOVER_RESULT_CODES[number]

/** The second exact approval: letting the page send. Safe references only. */
export interface AgentHandoverCardView {
  actionId: string
  revision: number
  actionStatus: AgentActionStatus
  approvalStatus?: AgentApprovalStatus
  approvalExpiresAt?: string
  draftId: string
  fieldCount: number
  partial: boolean
  site: string
  resultCode?: AgentHandoverResultCode
}

export interface AgentFormPlanView {
  taskId: string
  site?: string
  savedDetails: AgentSavedDetailView[]
  grant?: AgentFormGrantView
  disclosure?: AgentDisclosureCardView
  formCount: number
  candidateElementCount: number
  /** The profile is open in a headed preparation window and nothing has been written yet. */
  preparing: boolean
  draft?: AgentDraftCardView
  handover?: AgentHandoverCardView
}

/**
 * Milestone 8a S2: manual login and human takeover.
 *
 * `AgentBrowserProfileView` is deliberately thin. It carries none of what S1
 * keeps internal to the runtime and the worker: no directory path, no
 * cookie, no token, no Chromium/Playwright version, no lease detail. Only
 * what a trusted "sign in" card needs to show.
 */
export const BROWSER_PROFILE_STATUSES = ['NEW', 'NEEDS_LOGIN', 'AUTHENTICATED', 'DELETED'] as const
export type AgentBrowserProfileStatus = typeof BROWSER_PROFILE_STATUSES[number]

export interface AgentBrowserProfileView {
  profileId: string
  label: string
  site: string
  status: AgentBrowserProfileStatus
  revision: number
  lastLoginCompletedAt?: string
  lastObservedAt?: string
  /**
   * The takeover still open on this profile, if there is one. Present so
   * that main can rediscover a live, human-driven sign-in window from
   * durable runtime state alone after an Electron-main restart, and keep
   * screen capture refused until it is gone -- nothing in main or in the
   * renderer has to remember an attempt id across a restart for the capture
   * exclusion to hold. `undefined` means the runtime reported no open
   * attempt for this profile, which is an answer, not a missing value.
   */
  activeTakeover?: AgentActiveTakeoverView
}

/**
 * Four fields and deliberately no fifth: enough to know that a headed,
 * human-driven browser window may be on screen and whose it is, and not
 * enough to describe anything about the page in it. No URL, no title, no
 * page text, no profile directory, no credential signal.
 */
export interface AgentActiveTakeoverView {
  profileId: string
  attemptId: string
  status: AgentLoginAttemptStatus
  expiresAt: string
}

export const LOGIN_ATTEMPT_STATUSES = [
  'OPEN', 'UNCONFIRMED', 'COMPLETED', 'CANCELLED', 'EXPIRED', 'INTERRUPTED'
] as const
export type AgentLoginAttemptStatus = typeof LOGIN_ATTEMPT_STATUSES[number]

/**
 * One takeover's bounded interval and how it ended. Note the absence: no
 * page text, no title, no URL, no credential signal. This is not an
 * authorization -- it funds nothing -- and clicking "I'm signed in" is not,
 * by itself, proof that anything succeeded; `refusalReason` on
 * `AgentLoginTakeoverView` is what a *completed* check actually found.
 */
export interface AgentLoginAttemptView {
  attemptId: string
  profileId: string
  status: AgentLoginAttemptStatus
  startedAt: string
  expiresAt: string
  completedAt?: string
  cancelledAt?: string
}

/** Statuses in which the takeover is still open: agent automation is
 * suspended and the human is driving the browser. */
export const OPEN_LOGIN_ATTEMPT_STATUSES: readonly AgentLoginAttemptStatus[] = ['OPEN', 'UNCONFIRMED']

export interface AgentLoginTakeoverView {
  attempt: AgentLoginAttemptView
  profile: AgentBrowserProfileView
  /**
   * Set only by a *completed* confirmation that did not result in
   * `AUTHENTICATED` -- a closed, stable, controller-authored reason, never
   * page text. `undefined` when the profile is now `AUTHENTICATED`, or while
   * the takeover is still open.
   */
  refusalReason?: string
}

// ---- Milestone 9 S2: exact desktop disclosure and read-only reasoning ------------------------
//
// Read these as the whole of what the renderer can learn about a desktop read. What is missing stays
// missing: no window handle, process id or path, no AutomationId, class name or coordinate, no
// snapshot, no digest, no other observation. The window display strings are `untrusted_environment`
// text: they are rendered as inert text and never choose the provider, the wording or a button.

export const DESKTOP_READ_PHASES = [
  'awaiting_approval', 'approved', 'reasoning', 'answered', 'failed', 'outcome_unknown', 'declined', 'expired'
] as const
export type AgentDesktopReadPhase = typeof DESKTOP_READ_PHASES[number]

export interface AgentDesktopSurface {
  surfaceRef: string
  surfaceEpoch: number
  /** Untrusted display text. */
  applicationLabel: string
  /** Untrusted display text. */
  windowTitle: string
  visible: boolean
  minimized: boolean
}

export interface AgentDesktopSurfaceList {
  workerGeneration: string
  surfaces: AgentDesktopSurface[]
  truncated: boolean
}

export interface AgentDesktopCardView {
  grantId: string
  grantRevision: number
  grantStatus: 'PENDING' | 'ACTIVE' | 'REVOKED' | 'EXPIRED' | 'COMPLETED'
  expiresAt?: string
  /** The one provider that would receive the snapshot, chosen by Lumi main, never the renderer. */
  recipient: AgentDisclosureRecipient
  model: string
  observedAt: string
  /** Untrusted display text. */
  applicationLabel: string
  /** Untrusted display text. */
  windowTitle: string
  maxNodes: number
  maxTextBytes: number
  redactionPolicy: string
  observationAvailable: boolean
  nodeCount?: number
  textBytes?: number
  redactionCount?: number
  truncated?: boolean
  truncation: string[]
}

export interface AgentDesktopDisclosureView {
  disclosureId: string
  status: 'STARTED' | 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN'
  errorCode?: string
  startedAt: string
  finishedAt?: string
  nodeCount: number
  textBytes: number
  redactionCount: number
  truncated: boolean
}

export interface AgentDesktopEvidenceView {
  controlRef: string
  quote: string
}

export interface AgentDesktopAnswerView {
  kind: 'answer' | 'cannot_answer'
  answer?: string
  reason?: string
  evidence: AgentDesktopEvidenceView[]
  recipient: AgentDisclosureRecipient
  model: string
  observedAt?: string
  createdAt: string
}

export interface AgentDesktopReadView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  /** The user's own typed question. */
  objective: string
  phase: AgentDesktopReadPhase
  card?: AgentDesktopCardView
  disclosure?: AgentDesktopDisclosureView
  answer?: AgentDesktopAnswerView
}

// ---- Milestone 9 S3: trusted focus, semantic scroll and registered-application launch ---------
//
// Three effects only, each behind an exact trusted approval on the ordinary action ledger. Read these
// as the whole of what the renderer can learn about a desktop action. What is missing stays missing: no
// window handle, process id or path, no executable path or argument, no AutomationId or coordinate, no
// snapshot, no digest. Application strings are `untrusted_environment` text, rendered inert. The renderer
// never supplies a handle, a path, an argument, a coordinate or a key: it picks an opaque surface it was
// listed, a scrollable control of a fresh local observation, a closed scroll step, or a registered app id.

export const DESKTOP_ACTION_OPERATIONS = [
  'focus_surface', 'scroll_control', 'launch_app', 'set_control_value', 'select_control', 'invoke_control'
] as const
export type AgentDesktopActionOperation = typeof DESKTOP_ACTION_OPERATIONS[number]

export const DESKTOP_SCROLL_STEPS = ['small_up', 'small_down', 'page_up', 'page_down'] as const
export type AgentDesktopScrollStep = typeof DESKTOP_SCROLL_STEPS[number]

export const DESKTOP_ACTION_STATUSES = [
  'PROPOSED', 'WAITING_APPROVAL', 'APPROVED', 'AUTHORIZED', 'REJECTED', 'EXECUTING', 'SUCCEEDED', 'FAILED',
  'OUTCOME_UNKNOWN', 'RECONCILING'
] as const
export type AgentDesktopActionStatus = typeof DESKTOP_ACTION_STATUSES[number]

export interface AgentRegisteredApp {
  appId: string
  /** From the trusted registry. */
  label: string
}

export interface AgentDesktopScrollTarget {
  controlRef: string
  role: string
  /** Untrusted display text. */
  name: string
}

export interface AgentDesktopScrollTargetList {
  observationId: string
  targets: AgentDesktopScrollTarget[]
}

export interface AgentDesktopActionResultView {
  outcome?: string
  percentBefore?: number
  percentAfter?: number
  /** The person used the machine while the effect ran. Automation stopped and re-read. */
  humanInputDuring?: boolean
  observationInvalidated?: boolean
  followUpObservationId?: string
  focused?: boolean
}

export interface AgentDesktopActionView {
  actionId: string
  revision: number
  status: AgentDesktopActionStatus
  operation: AgentDesktopActionOperation
  /** Untrusted display text (from the trusted registry for a launch). */
  applicationLabel: string
  /** Untrusted display text. Absent for a launch. */
  windowTitle?: string
  controlRole?: string
  /** Untrusted display text. */
  controlName?: string
  step?: AgentDesktopScrollStep
  appId?: string
  /**
   * S4 `set_control_value` only: the exact trusted text that will be written. The person's own input,
   * shown back to them verbatim.
   */
  value?: string
  containerRole?: string
  /** Untrusted display text. */
  containerName?: string
  optionRole?: string
  /** Untrusted display text. */
  optionName?: string
  /** S4 `invoke_control` only: the closed, controller-derived effect being invoked. */
  effect?: AgentDesktopInvokeEffect
  expiresAt?: string
  attemptOutcome?: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN'
  errorCode?: string
  result?: AgentDesktopActionResultView
}

// ---- Milestone 9 S4: bounded semantic desktop actions ------------------------------------------
//
// Three more effects, each on one control re-resolved from the current tree, each behind its OWN
// separate exact trusted approval -- never the same click that approved seeing the snapshot. The
// renderer never picks the operation, the target or (for `set_control_value`) the value directly: it
// only ever reviews and approves what a validated model proposal, re-verified by the controller,
// already narrowed to one closed action.

export const DESKTOP_INVOKE_EFFECTS = ['name_toggle'] as const
export type AgentDesktopInvokeEffect = typeof DESKTOP_INVOKE_EFFECTS[number]

export const DESKTOP_PLAN_PHASES = [
  'awaiting_approval', 'approved', 'reasoning', 'proposed', 'failed', 'outcome_unknown', 'declined', 'expired'
] as const
export type AgentDesktopPlanPhase = typeof DESKTOP_PLAN_PHASES[number]

/** One candidate value the person typed directly in the trusted panel, before any plan is created. */
export interface AgentPlanValueInput {
  /** A short label the person chose for their own value (e.g. "search text"). Never inferred. */
  classification: string
  value: string
}

/** What the model is shown for one candidate value: never the text itself. */
export interface AgentDesktopValueDescriptor {
  valueRef: string
  classification: string
  length: number
}

/** Card-only: the person's own value, shown back to them. Never sent to a provider raw. */
export interface AgentDesktopPlanValueView extends AgentDesktopValueDescriptor {
  value: string
}

export interface AgentDesktopPlanCardView {
  grantId: string
  grantRevision: number
  grantStatus: 'PENDING' | 'ACTIVE' | 'REVOKED' | 'EXPIRED' | 'COMPLETED'
  expiresAt?: string
  recipient: AgentDisclosureRecipient
  model: string
  observedAt: string
  applicationLabel: string
  windowTitle: string
  maxNodes: number
  maxTextBytes: number
  redactionPolicy: string
  observationAvailable: boolean
  nodeCount?: number
  textBytes?: number
  redactionCount?: number
  truncated?: boolean
  truncation: string[]
  values: AgentDesktopPlanValueView[]
}

/** The ONE closed action a provider proposed. Opaque refs only -- never a raw value. */
export type AgentProposedAction =
  | { action: 'invoke'; controlRef: string }
  | { action: 'set_value'; controlRef: string; valueRef: string }
  | { action: 'select'; containerRef: string; optionRef: string }

export interface AgentDesktopPlanStateView {
  planId: string
  status: 'STARTED' | 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN'
  errorCode?: string
  startedAt: string
  finishedAt?: string
  nodeCount: number
  textBytes: number
  redactionCount: number
  truncated: boolean
  proposedAction?: AgentProposedAction
}

export interface AgentDesktopPlanView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  /** The user's own typed objective. */
  objective: string
  phase: AgentDesktopPlanPhase
  card?: AgentDesktopPlanCardView
  plan?: AgentDesktopPlanStateView
  /** Set once a plan succeeded and the execution card has been opened. */
  actionId?: string
}

// ---- Milestone 9 S5: scoped desktop visual fallback -----------------------------------------
//
// Two SEPARATE trusted cards for one task: a capture card (no provider named) and, only after a
// capture succeeds, a disclosure card (one provider/model/purpose). A vision result is evidence
// only -- there is no field anywhere in this section for a click, a key, a coordinate or an action.

export const DESKTOP_FALLBACK_REASONS = [
  'uia_empty', 'uia_missing_required_semantics', 'uia_truncated_without_target'
] as const
export type AgentDesktopFallbackReason = typeof DESKTOP_FALLBACK_REASONS[number]

export const DESKTOP_VISION_PHASES = [
  'awaiting_approval', 'approved', 'capturing', 'captured', 'capture_failed', 'capture_outcome_unknown',
  'awaiting_disclosure_approval', 'disclosure_approved', 'sending_to_provider', 'candidates_ready',
  'disclosure_failed', 'disclosure_outcome_unknown', 'declined'
] as const
export type AgentDesktopVisionPhase = typeof DESKTOP_VISION_PHASES[number]

type AgentVisionAttemptStatus = 'STARTED' | 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN'

export interface AgentDesktopCaptureCardView {
  grantId: string
  grantRevision: number
  grantStatus: AgentGrantStatus
  expiresAt?: string
  applicationLabel: string
  windowTitle: string
  /** The deterministic, locally-computed reason UIA was insufficient. Never model-chosen. */
  fallbackReason: AgentDesktopFallbackReason
}

export interface AgentDesktopCaptureStateView {
  captureId: string
  status: AgentVisionAttemptStatus
  errorCode?: string
  startedAt: string
  finishedAt?: string
  width?: number
  height?: number
  dpi?: number
}

export interface AgentDesktopDisclosureCardView {
  grantId: string
  grantRevision: number
  grantStatus: AgentGrantStatus
  expiresAt?: string
  applicationLabel: string
  windowTitle: string
  provider: AgentDisclosureRecipient
  model: string
  /** The person's own typed purpose for sending this image. */
  purpose: string
}

/** A region normalised to the captured crop (0..1). Never a screen or window coordinate. */
export interface AgentVisionRegion {
  x: number
  y: number
  w: number
  h: number
}

/** One piece of visual evidence. Never authority: nothing here can express a click or an action. */
export interface AgentVisionCandidate {
  label: string
  region: AgentVisionRegion
  confidence: number
  observedText?: string
}

export interface AgentDesktopDisclosureStateView {
  disclosureId: string
  status: AgentVisionAttemptStatus
  errorCode?: string
  startedAt: string
  finishedAt?: string
  candidateCount?: number
}

export interface AgentDesktopVisionView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  /** The user's own typed objective: why this fallback was requested. */
  objective: string
  phase: AgentDesktopVisionPhase
  captureCard?: AgentDesktopCaptureCardView
  capture?: AgentDesktopCaptureStateView
  disclosureCard?: AgentDesktopDisclosureCardView
  disclosure?: AgentDesktopDisclosureStateView
  candidates?: AgentVisionCandidate[]
}

export const AGENT_ERROR_CODES = [
  'runtime_unavailable',
  'runtime_restarted',
  'busy',
  'invalid_request',
  'invalid_response',
  'no_active_task',
  'active_task_unresolved',
  'not_found',
  'stale_revision',
  'approval_not_usable',
  'invalid_transition',
  'action_already_open',
  'slot_unavailable',
  'criteria_mismatch',
  'invalid_criteria',
  'already_booked',
  'browser_unavailable',
  'not_accepting_actions',
  'destination_not_allowed',
  'inspection_unavailable',
  'stale_observation',
  'answer_unavailable',
  'research_unavailable',
  'research_not_granted',
  'research_refused',
  'research_budget_exhausted',
  'research_in_flight',
  // Milestone 8a S3: authenticated account reading.
  'authenticated_unavailable',
  'authenticated_not_granted',
  'authenticated_refused',
  'authenticated_budget_exhausted',
  'authenticated_in_flight',
  // Milestone 8b S5: form planning and the exact disclosure approval.
  'form_plan_refused',
  'form_plan_stale',
  // The one approved AI provider could not be reached. Lumi stopped; it never tries another.
  'model_unavailable',
  // Milestone 8a S1/S2: a browser-profile or login-takeover operation was
  // refused. One code for both server-side families; the message already
  // names the specific reason.
  'browser_profile_refused',
  // Milestone 9 S2: a desktop read was refused (a credential or elevated window, a surface that
  // changed) or its approval moved on since the card. The message names the specific reason.
  'desktop_refused',
  'desktop_read_stale',
  // Milestone 10 S1: a document request was refused, or the approved file/document/approval changed.
  'document_refused',
  'document_state_changed',
  'request_failed'
] as const
export type AgentErrorCode = typeof AGENT_ERROR_CODES[number]

export interface AgentError {
  code: AgentErrorCode
  message: string
  currentRevision?: number
}

export type AgentResult<T> = { ok: true; value: T } | { ok: false; error: AgentError }

/**
 * The renderer's whole agent capability. Every mutation names an action by id
 * and the revision the user reviewed; nothing can carry a doctor, time, price,
 * proposal or digest. There is no generic request, URL or dispatch method.
 */
export interface AgentApi extends AgentDocumentApi {
  getRuntimeStatus: () => Promise<AgentRuntimeView>
  onRuntimeStatus: (listener: (status: AgentRuntimeView) => void) => () => void
  restartRuntime: () => Promise<AgentResult<AgentRuntimeView>>
  /** Read-only: the persisted active task and events after `afterSequence`. */
  loadActiveTask: (afterSequence: number) => Promise<AgentResult<AgentTaskSnapshot | null>>
  createBookingTask: (criteria: AgentBookingCriteria) => Promise<AgentResult<AgentTaskSnapshot>>
  closeActiveTask: () => Promise<AgentResult<null>>
  searchAppointments: () => Promise<AgentResult<AgentSlotView[]>>
  prepareBooking: (slotId: string) => Promise<AgentResult<AgentActionView>>
  requestApproval: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentActionView>>
  approveAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentActionView>>
  rejectAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentActionView>>
  executeAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentActionView>>
  reconcileAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentActionView>>
  /**
   * One closed voice command bound to a completed user turn. Main validates
   * it against durable state; no command can approve or execute a booking.
   */
  voiceCommand: (command: VoiceTaskCommand) => Promise<AgentResult<VoiceTaskOutcome>>
  /**
   * A typed request ("find a dermatologist tomorrow evening and prepare the
   * cheapest"). Main interprets it into the same bounded plan voice uses; it
   * can never approve or execute.
   */
  submitTextRequest: (requestId: string, text: string) => Promise<AgentResult<VoiceTaskOutcome>>
  /**
   * The main composer's first stop. Main decides whether a durable-agent
   * capability owns the request; only an unhandled request may continue to
   * the realtime conversation. Never approves or executes anything.
   */
  routeTypedRequest: (requestId: string, text: string) => Promise<TypedRequestRoute>
  /** Read-only lookup for the active clinic-info task. */
  lookupClinicInfo: () => Promise<AgentResult<AgentDoctorProfileView[]>>
  /**
   * Milestone 7a. Create a page-inspection task from a URL and question the
   * user typed, and show its exact approval card. Opens nothing.
   */
  createPageInspection: (url: string, question: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /** The trusted click: approve exactly the inspection on screen, by id and revision. */
  approveInspection: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentInspectionView>>
  rejectInspection: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentInspectionView>>
  /** Run an approved inspection once, then answer from what was observed. */
  executeInspection: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentInspectionView>>
  /** Answer from the stored observation (after a restart or failed model call). Opens nothing. */
  answerInspection: (actionId: string) => Promise<AgentResult<AgentInspectionView>>
  /** A new inspection of the same page and question, behind a new approval card. */
  inspectPageAgain: () => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Milestone 7b. Create a public-research task from an objective the user
   * typed and show its bounded scope card. Searches nothing and opens nothing.
   */
  createResearchTask: (objective: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * The trusted click: confirm exactly the scope on screen, by grant id and
   * the revision that was shown. This is the only way research becomes
   * possible; no voice command, typed sentence or model output reaches it.
   */
  grantResearchScope: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Decline the scope card. Nothing was searched or opened. */
  declineResearchScope: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Run the bounded research loop under the active scope: plan one step, run
   * it, observe, replan, and stop at the goal or a budget. Safe to call again
   * after it returns; it never runs two loops at once.
   */
  runResearch: () => Promise<AgentResult<AgentTaskSnapshot>>
  /** Stop now: withdraw the scope, drop the browser session, keep the evidence. */
  stopResearch: () => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Milestone 8a S3. Read-only: the providers the trusted UI may offer for an
   * authenticated task, as stable ids from main's own configuration.
   */
  getAuthenticatedOptions: () => Promise<AgentResult<AgentAuthenticatedOptions>>
  /**
   * Create an authenticated-read task and show its trusted disclosure card.
   * Carries the user's question, an opaque profile id and one recipient id from
   * `getAuthenticatedOptions` -- never a URL, a cookie, a path, page content or
   * a free-form provider name. Opens no browser and reads nothing.
   */
  createAuthenticatedTask: (objective: string, profileId: string, recipientId: string) => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * The trusted click: allow exactly the scope on screen, by grant id and the
   * revision that was shown. The only way account reading becomes possible.
   * No voice command, typed sentence or model output reaches it.
   */
  grantAuthenticatedScope: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Decline the card. Nothing was opened and nothing left this computer. */
  declineAuthenticatedScope: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Run the bounded account-reading loop under the active scope. */
  runAuthenticated: () => Promise<AgentResult<AgentTaskSnapshot>>
  /** Stop now: withdraw the scope and release the browser. */
  stopAuthenticated: () => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Milestone 8b S5. Show the trusted FORM PLANNING card: which saved details a
   * planner may see masked previews of. Carries closed data-ref ids only. Sends
   * nothing to any provider and opens nothing.
   */
  prepareFormPlanning: (allowedDataRefs: AgentProtectedDataKind[]) => Promise<AgentResult<AgentTaskSnapshot>>
  /** The trusted "Allow planning" click, by grant id and the revision shown. */
  grantFormPlanning: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Decline the planning card. Nothing was sent. */
  declineFormPlanning: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Ask the one named planner for a `prepare_form` proposal and show its exact manifest card. */
  runFormPlanning: () => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * The trusted click on the manifest card: approve exactly this disclosure, by
   * action id and the revision shown. Carries no manifest, value, origin, field or
   * provider -- the runtime resolves all of it from persisted state. It changes no website.
   */
  approveFieldDisclosure: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  rejectFieldDisclosure: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /**
   * Milestone 8b S6. Open the headed preparation window: the same profile, the same page, a
   * completely fresh observation. Carries nothing. Writes nothing.
   */
  startFormPreparationMode: () => Promise<AgentResult<AgentTaskSnapshot>>
  /** Stop: discard any local draft while the network is frozen, then close the window. */
  stopFormPreparation: () => Promise<AgentResult<AgentTaskSnapshot>>
  /** Discard the local draft: the page is destroyed while frozen, then the network returns. */
  discardFormDraft: (draftId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Ask for the second, exact approval that lets the page send. Changes nothing yet. */
  prepareFormHandover: (draftId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** The trusted "Hand over to me" click: the network is restored for the user. Never submits. */
  approveFormHandover: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  /** Cancel the handover; the network stays frozen. */
  rejectFormHandover: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentTaskSnapshot>>
  listPreferences: () => Promise<AgentResult<AgentPreferenceView[]>>
  forgetPreference: (key: PreferenceKey) => Promise<AgentResult<AgentPreferenceView[]>>
  /** Redacted model/controller diagnostics. Empty in packaged builds unless enabled. */
  getDiagnostics: () => Promise<AgentResult<ModelDiagnosticView[]>>
  /**
   * Milestone 8a S2. Read-only: every Lumi-managed browser profile main
   * knows about. Takes no argument -- there is no channel that creates a
   * profile from a hostname the renderer chose.
   */
  listBrowserProfiles: () => Promise<AgentResult<AgentBrowserProfileView[]>>
  /**
   * The trusted "Sign in manually" click. Names the profile by id and the
   * revision shown on screen; carries nothing else. Opens a headed,
   * Lumi-managed Chromium window for the human to drive. Agent automation
   * (planner, provider, observation, capture) is suspended for the life of
   * the takeover this starts.
   */
  openLoginWindow: (profileId: string, expectedRevision: number) => Promise<AgentResult<AgentLoginTakeoverView>>
  /**
   * The trusted "I'm signed in" click. Not, by itself, an authentication
   * claim -- it only starts the deterministic post-login check whose result
   * `AgentLoginTakeoverView.profile.status` and `.refusalReason` report.
   */
  confirmSignedIn: (profileId: string, attemptId: string, expectedRevision: number) => Promise<AgentResult<AgentLoginTakeoverView>>
  /** The trusted "Cancel" click. Never a logout. */
  cancelLogin: (profileId: string, attemptId: string, expectedRevision: number) => Promise<AgentResult<AgentLoginTakeoverView>>
  /** Read-only: one takeover's bounded interval and how it ended. */
  getLoginTakeover: (profileId: string, attemptId: string) => Promise<AgentResult<AgentLoginAttemptView>>
  /**
   * Milestone 9 S2. The user-visible Windows surfaces, as opaque `(workerGeneration, surfaceRef,
   * surfaceEpoch)` identities plus display labels. Takes no argument. Read-only.
   */
  listDesktopSurfaces: () => Promise<AgentResult<AgentDesktopSurfaceList>>
  /**
   * Choose ONE surface and type a question. Lumi inspects it LOCALLY and opens the trusted disclosure
   * card. Nothing is sent anywhere. The question never goes through the conversation model, and
   * the renderer cannot choose the provider: main does, from its own configuration.
   */
  createDesktopRead: (objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number) => Promise<AgentResult<AgentDesktopReadView>>
  /** Read-only: the newest desktop read, or none. */
  getDesktopRead: () => Promise<AgentResult<AgentDesktopReadView | null>>
  /** The trusted "Allow once" click: names the grant and the revision the card showed. */
  grantDesktopDisclosure: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopReadView>>
  /** The trusted "Cancel" click. Before the claim, nothing was sent. */
  declineDesktopDisclosure: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopReadView>>
  /** Run the approved read: ONE call to the approved provider. There is no retry and no other provider. */
  runDesktopRead: () => Promise<AgentResult<AgentDesktopReadView>>
  /** Milestone 9 S3. Read-only: the registered applications (ids and labels). */
  listDesktopApps: () => Promise<AgentResult<AgentRegisteredApp[]>>
  /** Observe one surface LOCALLY and list only its scrollable controls. Nothing is sent anywhere. */
  findDesktopScrollTargets: (workerGeneration: string, surfaceRef: string, surfaceEpoch: number) => Promise<AgentResult<AgentDesktopScrollTargetList>>
  /** Open the exact approval card for bringing ONE surface to the front. Nothing happens yet. */
  proposeDesktopFocus: (workerGeneration: string, surfaceRef: string, surfaceEpoch: number) => Promise<AgentResult<AgentDesktopActionView>>
  /** Open the exact approval card for ONE semantic scroll of a control of a fresh observation. */
  proposeDesktopScroll: (workerGeneration: string, observationId: string, controlRef: string, step: AgentDesktopScrollStep) => Promise<AgentResult<AgentDesktopActionView>>
  /** Open the exact approval card for opening ONE registered application by id. */
  proposeDesktopLaunch: (appId: string) => Promise<AgentResult<AgentDesktopActionView>>
  /** Read-only: the newest desktop action, or none. */
  getDesktopAction: () => Promise<AgentResult<AgentDesktopActionView | null>>
  /** The trusted "Approve" click: names the action and the revision the card showed. Performs ONE effect. */
  approveDesktopAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopActionView>>
  /** The trusted "Cancel" click. A declined action can never run. */
  declineDesktopAction: (actionId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopActionView>>
  /** The only way out of an unresolved S4 mutation: what the person actually observed. Never retries. */
  reconcileDesktopAction: (
    actionId: string, expectedRevision: number, outcome: 'succeeded' | 'failed' | 'still_unknown'
  ) => Promise<AgentResult<AgentDesktopActionView>>

  // ---- Milestone 9 S4: bounded desktop-action planning ------------------------------------------
  //
  // Disclosure authority only: nothing here runs anything. Choose ONE surface, type an objective and
  // (optionally) up to 4 candidate values, and Lumi inspects it locally and opens the planning card.
  /** Choose ONE surface, type an objective and candidate values: opens the planning card. Nothing is sent. */
  createDesktopPlan: (
    objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number, values: AgentPlanValueInput[]
  ) => Promise<AgentResult<AgentDesktopPlanView>>
  /** Read-only: the newest desktop action plan, or none. */
  getDesktopPlan: () => Promise<AgentResult<AgentDesktopPlanView | null>>
  /** The trusted "Allow once" click on the planning card. */
  grantDesktopPlan: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopPlanView>>
  /** The trusted "Cancel" click. Before the claim, nothing was sent. */
  declineDesktopPlan: (grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopPlanView>>
  /** Run the approved plan: ONE call to the approved provider. There is no retry and no other provider. */
  runDesktopPlan: () => Promise<AgentResult<AgentDesktopPlanView>>
  /**
   * Open the SECOND, separate exact execution approval card from one SUCCEEDED plan. Nothing happens
   * yet: this is the moment disclosure authority ends and execution review begins.
   */
  proposeDesktopActionFromPlan: (planId: string) => Promise<AgentResult<AgentDesktopActionView>>

  // ---- Milestone 9 S5: scoped desktop visual fallback -------------------------------------------
  //
  // UIA-first, always: this is offered only when deterministic local code already found UIA
  // insufficient. A capture requires its own trusted approval before a single pixel is taken;
  // sending an image to a provider requires a SECOND, separate approval, naming the provider, model
  // and purpose, and always takes a brand-new screenshot rather than reusing the first one.
  /** Open the capture card for ONE surface, only if UIA was deterministically insufficient. */
  createDesktopCapture: (
    objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number, targetHint?: string
  ) => Promise<AgentResult<AgentDesktopVisionView>>
  /** Read-only: the current desktop visual-fallback task, or none. */
  getDesktopCapture: (taskId: string) => Promise<AgentResult<AgentDesktopVisionView>>
  /** The trusted "Allow once" click on the capture card. */
  grantDesktopCapture: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopVisionView>>
  /** The trusted "Cancel" click. Before the claim, nothing was captured. */
  declineDesktopCapture: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopVisionView>>
  /** Claim the capture approval: ONE screenshot, for local use only (on-device display, local OCR). */
  runDesktopCapture: (taskId: string) => Promise<AgentResult<AgentDesktopVisionView>>
  /**
   * Open a SEPARATE trusted card naming one provider/model (main's own choice, never a renderer
   * field) and the person's own typed purpose, for the already-succeeded capture.
   */
  createDesktopVisionDisclosure: (taskId: string, purpose: string) => Promise<AgentResult<AgentDesktopVisionView>>
  /** The trusted "Allow once" click on the vision-disclosure card. */
  grantDesktopVisionDisclosure: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopVisionView>>
  /** The trusted "Cancel" click. Before the claim, no image was sent. */
  declineDesktopVisionDisclosure: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDesktopVisionView>>
  /** Claim the disclosure approval: ONE brand-new screenshot, sent once to the one approved provider. */
  runDesktopVisionDisclosure: (taskId: string) => Promise<AgentResult<AgentDesktopVisionView>>
}

/**
 * How main routed one request typed in the main composer. Exactly one path
 * owns a request: `handled` means the durable agent took it (successfully or
 * not) and it must not also reach the realtime conversation, where a legacy
 * tool could act on it; `handled: false` means no durable-agent capability
 * claimed it and nothing was done.
 */
export type TypedRequestRoute =
  | { handled: false }
  | { handled: true; result: AgentResult<VoiceTaskOutcome> }

export const AGENT_IPC_CHANNELS = {
  getRuntimeStatus: 'lifelens:agent:get-runtime-status',
  runtimeStatusChanged: 'lifelens:agent:runtime-status-changed',
  restartRuntime: 'lifelens:agent:restart-runtime',
  loadActiveTask: 'lifelens:agent:load-active-task',
  createBookingTask: 'lifelens:agent:create-booking-task',
  closeActiveTask: 'lifelens:agent:close-active-task',
  searchAppointments: 'lifelens:agent:search-appointments',
  prepareBooking: 'lifelens:agent:prepare-booking',
  requestApproval: 'lifelens:agent:request-approval',
  approveAction: 'lifelens:agent:approve-action',
  rejectAction: 'lifelens:agent:reject-action',
  executeAction: 'lifelens:agent:execute-action',
  reconcileAction: 'lifelens:agent:reconcile-action',
  voiceCommand: 'lifelens:agent:voice-command',
  submitTextRequest: 'lifelens:agent:submit-text-request',
  routeTypedRequest: 'lifelens:agent:route-typed-request',
  lookupClinicInfo: 'lifelens:agent:lookup-clinic-info',
  createPageInspection: 'lifelens:agent:create-page-inspection',
  approveInspection: 'lifelens:agent:approve-inspection',
  rejectInspection: 'lifelens:agent:reject-inspection',
  executeInspection: 'lifelens:agent:execute-inspection',
  answerInspection: 'lifelens:agent:answer-inspection',
  inspectPageAgain: 'lifelens:agent:inspect-page-again',
  createResearchTask: 'lifelens:agent:create-research-task',
  grantResearchScope: 'lifelens:agent:grant-research-scope',
  declineResearchScope: 'lifelens:agent:decline-research-scope',
  runResearch: 'lifelens:agent:run-research',
  stopResearch: 'lifelens:agent:stop-research',
  getAuthenticatedOptions: 'lifelens:agent:get-authenticated-options',
  createAuthenticatedTask: 'lifelens:agent:create-authenticated-task',
  grantAuthenticatedScope: 'lifelens:agent:grant-authenticated-scope',
  declineAuthenticatedScope: 'lifelens:agent:decline-authenticated-scope',
  runAuthenticated: 'lifelens:agent:run-authenticated',
  stopAuthenticated: 'lifelens:agent:stop-authenticated',
  prepareFormPlanning: 'lifelens:agent:prepare-form-planning',
  grantFormPlanning: 'lifelens:agent:grant-form-planning',
  declineFormPlanning: 'lifelens:agent:decline-form-planning',
  runFormPlanning: 'lifelens:agent:run-form-planning',
  approveFieldDisclosure: 'lifelens:agent:approve-field-disclosure',
  rejectFieldDisclosure: 'lifelens:agent:reject-field-disclosure',
  startFormPreparationMode: 'lifelens:agent:start-form-preparation-mode',
  stopFormPreparation: 'lifelens:agent:stop-form-preparation',
  discardFormDraft: 'lifelens:agent:discard-form-draft',
  prepareFormHandover: 'lifelens:agent:prepare-form-handover',
  approveFormHandover: 'lifelens:agent:approve-form-handover',
  rejectFormHandover: 'lifelens:agent:reject-form-handover',
  listPreferences: 'lifelens:agent:list-preferences',
  forgetPreference: 'lifelens:agent:forget-preference',
  getDiagnostics: 'lifelens:agent:get-diagnostics',
  listBrowserProfiles: 'lifelens:agent:list-browser-profiles',
  openLoginWindow: 'lifelens:agent:open-login-window',
  confirmSignedIn: 'lifelens:agent:confirm-signed-in',
  cancelLogin: 'lifelens:agent:cancel-login',
  getLoginTakeover: 'lifelens:agent:get-login-takeover',
  // Milestone 9 S2: exact desktop disclosure. Six fixed channels; none takes a method, a route, a
  // provider, a snapshot, a handle or a coordinate, and none performs any desktop action.
  listDesktopSurfaces: 'lifelens:agent:list-desktop-surfaces',
  createDesktopRead: 'lifelens:agent:create-desktop-read',
  getDesktopRead: 'lifelens:agent:get-desktop-read',
  grantDesktopDisclosure: 'lifelens:agent:grant-desktop-disclosure',
  declineDesktopDisclosure: 'lifelens:agent:decline-desktop-disclosure',
  runDesktopRead: 'lifelens:agent:run-desktop-read',
  listDesktopApps: 'lifelens:agent:list-desktop-apps',
  findDesktopScrollTargets: 'lifelens:agent:find-desktop-scroll-targets',
  proposeDesktopFocus: 'lifelens:agent:propose-desktop-focus',
  proposeDesktopScroll: 'lifelens:agent:propose-desktop-scroll',
  proposeDesktopLaunch: 'lifelens:agent:propose-desktop-launch',
  getDesktopAction: 'lifelens:agent:get-desktop-action',
  approveDesktopAction: 'lifelens:agent:approve-desktop-action',
  declineDesktopAction: 'lifelens:agent:decline-desktop-action',
  reconcileDesktopAction: 'lifelens:agent:reconcile-desktop-action',
  // Milestone 9 S4: bounded desktop-action planning. Disclosure authority only; none of these
  // channels performs any desktop action.
  createDesktopPlan: 'lifelens:agent:create-desktop-plan',
  getDesktopPlan: 'lifelens:agent:get-desktop-plan',
  grantDesktopPlan: 'lifelens:agent:grant-desktop-plan',
  declineDesktopPlan: 'lifelens:agent:decline-desktop-plan',
  runDesktopPlan: 'lifelens:agent:run-desktop-plan',
  proposeDesktopActionFromPlan: 'lifelens:agent:propose-desktop-action-from-plan',
  // Milestone 9 S5: scoped desktop visual fallback. A capture requires its own approval before a
  // single pixel is taken; a vision-provider disclosure requires a SEPARATE approval and always
  // takes a brand-new screenshot.
  createDesktopCapture: 'lifelens:agent:create-desktop-capture',
  getDesktopCapture: 'lifelens:agent:get-desktop-capture',
  grantDesktopCapture: 'lifelens:agent:grant-desktop-capture',
  declineDesktopCapture: 'lifelens:agent:decline-desktop-capture',
  runDesktopCapture: 'lifelens:agent:run-desktop-capture',
  createDesktopVisionDisclosure: 'lifelens:agent:create-desktop-vision-disclosure',
  grantDesktopVisionDisclosure: 'lifelens:agent:grant-desktop-vision-disclosure',
  declineDesktopVisionDisclosure: 'lifelens:agent:decline-desktop-vision-disclosure',
  runDesktopVisionDisclosure: 'lifelens:agent:run-desktop-vision-disclosure',
  // Milestone 10 S1: M10 file roots and approved documents. Ids, revisions, labels, booleans and a
  // typed purpose only; the one channel that involves a folder opens a NATIVE dialog in main.
  listFileRoots: 'lifelens:agent:list-file-roots',
  addFileRoot: 'lifelens:agent:add-file-root',
  revokeFileRoot: 'lifelens:agent:revoke-file-root',
  listFileRootFiles: 'lifelens:agent:list-file-root-files',
  createDocumentTask: 'lifelens:agent:create-document-task',
  getDocumentTask: 'lifelens:agent:get-document-task',
  addDocumentFromRoot: 'lifelens:agent:add-document-from-root',
  addDroppedDocument: 'lifelens:agent:add-dropped-document',
  extractDocument: 'lifelens:agent:extract-document',
  compareDocumentsLocally: 'lifelens:agent:compare-documents-locally',
  createDocumentDisclosure: 'lifelens:agent:create-document-disclosure',
  grantDocumentDisclosure: 'lifelens:agent:grant-document-disclosure',
  declineDocumentDisclosure: 'lifelens:agent:decline-document-disclosure',
  runDocumentDisclosure: 'lifelens:agent:run-document-disclosure'
} as const

/** Actions whose side effect is unresolved or in flight. */
export const UNRESOLVED_ACTION_STATUSES: readonly AgentActionStatus[] = ['EXECUTING', 'OUTCOME_UNKNOWN', 'RECONCILING']

/** Prepared, possibly approved or authorized, never executed: safe to reject. */
export const OPEN_ACTION_STATUSES: readonly AgentActionStatus[] = ['PROPOSED', 'WAITING_APPROVAL', 'APPROVED', 'AUTHORIZED']

export const TERMINAL_TASK_STATUSES: readonly AgentTaskStatus[] = ['SUCCEEDED', 'FAILED', 'CANCELLED']
