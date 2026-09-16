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

export const TASK_STATUSES = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING',
  'OUTCOME_UNKNOWN', 'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
] as const
export type AgentTaskStatus = typeof TASK_STATUSES[number]

export const ACTION_STATUSES = [
  'PROPOSED', 'WAITING_APPROVAL', 'APPROVED', 'REJECTED', 'EXECUTING', 'SUCCEEDED',
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
  'task.created', 'task.cancelled', 'action.proposed', 'action.approval_requested',
  'action.approved', 'action.rejected', 'action.execution_started', 'action.succeeded',
  'action.failed', 'action.outcome_unknown', 'action.reconciliation_started', 'action.reconciled'
] as const
export type AgentTaskEventType = typeof TASK_EVENT_TYPES[number]

export const BOOKING_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] as const
export type AgentBookingDay = typeof BOOKING_DAYS[number]

export type AgentRuntimeState = 'stopped' | 'starting' | 'running' | 'unavailable' | 'failed' | 'stopping' | 'not_installed'

export interface AgentRuntimeView {
  state: AgentRuntimeState
  /** Opaque process-generation id. Not a credential; shown in technical details. */
  generation?: string
}

export interface AgentBookingCriteria {
  specialty: string
  day: AgentBookingDay | ''
}

export interface AgentTaskView {
  taskId: string
  status: AgentTaskStatus
  revision: number
  lastEventSequence: number
  criteria: AgentBookingCriteria
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
  actionStatus?: AgentActionStatus
  actionRevision?: number
  attemptId?: string
  attemptNumber?: number
  approvalId?: string
  outcome?: AgentAttemptOutcome
  errorCode?: string
  reason?: string
  reconciliation?: AgentReconciliationView
}

export interface AgentTaskSnapshot {
  runtimeGeneration: string
  task: AgentTaskView
  actions: AgentActionView[]
  /** Events strictly after the requested sequence, ordered, without gaps. */
  events: AgentEventView[]
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
  'browser_unavailable',
  'not_accepting_actions',
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
export interface AgentApi {
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
}

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
  reconcileAction: 'lifelens:agent:reconcile-action'
} as const

/** Actions whose side effect is unresolved or in flight. */
export const UNRESOLVED_ACTION_STATUSES: readonly AgentActionStatus[] = ['EXECUTING', 'OUTCOME_UNKNOWN', 'RECONCILING']
