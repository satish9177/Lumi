import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  BOOKING_DAYS,
  CLINIC_INFO_TOPICS,
  PROTECTED_DATA_KINDS,
  UNRESOLVED_ACTION_STATUSES,
  type AgentActionView,
  type AgentAuthenticatedOptions,
  type AgentAuthenticatedView,
  type AgentFormPlanView,
  type AgentProtectedDataKind,
  type AgentBookingCriteria,
  type AgentClinicInfoQuery,
  type AgentDisclosureRecipient,
  type AgentDoctorProfileView,
  type AgentError,
  type AgentEventView,
  type AgentInspectionView,
  type AgentResearchStopReason,
  type AgentResult,
  type AgentSlotView,
  type AgentTaskSnapshot
} from '../../shared/agent-contracts'
import {
  RuntimeRestartedError,
  RuntimeUnavailableError,
  type RuntimeMethod,
  type RuntimeReply
} from './agent-runtime-supervisor'
import type { AnswerOutcome, PageObservationDetail } from '../agent/page-answer'
import type { ResearchAnswerOutcome } from '../agent/research-answer'
import type { AuthenticatedAnswerOutcome } from '../agent/authenticated-answer'
import type { FormPlanOutcome, FormPlanningContext } from '../agent/form-planner'
import { setFormDraftWindowActive } from './capture'
import type {
  AuthenticatedDecision, AuthenticatedPlanOutcome, AuthenticatedStepChoice
} from '../agent/authenticated-planner'
import type { ResearchDecision, ResearchPlanOutcome, ResearchStepChoice } from '../agent/research-planner'
import { PublicUrlPolicy, UrlPolicyError, describeRefusal } from '../agent/public-url-policy'
import {
  WireError,
  isRecord,
  latestInspectionActionId,
  parseAction,
  parseActionList,
  parseInspection,
  parseInspectionAction,
  parseCancellation,
  parseClinicInfo,
  parseCriteriaRevision,
  parseEventPage,
  parseAuthenticated,
  parseAuthenticatedStep,
  parseFormPlan,
  parsePlanningContext,
  parseResearch,
  parseResearchStep,
  parseSearch,
  parseTask,
  projectRuntimeError,
  type CriteriaRevision,
  type InspectionDetail,
  type AuthenticatedDetail,
  type AuthenticatedStepOutcome,
  type ResearchDetail,
  type ResearchStepOutcome,
  type TaskCancellation
} from './agent-wire'

/**
 * The trusted domain client for durable agent tasks, owned by Electron main.
 *
 * It exposes booking, clinic-information and page-inspection task operations
 * and nothing else: there is no generic
 * path, method, body or URL parameter. Every mutation is an explicit user
 * action from the renderer, names an action of the *active* task by id, and
 * carries only the revision the user reviewed. Mutations are never retried;
 * an unconfirmed mutation is reported so the UI re-reads durable state.
 *
 * Python/PostgreSQL stay authoritative. Main persists only which task is
 * active, so the same task is restored after a reload or restart.
 */

export interface RuntimeRequester {
  request(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply>
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SLOT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
const SPECIALTY = /^[A-Za-z][A-Za-z .'-]{0,59}$/
const CLOCK = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const CURRENCY = /^[A-Z]{3}$/
const TURN_ID = /^[A-Za-z0-9_-]{1,64}$/
const CRITERIA_KEYS = new Set(['specialty', 'day', 'earliestTime', 'latestTime', 'maxPrice', 'maxPriceCurrency', 'dateFrom', 'dateTo'])
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const PERSON_NAME = /^[A-Za-z][A-Za-z .'-]{0,59}$/
const MAX_DATE_SPAN_DAYS = 14
const MAX_UTTERANCE = 500
const EVENT_PAGE = 200
const MAX_EVENT_PAGES = 50
// Matches the 15-digit cursor the runtime route allowlist accepts.
const MAX_SEQUENCE = 999_999_999_999_999

const TIMEOUTS = {
  read: 10_000,
  write: 15_000,
  observe: 150_000,
  // Longer than the runtime's own worker timeout: cutting execution short
  // here would only turn a knowable answer into an unconfirmed one.
  execute: 200_000,
  reconcile: 180_000,
  // Reopening the profile headed, returning to the page and observing it takes a browser
  // launch and a page load; a fill waits for a settled page and a two-layer freeze.
  prepare: 180_000
} as const

export class AgentRequestError extends Error {
  constructor(readonly agentError: AgentError) {
    super(agentError.message)
    this.name = 'AgentRequestError'
  }
}

function fail(code: AgentError['code'], message: string): never {
  throw new AgentRequestError({ code, message })
}

export function parseActionId(value: unknown): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', 'That booking reference is invalid.')
  return value
}

export function parseExpectedRevision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
    fail('invalid_request', 'That booking revision is invalid.')
  }
  return value
}

export function parseSlotId(value: unknown): string {
  if (typeof value !== 'string' || !SLOT_ID.test(value)) fail('invalid_request', 'That appointment reference is invalid.')
  return value
}

export function parseAfterSequence(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > MAX_SEQUENCE) {
    fail('invalid_request', 'That timeline position is invalid.')
  }
  return value
}

export function parseBookingCriteria(value: unknown): AgentBookingCriteria {
  if (!isRecord(value) || Object.keys(value).some((key) => !CRITERIA_KEYS.has(key))) {
    fail('invalid_request', 'Booking search criteria are invalid.')
  }
  const specialty = typeof value.specialty === 'string' ? value.specialty.trim() : undefined
  if (specialty === undefined || (specialty !== '' && !SPECIALTY.test(specialty))) {
    fail('invalid_request', 'Enter a specialty using letters only.')
  }
  const day = value.day
  if (day !== '' && !(typeof day === 'string' && (BOOKING_DAYS as readonly string[]).includes(day))) {
    fail('invalid_request', 'Choose a valid day.')
  }
  const criteria: AgentBookingCriteria = { specialty, day: day as AgentBookingCriteria['day'] }
  for (const key of ['earliestTime', 'latestTime'] as const) {
    const bound = value[key]
    if (bound === undefined) continue
    if (typeof bound !== 'string' || !CLOCK.test(bound)) fail('invalid_request', 'Times must be 24-hour HH:MM.')
    criteria[key] = bound
  }
  if (criteria.earliestTime && criteria.latestTime && criteria.earliestTime > criteria.latestTime) {
    fail('invalid_request', 'The earliest time must not be after the latest time.')
  }
  const { maxPrice, maxPriceCurrency } = value
  if ((maxPrice === undefined) !== (maxPriceCurrency === undefined)) {
    fail('invalid_request', 'A price limit needs its currency.')
  }
  if (maxPrice !== undefined) {
    if (typeof maxPrice !== 'number' || !Number.isSafeInteger(maxPrice) || maxPrice < 0 || maxPrice > 10_000_000) {
      fail('invalid_request', 'The price limit is invalid.')
    }
    if (typeof maxPriceCurrency !== 'string' || !CURRENCY.test(maxPriceCurrency)) {
      fail('invalid_request', 'The price currency is invalid.')
    }
    criteria.maxPrice = maxPrice
    criteria.maxPriceCurrency = maxPriceCurrency
  }
  const { dateFrom, dateTo } = value
  if ((dateFrom === undefined) !== (dateTo === undefined)) fail('invalid_request', 'A date range needs both ends.')
  if (dateFrom !== undefined) {
    if (typeof dateFrom !== 'string' || typeof dateTo !== 'string' || !ISO_DATE.test(dateFrom) || !ISO_DATE.test(dateTo)) {
      fail('invalid_request', 'Dates must be YYYY-MM-DD.')
    }
    const from = Date.parse(`${dateFrom}T00:00:00Z`)
    const to = Date.parse(`${dateTo}T00:00:00Z`)
    if (Number.isNaN(from) || Number.isNaN(to) || new Date(from).toISOString().slice(0, 10) !== dateFrom ||
        new Date(to).toISOString().slice(0, 10) !== dateTo) {
      fail('invalid_request', 'Dates must be real calendar dates.')
    }
    if (from > to || (to - from) / 86_400_000 >= MAX_DATE_SPAN_DAYS) fail('invalid_request', 'That date range is not supported.')
    if (criteria.day && from === to && BOOKING_DAYS[(new Date(from).getUTCDay() + 6) % 7] !== criteria.day) {
      fail('invalid_request', 'The day does not match the date.')
    }
    criteria.dateFrom = dateFrom
    criteria.dateTo = dateTo
  }
  return criteria
}

export function parseClinicInfoQuery(value: unknown): AgentClinicInfoQuery {
  if (!isRecord(value) || Object.keys(value).some((key) => !['specialty', 'doctor', 'topic'].includes(key))) {
    fail('invalid_request', 'That clinic question is invalid.')
  }
  const specialty = typeof value.specialty === 'string' ? value.specialty.trim() : ''
  const doctor = typeof value.doctor === 'string' ? value.doctor.normalize('NFKC').trim() : ''
  if (specialty && !SPECIALTY.test(specialty)) fail('invalid_request', 'That clinic question is invalid.')
  if (doctor && !PERSON_NAME.test(doctor)) fail('invalid_request', 'That doctor name is invalid.')
  if (!specialty && !doctor) fail('invalid_request', 'Say which doctor or specialty you mean.')
  const topic = value.topic ?? 'overview'
  if (typeof topic !== 'string' || !(CLINIC_INFO_TOPICS as readonly string[]).includes(topic)) {
    fail('invalid_request', 'That clinic question is invalid.')
  }
  return { specialty, doctor, topic: topic as AgentClinicInfoQuery['topic'] }
}

/** The runtime's snake_case constraint fields. Unset bounds are omitted. */
export function criteriaFields(criteria: AgentBookingCriteria): Record<string, string | number> {
  return {
    ...(criteria.specialty ? { specialty: criteria.specialty } : {}),
    ...(criteria.day ? { day: criteria.day } : {}),
    ...(criteria.earliestTime ? { earliest_time: criteria.earliestTime } : {}),
    ...(criteria.latestTime ? { latest_time: criteria.latestTime } : {}),
    ...(criteria.maxPrice !== undefined && criteria.maxPriceCurrency
      ? { max_price: criteria.maxPrice, max_price_currency: criteria.maxPriceCurrency }
      : {}),
    ...(criteria.dateFrom && criteria.dateTo ? { date_from: criteria.dateFrom, date_to: criteria.dateTo } : {})
  }
}

/**
 * Where a task came from. Only main constructs this (the voice controller);
 * the renderer's create-task IPC passes criteria and nothing else.
 */
export interface TaskOrigin {
  /** A completed voice turn, or a typed request from the task panel. */
  source: 'voice' | 'text'
  turnId: string
  utterance: string
}

function originFields(origin: TaskOrigin | undefined, fallback = 'Book a clinic appointment'): Record<string, string> {
  if (!origin) return { text: fallback }
  if (!TURN_ID.test(origin.turnId)) fail('invalid_request', 'That request reference is invalid.')
  const utterance = origin.utterance.trim().slice(0, MAX_UTTERANCE)
  // A voice turn and a typed request are both durable de-duplication keys.
  const fields: Record<string, string> = { text: utterance || fallback, source: origin.source }
  fields[origin.source === 'voice' ? 'voice_turn_id' : 'request_id'] = origin.turnId
  return fields
}

/** Remembers which durable task is on screen. Never the task's contents. */
export class ActiveTaskStore {
  private readonly path: string

  constructor(userDataDir: string) {
    this.path = join(userDataDir, 'agent-active-task.json')
  }

  async read(): Promise<string | undefined> {
    try {
      const value: unknown = JSON.parse(await readFile(this.path, 'utf8'))
      if (isRecord(value) && value.version === 1 && typeof value.taskId === 'string' && UUID.test(value.taskId)) {
        return value.taskId
      }
    } catch {
      // Missing or unreadable: no active task. Durable state stays in Python.
    }
    return undefined
  }

  async write(taskId: string): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true })
    const temporary = `${this.path}.${randomUUID()}.tmp`
    await writeFile(temporary, JSON.stringify({ version: 1, taskId }), 'utf8')
    await rename(temporary, this.path)
  }

  async clear(): Promise<void> {
    await rm(this.path, { force: true })
  }
}

/**
 * What main needs to offer page inspection: its own destination policy (the
 * runtime and worker apply theirs too) and the answer step, which owns the
 * model router and therefore the provider credentials.
 */
export interface PageInspectionSupport {
  policy: PublicUrlPolicy
  answerer?: {
    recipients(): AgentDisclosureRecipient[]
    answer(input: {
      question: string
      observation: PageObservationDetail
      recipients: readonly AgentDisclosureRecipient[]
      taskId: string
    }): Promise<AnswerOutcome>
  }
}

const MAX_QUESTION = 500
const INSPECTION_TEXT_CHARS = 12_000
const MAX_OBJECTIVE = 500
const RESEARCH_TEXT_CHARS = 10_000
/**
 * How many refused or failed steps a research loop absorbs by re-observing
 * before it stops. Small on purpose: a planner that keeps asking for something
 * the scope does not allow is stopped, not negotiated with.
 */
const RESEARCH_MAX_REFUSALS = 2

export function parseResearchObjective(value: unknown): string {
  const objective = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
  // eslint-disable-next-line no-control-regex
  if (!objective || objective.length > MAX_OBJECTIVE || /[\x00-\x1f\x7f]/.test(objective)) {
    fail('invalid_request', 'Describe what to research in up to 500 characters.')
  }
  return objective
}

/**
 * What main needs to offer public research: its own destination policy (the
 * runtime and worker apply theirs too), the bounded planner, and the answer
 * step. Both model roles live here because main is the only process that holds
 * provider credentials.
 */
export interface ResearchSupport {
  policy: PublicUrlPolicy
  planner?: {
    recipients(): string[]
    next(input: {
      objective: string
      view: AgentResearchViewInput
      taskId: string
      permits?: (id: string) => boolean
    }): Promise<ResearchPlanOutcome>
  }
  answerer?: {
    recipients(): AgentDisclosureRecipient[]
    answer(input: {
      objective: string
      view: AgentResearchViewInput
      recipients: readonly AgentDisclosureRecipient[]
      stopReason: AgentResearchStopReason
      taskId: string
    }): Promise<ResearchAnswerOutcome>
  }
}

type AgentResearchViewInput = ResearchDetail['view']

/**
 * What main needs to offer authenticated account reading (Milestone 8a S3): the
 * bounded planner and the answer step. Both are **pinned to one recipient per
 * call** -- the recipient comes from the confirmed grant and nowhere else -- and
 * neither has a failover path.
 */
export interface AuthenticatedSupport {
  /**
   * Milestone 8b S5. One proposal per call, to the form-planning grant's one recipient.
   * It cannot act: its output is a `prepare_form` proposal the runtime validates.
   */
  formPlanner?: {
    recipients(): AgentDisclosureRecipient[]
    plan(input: { context: FormPlanningContext; taskId: string; recipient: AgentDisclosureRecipient }): Promise<FormPlanOutcome>
  }
  planner?: {
    recipients(): AgentDisclosureRecipient[]
    next(input: {
      objective: string
      view: AgentAuthenticatedView
      taskId: string
      recipient: AgentDisclosureRecipient
    }): Promise<AuthenticatedPlanOutcome>
  }
  answerer?: {
    recipients(): AgentDisclosureRecipient[]
    answer(input: {
      objective: string
      view: AgentAuthenticatedView
      recipient: AgentDisclosureRecipient
      stopReason: AgentResearchStopReason
      taskId: string
    }): Promise<AuthenticatedAnswerOutcome>
  }
}

/**
 * How many refused steps an authenticated loop absorbs by re-observing before
 * it stops. Smaller than public research's: this is somebody's account.
 */
const AUTHENTICATED_MAX_REFUSALS = 2

export function parseAuthenticatedObjective(value: unknown): string {
  const objective = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
  // eslint-disable-next-line no-control-regex
  if (!objective || objective.length > MAX_OBJECTIVE || /[\x00-\x1f\x7f]/.test(objective)) {
    fail('invalid_request', 'Ask a question about your account in up to 500 characters.')
  }
  return objective
}

export function parseInspectionQuestion(value: unknown): string {
  const question = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
  // eslint-disable-next-line no-control-regex
  if (!question || question.length > MAX_QUESTION || /[\x00-\x1f\x7f]/.test(question)) {
    fail('invalid_request', 'Ask a question of up to 500 characters about the page.')
  }
  return question
}

/** Inspection wording for ledger refusals whose default text talks about bookings. */
function inspectionError(agentError: AgentError): AgentError {
  switch (agentError.code) {
    case 'stale_revision':
      return { ...agentError, message: 'The inspection changed since you reviewed it. Review the current card.' }
    case 'invalid_transition':
      return { ...agentError, message: 'That step is not available for this inspection any more.' }
    case 'approval_not_usable':
      return { ...agentError, message: 'That approval can no longer be used. Review the inspection again.' }
    case 'not_found':
      return { ...agentError, message: 'That inspection does not belong to the current task.' }
    default:
      return agentError
  }
}

export class AgentTaskController {
  private readonly inFlight = new Set<string>()

  constructor(
    private readonly runtime: RuntimeRequester,
    private readonly store: ActiveTaskStore,
    private readonly inspection?: PageInspectionSupport,
    private readonly research?: ResearchSupport,
    private readonly authenticated?: AuthenticatedSupport
  ) {}

  // ---- plumbing ------------------------------------------------------------

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) {
        fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was sent.')
      }
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectRuntimeError(reply.status, reply.body))
    return reply
  }

  private async exclusive<T>(key: string, work: () => Promise<T>): Promise<AgentResult<T>> {
    if (this.inFlight.has(key)) {
      return { ok: false, error: { code: 'busy', message: 'Lumi is already working on that.' } }
    }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  private async activeTaskId(): Promise<string> {
    const taskId = await this.store.read()
    if (!taskId) fail('no_active_task', 'Start a booking task first.')
    return taskId
  }

  private async loadActions(taskId: string): Promise<{ actions: AgentActionView[]; generation: string; inspectionId?: string }> {
    const reply = await this.call('GET', `/tasks/${taskId}/actions?limit=100`, undefined, TIMEOUTS.read)
    const inspectionId = latestInspectionActionId(reply.body, taskId)
    return { actions: parseActionList(reply.body, taskId), generation: reply.generation, ...(inspectionId ? { inspectionId } : {}) }
  }

  private async snapshot(taskId: string, afterSequence: number): Promise<AgentTaskSnapshot> {
    const taskReply = await this.call('GET', `/tasks/${taskId}`, undefined, TIMEOUTS.read)
    const task = parseTask(taskReply.body)
    if (task.taskId !== taskId) throw new WireError('task.id')
    const { actions, generation, inspectionId } = await this.loadActions(taskId)
    let inspection: AgentInspectionView | undefined
    if (task.kind === 'page_inspection' && inspectionId) {
      const detail = await this.loadInspection(inspectionId)
      if (detail.generation !== generation) fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
      if (detail.detail.view.taskId !== taskId) throw new WireError('inspection.task_id')
      inspection = detail.detail.view
    }
    let research: AgentResearchViewInput | undefined
    if (task.kind === 'public_research') {
      const loaded = await this.loadResearch(taskId)
      if (loaded.generation !== generation) fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
      research = loaded.detail.view
    }
    let authenticated: AgentAuthenticatedView | undefined
    if (task.kind === 'authenticated_read') {
      const loaded = await this.loadAuthenticated(taskId)
      if (loaded.generation !== generation) fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
      authenticated = loaded.detail.view
    }
    let formPlan: AgentFormPlanView | undefined
    if (task.kind === 'authenticated_read') {
      const loaded = await this.call('GET', `/tasks/${taskId}/authenticated/form`, undefined, TIMEOUTS.read)
      if (loaded.generation !== generation) fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
      formPlan = parseFormPlan(loaded.body)
      if (formPlan.taskId !== taskId) throw new WireError('form_plan.task_id')
    }
    // The preparation window, or a draft that still exists, can show private details: no screen
    // capture (which goes to a model) while it does. Derived here, from the runtime's own answer.
    setFormDraftWindowActive(Boolean(
      formPlan && (formPlan.preparing || formPlan.draft?.status === 'PREPARED' || formPlan.draft?.status === 'STALE')
    ))
    const events: AgentEventView[] = []
    let cursor = afterSequence
    for (let page = 0; ; page += 1) {
      if (page >= MAX_EVENT_PAGES) fail('invalid_response', 'The task timeline is too long to display.')
      const reply = await this.call(
        'GET', `/tasks/${taskId}/events?after_sequence=${cursor}&limit=${EVENT_PAGE}`, undefined, TIMEOUTS.read
      )
      if (reply.generation !== generation) fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
      const batch = parseEventPage(reply.body, taskId, cursor)
      events.push(...batch)
      if (batch.length > 0) cursor = batch[batch.length - 1].sequence
      if (batch.length < EVENT_PAGE) break
    }
    // Sequences are allocated without gaps; the task read happened first, so
    // everything up to its last sequence must now be present.
    if (cursor < task.lastEventSequence) throw new WireError('events.incomplete')
    let expected = afterSequence + 1
    for (const event of events) {
      if (event.sequence !== expected) throw new WireError('events.gap')
      expected += 1
    }
    if (taskReply.generation !== generation) {
      fail('runtime_restarted', 'The agent runtime restarted while loading. Showing the latest saved state.')
    }
    return {
      runtimeGeneration: generation,
      task,
      actions,
      events,
      ...(inspection ? { inspection } : {}),
      ...(research ? { research } : {}),
      ...(authenticated ? { authenticated } : {}),
      ...(formPlan ? { formPlan } : {})
    }
  }

  private async loadInspection(actionId: string): Promise<{ detail: InspectionDetail; generation: string }> {
    const reply = await this.call('GET', `/actions/${actionId}/inspection`, undefined, TIMEOUTS.read)
    const detail = parseInspection(reply.body)
    if (detail.view.actionId !== actionId) throw new WireError('inspection.action_id')
    return { detail, generation: reply.generation }
  }

  private async ownedAction(actionId: string): Promise<AgentActionView> {
    const taskId = await this.activeTaskId()
    const reply = await this.call('GET', `/actions/${actionId}`, undefined, TIMEOUTS.read)
    const action = parseAction(reply.body)
    if (action.actionId !== actionId || action.taskId !== taskId) {
      fail('not_found', 'That booking does not belong to the current task.')
    }
    return action
  }

  private async assertNoUnresolvedAction(): Promise<void> {
    const taskId = await this.store.read()
    if (!taskId) return
    let actions: AgentActionView[]
    try {
      actions = (await this.loadActions(taskId)).actions
    } catch (error) {
      if (error instanceof AgentRequestError && error.agentError.code === 'not_found') return
      throw error
    }
    if (actions.some((action) => UNRESOLVED_ACTION_STATUSES.includes(action.status))) {
      fail('active_task_unresolved', 'The current booking has an unresolved outcome. Check it before starting another task.')
    }
  }

  private async actionMutation(
    kind: string,
    actionIdValue: unknown,
    revisionValue: unknown,
    route: string,
    timeoutMs: number
  ): Promise<AgentResult<AgentActionView>> {
    let actionId: string
    let expectedRevision: number
    try {
      actionId = parseActionId(actionIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    // One mutation per action at a time, whatever its kind.
    return this.exclusive(`action:${actionId}`, async () => {
      const current = await this.ownedAction(actionId)
      if (current.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The booking changed since you reviewed it. Review the current details.',
          currentRevision: current.revision
        })
      }
      const reply = await this.call('POST', `/actions/${actionId}/${route}`, { expected_revision: expectedRevision }, timeoutMs)
      const action = parseAction(reply.body)
      if (action.actionId !== actionId || action.taskId !== current.taskId) throw new WireError(`${kind}.action`)
      return action
    })
  }

  // ---- read ----------------------------------------------------------------

  async loadActiveTask(afterSequenceValue: unknown): Promise<AgentResult<AgentTaskSnapshot | null>> {
    let afterSequence: number
    try {
      afterSequence = parseAfterSequence(afterSequenceValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    try {
      const taskId = await this.store.read()
      if (!taskId) return { ok: true, value: null }
      try {
        return { ok: true, value: await this.snapshot(taskId, afterSequence) }
      } catch (error) {
        // PostgreSQL is authoritative: a task it says does not exist is not
        // restored. Any other failure keeps the pointer for the next attempt.
        if (error instanceof AgentRequestError && error.agentError.code === 'not_found') {
          await this.store.clear()
          return { ok: true, value: null }
        }
        throw error
      }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
  }

  // ---- task lifecycle ------------------------------------------------------

  async createBookingTask(criteriaValue: unknown, origin?: TaskOrigin): Promise<AgentResult<AgentTaskSnapshot>> {
    let criteria: AgentBookingCriteria
    let provenance: Record<string, string>
    try {
      criteria = parseBookingCriteria(criteriaValue)
      provenance = originFields(origin)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      await this.assertNoUnresolvedAction()
      const request = { type: 'appointment_booking', ...provenance, ...criteriaFields(criteria) }
      const reply = await this.call('POST', '/tasks', { request }, TIMEOUTS.write)
      const task = parseTask(reply.body)
      await this.store.write(task.taskId)
      return await this.snapshot(task.taskId, 0)
    })
  }

  /**
   * Replace the active task's constraints, bound to the task revision the
   * caller derived them from. The runtime rejects, in the same transaction,
   * any prepared booking the new constraints exclude.
   */
  async reviseCriteria(criteriaValue: unknown, revisionValue: unknown): Promise<AgentResult<CriteriaRevision>> {
    let criteria: AgentBookingCriteria
    let expectedRevision: number
    try {
      criteria = parseBookingCriteria(criteriaValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const body = {
        expected_revision: expectedRevision,
        criteria: { specialty: criteria.specialty, day: criteria.day, ...criteriaFields(criteria) }
      }
      const reply = await this.call('POST', `/tasks/${taskId}/booking/criteria`, body, TIMEOUTS.write)
      return parseCriteriaRevision(reply.body, taskId)
    })
  }

  /**
   * Cancel the active task. The runtime refuses while a booking may exist and
   * otherwise rejects open bookings with the cancellation. The pointer stays,
   * so the cancelled task and its timeline remain visible until closed.
   */
  async cancelActiveTask(revisionValue: unknown): Promise<AgentResult<TaskCancellation>> {
    let expectedRevision: number
    try {
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/booking/cancel`, { expected_revision: expectedRevision }, TIMEOUTS.write)
      return parseCancellation(reply.body, taskId)
    })
  }

  /**
   * Start the read-only clinic-information workflow. Same guard as a booking
   * task: an unresolved booking must be checked before the panel moves on.
   */
  async createClinicInfoTask(queryValue: unknown, origin?: TaskOrigin): Promise<AgentResult<AgentTaskSnapshot>> {
    let query: AgentClinicInfoQuery
    let provenance: Record<string, string>
    try {
      query = parseClinicInfoQuery(queryValue)
      provenance = originFields(origin, 'Clinic information')
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      await this.assertNoUnresolvedAction()
      const request = {
        type: 'clinic_info',
        ...provenance,
        ...(query.specialty ? { specialty: query.specialty } : {}),
        ...(query.doctor ? { doctor: query.doctor } : {}),
        topic: query.topic
      }
      const reply = await this.call('POST', '/tasks', { request }, TIMEOUTS.write)
      const task = parseTask(reply.body)
      await this.store.write(task.taskId)
      return await this.snapshot(task.taskId, 0)
    })
  }

  /** Read public doctor profiles for the active clinic-info task. Read-only; safe to repeat. */
  async lookupClinicInfo(): Promise<AgentResult<AgentDoctorProfileView[]>> {
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/info/lookup`, undefined, TIMEOUTS.observe)
      return parseClinicInfo(reply.body, taskId).profiles
    })
  }

  async closeActiveTask(): Promise<AgentResult<null>> {
    return this.exclusive('task', async () => {
      await this.assertNoUnresolvedAction()
      // Closing an account-reading task releases its browser: the scope is
      // withdrawn (best effort) so a sign-in window can open this profile again.
      const activeId = await this.store.read()
      if (activeId) {
        const active = await this.snapshot(activeId, 0).catch(() => undefined)
        if (active?.task.kind === 'authenticated_read' && active.authenticated?.grant &&
            (active.authenticated.grant.status === 'ACTIVE' || active.authenticated.grant.status === 'PENDING')) {
          await this.call('POST', `/tasks/${activeId}/authenticated/revoke`, { reason: 'user_stopped' }, TIMEOUTS.write).catch(() => undefined)
        }
      }
      await this.store.clear()
      return null
    })
  }

  // ---- page inspection (Milestone 7a) ----------------------------------------

  private requireInspection(): PageInspectionSupport {
    if (!this.inspection || !this.inspection.policy.configured) {
      fail('inspection_unavailable', 'Page inspection is not set up on this computer. Nothing was opened.')
    }
    return this.inspection
  }

  private recipients(support: PageInspectionSupport): AgentDisclosureRecipient[] {
    const recipients = support.answerer?.recipients() ?? []
    if (recipients.length === 0) {
      fail('inspection_unavailable', 'No text model is configured to answer from a page, so Lumi will not open it.')
    }
    return recipients
  }

  private async prepareInspection(taskId: string, recipients: AgentDisclosureRecipient[]): Promise<void> {
    const reply = await this.call('POST', `/tasks/${taskId}/inspection/prepare`, {
      disclosure: { recipients, max_text_chars: INSPECTION_TEXT_CHARS }
    }, TIMEOUTS.write)
    const action = parseInspectionAction(reply.body)
    if (action.taskId !== taskId) throw new WireError('inspection.prepare')
  }

  /**
   * Create a page-inspection task and show its exact approval card. Nothing is
   * opened: the page is read only after the trusted Approve click.
   *
   * The URL is canonicalized and checked here, then again by the runtime, and
   * again by the worker. The same typed request id is never turned into a
   * second task, including after a main restart.
   */
  async createPageInspection(urlValue: unknown, questionValue: unknown, origin?: TaskOrigin): Promise<AgentResult<AgentTaskSnapshot>> {
    let url: string
    let question: string
    let provenance: Record<string, string>
    let support: PageInspectionSupport
    let recipients: AgentDisclosureRecipient[]
    try {
      support = this.requireInspection()
      if (typeof urlValue !== 'string') fail('invalid_request', 'Enter a web address to inspect.')
      try {
        url = support.policy.canonicalize(urlValue).url
      } catch (error) {
        if (error instanceof UrlPolicyError) fail('destination_not_allowed', describeRefusal(error.code))
        throw error
      }
      question = parseInspectionQuestion(questionValue)
      provenance = originFields(origin, `Inspect ${url}`)
      recipients = this.recipients(support)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      if (origin) {
        const activeId = await this.store.read()
        if (activeId) {
          const active = await this.snapshot(activeId, 0).catch(() => undefined)
          if (active && (active.task.requestId === origin.turnId || active.task.voiceTurnId === origin.turnId)) return active
        }
      }
      await this.assertNoUnresolvedAction()
      const request = { type: 'page_inspection', ...provenance, url, question }
      const reply = await this.call('POST', '/tasks', { request }, TIMEOUTS.write)
      const task = parseTask(reply.body)
      if (task.kind !== 'page_inspection' || task.inspection?.url !== url) throw new WireError('inspection.task')
      await this.store.write(task.taskId)
      await this.prepareInspection(task.taskId, recipients)
      return await this.snapshot(task.taskId, 0)
    })
  }

  /** A new card for the active inspection task. Returns the open one if it is still reviewable. */
  async inspectPageAgain(): Promise<AgentResult<AgentTaskSnapshot>> {
    let recipients: AgentDisclosureRecipient[]
    try {
      recipients = this.recipients(this.requireInspection())
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const current = await this.snapshot(taskId, 0)
      if (current.task.kind !== 'page_inspection') fail('invalid_request', 'That step does not apply to this kind of task.')
      await this.prepareInspection(taskId, recipients)
      return await this.snapshot(taskId, 0)
    })
  }

  private async ownedInspection(actionId: string): Promise<InspectionDetail> {
    const taskId = await this.activeTaskId()
    const { detail } = await this.loadInspection(actionId)
    if (detail.view.taskId !== taskId) fail('not_found', 'That inspection does not belong to the current task.')
    return detail
  }

  private async inspectionMutation(
    actionIdValue: unknown, revisionValue: unknown, route: 'approve' | 'reject'
  ): Promise<AgentResult<AgentInspectionView>> {
    let actionId: string
    let expectedRevision: number
    try {
      actionId = parseActionId(actionIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    const result = await this.exclusive(`action:${actionId}`, async () => {
      const current = await this.ownedInspection(actionId)
      if (current.view.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision', message: 'The inspection changed since you reviewed it.', currentRevision: current.view.revision
        })
      }
      const reply = await this.call('POST', `/actions/${actionId}/${route}`, { expected_revision: expectedRevision }, TIMEOUTS.write)
      const action = parseInspectionAction(reply.body)
      if (action.actionId !== actionId || action.taskId !== current.view.taskId) throw new WireError(`inspection.${route}`)
      return (await this.loadInspection(actionId)).detail.view
    })
    return result.ok ? result : { ok: false, error: inspectionError(result.error) }
  }

  approveInspection(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentInspectionView>> {
    return this.inspectionMutation(actionId, expectedRevision, 'approve')
  }

  rejectInspection(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentInspectionView>> {
    return this.inspectionMutation(actionId, expectedRevision, 'reject')
  }

  /**
   * Run the approved inspection once: the runtime claims the single-use
   * approval, the worker reads the page, and the observation is stored. Then,
   * and only from that stored observation, main asks a permitted model.
   * Never retried: an unconfirmed execution is reported so the UI re-reads.
   */
  async executeInspection(actionIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentInspectionView>> {
    let actionId: string
    let expectedRevision: number
    try {
      actionId = parseActionId(actionIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    const result = await this.exclusive(`action:${actionId}`, async () => {
      const current = await this.ownedInspection(actionId)
      if (current.view.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision', message: 'The inspection changed since you reviewed it.', currentRevision: current.view.revision
        })
      }
      const reply = await this.call('POST', `/actions/${actionId}/browser-execution`, { expected_revision: expectedRevision }, TIMEOUTS.execute)
      const action = parseInspectionAction(reply.body)
      if (action.actionId !== actionId || action.taskId !== current.view.taskId) throw new WireError('inspection.execute')
      const executed = await this.ownedInspection(actionId)
      if (executed.observation && !executed.view.answer) {
        try {
          return await this.answerFrom(executed)
        } catch (error) {
          // The observation is durable; the answer can be produced later from
          // it without opening the page again. Report the view as it stands.
          if (!(error instanceof AgentRequestError)) throw error
        }
      }
      return executed.view
    })
    return result.ok ? result : { ok: false, error: inspectionError(result.error) }
  }

  /** Answer from the stored observation. Never opens the page. */
  async answerInspection(actionIdValue: unknown): Promise<AgentResult<AgentInspectionView>> {
    let actionId: string
    try {
      actionId = parseActionId(actionIdValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    const result = await this.exclusive(`action:${actionId}`, async () => {
      const current = await this.ownedInspection(actionId)
      if (current.view.answer) return current.view
      if (!current.observation) fail('answer_unavailable', 'There is no saved page observation to answer from.')
      return await this.answerFrom(current)
    })
    return result.ok ? result : { ok: false, error: inspectionError(result.error) }
  }

  private async answerFrom(detail: InspectionDetail): Promise<AgentInspectionView> {
    const answerer = this.inspection?.answerer
    const observation = detail.observation
    if (!answerer || !observation) fail('answer_unavailable', 'No text model is configured to answer from the page.')
    const outcome = await answerer.answer({
      question: detail.view.proposal.question,
      observation,
      recipients: detail.view.proposal.recipients,
      taskId: detail.view.taskId
    })
    if (outcome.kind === 'unavailable') {
      fail('answer_unavailable', 'Lumi read the page, but no approved text model answered. Try again; the page will not be opened again.')
    }
    const reply = await this.call('POST', `/actions/${detail.view.actionId}/inspection/answer`, {
      observation_id: observation.observationId,
      content_hash: observation.contentHash,
      answer: outcome.answer,
      provider: outcome.provider,
      model: outcome.model
    }, TIMEOUTS.write)
    const recorded = parseInspection(reply.body)
    if (recorded.view.actionId !== detail.view.actionId || !recorded.view.answer) throw new WireError('inspection.answer')
    return recorded.view
  }


  // ---- public web research (Milestone 7b) ------------------------------------
  //
  // Main owns the planner, so main owns this loop. What it may not do is
  // decide whether a step is allowed: every iteration submits one step to the
  // runtime, which checks it against the scope the user confirmed and refuses
  // it otherwise. A refusal ends the loop or forces a re-observation; it is
  // never argued with, and the same step is never repeated blindly.

  private requireResearch(): ResearchSupport {
    if (!this.research || !this.research.policy.configured) {
      fail('research_unavailable', 'Public web research is not set up on this computer. Nothing was searched or opened.')
    }
    if (!this.research.planner || !this.research.answerer) {
      fail('research_unavailable', 'No text model is configured to plan or answer research, so Lumi will not start it.')
    }
    return this.research
  }

  private researchRecipients(support: ResearchSupport): AgentDisclosureRecipient[] {
    const recipients = support.answerer?.recipients() ?? []
    if (recipients.length === 0) {
      fail('research_unavailable', 'No text model is configured to answer from public pages, so Lumi will not open any.')
    }
    return recipients
  }

  private async loadResearch(taskId: string): Promise<{ detail: ResearchDetail; generation: string }> {
    const reply = await this.call('GET', `/tasks/${taskId}/research`, undefined, TIMEOUTS.read)
    const detail = parseResearch(reply.body)
    if (detail.view.taskId !== taskId) throw new WireError('research.task_id')
    return { detail, generation: reply.generation }
  }

  private async prepareResearch(taskId: string, recipients: AgentDisclosureRecipient[]): Promise<ResearchDetail> {
    const reply = await this.call('POST', `/tasks/${taskId}/research/prepare`, {
      disclosure: { recipients, max_text_chars: RESEARCH_TEXT_CHARS }
    }, TIMEOUTS.write)
    const detail = parseResearch(reply.body)
    if (detail.view.taskId !== taskId) throw new WireError('research.prepare')
    return detail
  }

  /**
   * Create a public-research task and show its bounded scope card. Nothing is
   * searched and nothing is opened: the scope authorises work only after the
   * trusted Allow click, which is a separate IPC channel.
   */
  async createResearchTask(objectiveValue: unknown, origin?: TaskOrigin): Promise<AgentResult<AgentTaskSnapshot>> {
    let objective: string
    let provenance: Record<string, string>
    let support: ResearchSupport
    let recipients: AgentDisclosureRecipient[]
    try {
      support = this.requireResearch()
      objective = parseResearchObjective(objectiveValue)
      provenance = originFields(origin, objective)
      recipients = this.researchRecipients(support)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      if (origin) {
        // The same typed request or voice turn never becomes a second task,
        // including after a main restart.
        const activeId = await this.store.read()
        if (activeId) {
          const active = await this.snapshot(activeId, 0).catch(() => undefined)
          if (active && (active.task.requestId === origin.turnId || active.task.voiceTurnId === origin.turnId)) return active
        }
      }
      await this.assertNoUnresolvedAction()
      const request = { type: 'public_research', ...provenance, objective }
      const reply = await this.call('POST', '/tasks', { request }, TIMEOUTS.write)
      const task = parseTask(reply.body)
      if (task.kind !== 'public_research' || task.research?.objective !== objective) {
        throw new WireError('research.task')
      }
      await this.store.write(task.taskId)
      await this.prepareResearch(task.taskId, recipients)
      return await this.snapshot(task.taskId, 0)
    })
  }

  /**
   * The trusted click. Confirms exactly the scope that was on screen, by id
   * and revision. There is no other route to an active scope: no voice
   * command, no typed sentence and no model output reaches this method.
   */
  grantResearchScope(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.researchScopeMutation(grantId, expectedRevision, 'grant')
  }

  /** Decline the card. The scope is withdrawn; nothing was searched or opened. */
  declineResearchScope(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.researchScopeMutation(grantId, expectedRevision, 'decline')
  }

  private async researchScopeMutation(
    grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'decline'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let grantId: string
    let expectedRevision: number
    try {
      grantId = parseActionId(grantIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('research', async () => {
      const taskId = await this.activeTaskId()
      const { detail } = await this.loadResearch(taskId)
      const grant = detail.view.grant
      if (!grant || grant.grantId !== grantId) {
        fail('not_found', 'That research permission does not belong to the current task.')
      }
      if (grant.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The research permission changed since you reviewed it. Review the current card.',
          currentRevision: grant.revision
        })
      }
      const body = kind === 'grant'
        ? { grant_id: grantId, expected_revision: expectedRevision }
        : { grant_id: grantId, expected_revision: expectedRevision, reason: 'user_declined' as const }
      const reply = await this.call('POST', `/tasks/${taskId}/research/${kind === 'grant' ? 'grant' : 'revoke'}`, body, TIMEOUTS.write)
      const updated = parseResearch(reply.body)
      if (updated.view.taskId !== taskId) throw new WireError(`research.${kind}`)
      return await this.snapshot(taskId, 0)
    })
  }

  /** Stop now: withdraw the scope, drop the browser session, keep the evidence. */
  async stopResearch(): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.exclusive('research', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/research/revoke`, { reason: 'user_stopped' }, TIMEOUTS.write)
      const updated = parseResearch(reply.body)
      if (updated.view.taskId !== taskId) throw new WireError('research.revoke')
      return await this.snapshot(taskId, 0)
    })
  }

  /**
   * Run the bounded research loop under the active scope.
   *
   * One planner call, one step, one observation, then plan again -- and every
   * exit is explicit: the goal, a budget, a refusal, or a planner that could
   * not answer within its contract. Nothing here retries a step, and an
   * unconfirmed step is reported rather than repeated: the browser may have
   * moved even though nothing consequential happened.
   */
  async runResearch(): Promise<AgentResult<AgentTaskSnapshot>> {
    let support: ResearchSupport
    let recipients: AgentDisclosureRecipient[]
    try {
      support = this.requireResearch()
      recipients = this.researchRecipients(support)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('research', async () => {
      const taskId = await this.activeTaskId()
      let { detail } = await this.loadResearch(taskId)
      if (detail.task.kind !== 'public_research') fail('invalid_request', 'That step does not apply to this kind of task.')
      if (detail.view.answer) return await this.snapshot(taskId, 0)
      const grant = detail.view.grant
      if (!grant || grant.status !== 'ACTIVE') {
        fail('research_not_granted', 'Allow public research on the card first. Nothing has been searched or opened.')
      }
      if (grant.expiresAt && Date.parse(grant.expiresAt) <= Date.now()) {
        fail('research_not_granted', 'That research permission has expired. Nothing was searched or opened.')
      }
      const objective = detail.view.objective
      const budgets = grant.scope.budgets
      const started = Date.now()
      let plannerCalls = detail.view.usage.plannerCalls
      let refusals = 0
      let forced: ResearchStepChoice | undefined

      for (let iteration = 0; ; iteration += 1) {
        const view = detail.view
        const overBudget = iteration >= budgets.maxSteps + 1 ||
          view.usage.steps >= budgets.maxSteps ||
          view.usage.observations >= budgets.maxObservations ||
          plannerCalls >= budgets.maxPlannerCalls ||
          (Date.now() - started) / 1_000 > budgets.maxActiveSeconds
        if (overBudget) {
          return await this.finishResearch(taskId, support, recipients, 'budget_exhausted', plannerCalls)
        }

        let step: ResearchStepChoice
        if (forced) {
          step = forced
          forced = undefined
        } else {
          plannerCalls += 1
          let decision: ResearchDecision
          try {
            decision = (await support.planner!.next({ objective, view, taskId, permits: (id) => recipients.includes(id as AgentDisclosureRecipient) })).decision
          } catch {
            // Every permitted model failed, or none could answer inside the
            // contract. Stop honestly rather than guessing a step.
            return await this.finishResearch(taskId, support, recipients, 'planner_failed', plannerCalls)
          }
          if (decision.kind === 'finish') {
            return await this.finishResearch(taskId, support, recipients, 'goal_reached', plannerCalls)
          }
          if (decision.kind === 'stop') {
            return await this.finishResearch(taskId, support, recipients, decision.stopReason, plannerCalls)
          }
          step = decision.step
        }

        let outcome: ResearchStepOutcome
        try {
          outcome = await this.submitResearchStep(taskId, step, plannerCalls)
        } catch (error) {
          const agentError = toAgentError(error)
          if (agentError.code === 'research_budget_exhausted') {
            return await this.finishResearch(taskId, support, recipients, 'budget_exhausted', plannerCalls)
          }
          if (agentError.code === 'research_refused' && refusals < RESEARCH_MAX_REFUSALS) {
            // A stale ref, a wrong tab, or a step the scope does not allow.
            // Re-observe authoritative browser state and let the planner
            // choose again from what is actually there.
            refusals += 1
            forced = { operation: 'observe', tab: 't1' }
            ;({ detail } = await this.loadResearch(taskId))
            continue
          }
          if (agentError.code === 'research_refused' || agentError.code === 'research_in_flight' ||
              agentError.code === 'research_unavailable' || agentError.code === 'research_not_granted') {
            return await this.finishResearch(taskId, support, recipients, 'blocked', plannerCalls)
          }
          throw error
        }
        detail = { view: outcome.view, task: outcome.task }
        if (outcome.outcome !== 'SUCCEEDED') {
          refusals += 1
          if (refusals > RESEARCH_MAX_REFUSALS) {
            return await this.finishResearch(taskId, support, recipients, 'blocked', plannerCalls)
          }
          if (outcome.outcome === 'OUTCOME_UNKNOWN') {
            // Lumi does not know what the browser did. The next step is to
            // look, not to repeat: an unresolved step also blocks the task
            // until it is recorded, so report and let the user retry.
            return await this.snapshot(taskId, 0)
          }
          forced = { operation: 'observe', tab: 't1' }
        }
      }
    })
  }

  private async submitResearchStep(
    taskId: string, step: ResearchStepChoice, plannerCalls: number
  ): Promise<ResearchStepOutcome> {
    const requestId = `req_${randomUUID().replaceAll('-', '')}`
    const reply = await this.call('POST', `/tasks/${taskId}/research/steps`, {
      request_id: requestId,
      step,
      planner_calls: plannerCalls
    }, TIMEOUTS.execute)
    const outcome = parseResearchStep(reply.body)
    if (outcome.view.taskId !== taskId) throw new WireError('research.step')
    return outcome
  }

  /**
   * Compose one grounded answer from the collected observations and record it.
   * The page is never opened again: this reads only what Lumi already stored.
   */
  private async finishResearch(
    taskId: string,
    support: ResearchSupport,
    recipients: AgentDisclosureRecipient[],
    stopReason: AgentResearchStopReason,
    plannerCalls: number
  ): Promise<AgentTaskSnapshot> {
    const { detail } = await this.loadResearch(taskId)
    if (detail.view.answer) return await this.snapshot(taskId, 0)
    const outcome = await support.answerer!.answer({
      objective: detail.view.objective,
      view: detail.view,
      recipients,
      stopReason,
      taskId
    })
    if (outcome.kind === 'unavailable') {
      // The evidence is durable. Nothing is recorded, so the answer can be
      // composed later without opening a single page again.
      fail('answer_unavailable', 'Lumi read the public pages, but no approved text model answered. Nothing was opened again.')
    }
    const reply = await this.call('POST', `/tasks/${taskId}/research/answer`, {
      answer: {
        status: outcome.answer.status,
        stop_reason: outcome.answer.stopReason,
        answer: outcome.answer.answer,
        evidence: outcome.answer.evidence
      },
      provider: outcome.provider,
      model: outcome.model,
      planner_calls: plannerCalls
    }, TIMEOUTS.write)
    const recorded = parseResearch(reply.body)
    if (recorded.view.taskId !== taskId || !recorded.view.answer) throw new WireError('research.answer')
    return await this.snapshot(taskId, 0)
  }


  // ---- authenticated account reading (Milestone 8a S3) -----------------------------
  //
  // Main owns the planner, so main owns this loop -- and, exactly as for public
  // research, it may not decide whether a step is allowed: every iteration
  // submits one step to the runtime, which checks it against the scope the user
  // confirmed. What is new here is what main may *not* choose at all:
  //
  //  * **The provider.** It is read from the confirmed grant, once, and passed to
  //    every model call. There is no code path that names another.
  //  * **Whether to continue after a pause.** A credential surface, a different
  //    account, an unknown account or a departure from the site each end the run
  //    in deterministic code. The planner is not consulted and no provider is
  //    called.

  private requireAuthenticated(): AuthenticatedSupport {
    if (!this.authenticated || !this.authenticated.planner || !this.authenticated.answerer) {
      fail('authenticated_unavailable', 'No AI provider is configured for reading your account, so Lumi will not start. Nothing was opened.')
    }
    return this.authenticated
  }

  /** The providers the trusted UI may offer: those that could both plan and answer. */
  private authenticatedRecipients(support: AuthenticatedSupport): AgentDisclosureRecipient[] {
    const answering = new Set(support.answerer?.recipients() ?? [])
    const recipients = (support.planner?.recipients() ?? []).filter((recipient) => answering.has(recipient))
    if (recipients.length === 0) {
      fail('authenticated_unavailable', 'No AI provider is configured for reading your account, so Lumi will not start. Nothing was opened.')
    }
    return recipients
  }

  async getAuthenticatedOptions(): Promise<AgentResult<AgentAuthenticatedOptions>> {
    try {
      return { ok: true, value: { recipients: this.authenticatedRecipients(this.requireAuthenticated()) } }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
  }

  private async loadAuthenticated(taskId: string): Promise<{ detail: AuthenticatedDetail; generation: string }> {
    const reply = await this.call('GET', `/tasks/${taskId}/authenticated`, undefined, TIMEOUTS.read)
    const detail = parseAuthenticated(reply.body)
    if (detail.view.taskId !== taskId) throw new WireError('authenticated.task_id')
    return { detail, generation: reply.generation }
  }

  /**
   * Create an authenticated-read task and show its trusted disclosure card.
   * Nothing is opened, navigated or read, and nothing leaves this computer: the
   * scope authorises work only after the trusted Allow click, a separate IPC
   * channel that no voice command, typed sentence or model reaches.
   *
   * `recipientId` must be one of the ids main itself offered. Anything else --
   * a name, a URL, a provider that is not configured -- is refused before the
   * runtime is contacted.
   */
  async createAuthenticatedTask(
    objectiveValue: unknown, profileIdValue: unknown, recipientIdValue: unknown, origin?: TaskOrigin
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let objective: string
    let profileId: string
    let recipient: AgentDisclosureRecipient
    let provenance: Record<string, string>
    try {
      const support = this.requireAuthenticated()
      const offered = this.authenticatedRecipients(support)
      objective = parseAuthenticatedObjective(objectiveValue)
      if (typeof profileIdValue !== 'string' || !UUID.test(profileIdValue)) fail('invalid_request', 'Choose one of your signed-in profiles.')
      profileId = profileIdValue
      if (typeof recipientIdValue !== 'string' || !(offered as readonly string[]).includes(recipientIdValue)) {
        fail('invalid_request', 'Choose one of the AI providers Lumi offered.')
      }
      recipient = recipientIdValue as AgentDisclosureRecipient
      provenance = originFields(origin, objective)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      if (origin) {
        const activeId = await this.store.read()
        if (activeId) {
          const active = await this.snapshot(activeId, 0).catch(() => undefined)
          if (active && (active.task.requestId === origin.turnId || active.task.voiceTurnId === origin.turnId)) return active
        }
      }
      await this.assertNoUnresolvedAction()
      const request = {
        type: 'authenticated_read',
        classification: 'account_private',
        ...provenance,
        objective,
        profile_id: profileId
      }
      const reply = await this.call('POST', '/tasks', { request }, TIMEOUTS.write)
      const task = parseTask(reply.body)
      if (task.kind !== 'authenticated_read' || task.authenticated?.objective !== objective) {
        throw new WireError('authenticated.task')
      }
      const prepared = await this.call('POST', `/tasks/${task.taskId}/authenticated/prepare`, { recipient }, TIMEOUTS.write)
      if (parseAuthenticated(prepared.body).view.taskId !== task.taskId) throw new WireError('authenticated.prepare')
      await this.store.write(task.taskId)
      return await this.snapshot(task.taskId, 0)
    })
  }

  /**
   * Milestone 12 S3: `OrchestrationCoordinator`'s own entry point for `account_read`. Exactly the same task
   * creation and trusted scope card `createAuthenticatedTask` already shows for a direct request -- the one
   * difference is that the disclosure recipient is chosen here, deterministically (the first provider main
   * would offer anyway, in the same order `getAuthenticatedOptions` returns), instead of by a renderer
   * selection, because dispatching a chosen capability happens with no renderer round trip in between. This
   * is not a new authority: the human still reviews and confirms the WHOLE scope card -- including this
   * recipient -- through the linked task's own existing trusted click before anything is opened.
   */
  async createAccountReadTask(objectiveValue: unknown, profileIdValue: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    let recipient: AgentDisclosureRecipient
    try {
      recipient = this.authenticatedRecipients(this.requireAuthenticated())[0]
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.createAuthenticatedTask(objectiveValue, profileIdValue, recipient)
  }

  /**
   * Milestone 10 S4. Make a cross-app workflow's `form` step the active task, exactly as
   * `createAuthenticatedTask` would have: the same offered recipients, the same account-reading card
   * (opened here, PENDING, confirmed only by the person), the same M8 planning and manifest approvals.
   * `taskId` comes from the runtime's own reply to the workflow controller, never from the renderer.
   */
  async activateWorkflowFormTask(taskIdValue: unknown, recipientIdValue: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    let taskId: string
    let recipient: AgentDisclosureRecipient
    try {
      const support = this.requireAuthenticated()
      const offered = this.authenticatedRecipients(support)
      if (typeof taskIdValue !== 'string' || !UUID.test(taskIdValue)) fail('invalid_request', 'That workflow step is invalid.')
      taskId = taskIdValue
      if (typeof recipientIdValue !== 'string' || !(offered as readonly string[]).includes(recipientIdValue)) {
        fail('invalid_request', 'Choose one of the AI providers Lumi offered.')
      }
      recipient = recipientIdValue as AgentDisclosureRecipient
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      await this.assertNoUnresolvedAction()
      const task = parseTask((await this.call('GET', `/tasks/${taskId}`, undefined, TIMEOUTS.read)).body)
      if (task.taskId !== taskId || task.kind !== 'authenticated_read') throw new WireError('workflow.form.task')
      const prepared = await this.call('POST', `/tasks/${taskId}/authenticated/prepare`, { recipient }, TIMEOUTS.write)
      if (parseAuthenticated(prepared.body).view.taskId !== taskId) throw new WireError('authenticated.prepare')
      await this.store.write(taskId)
      return await this.snapshot(taskId, 0)
    })
  }

  /**
   * The trusted click. Confirms exactly the scope that was on screen, by id and
   * revision. There is no other route to an active scope.
   */
  grantAuthenticatedScope(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.authenticatedScopeMutation(grantId, expectedRevision, 'grant')
  }

  /** Decline the card. Nothing was opened and nothing left this computer. */
  declineAuthenticatedScope(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.authenticatedScopeMutation(grantId, expectedRevision, 'decline')
  }

  private async authenticatedScopeMutation(
    grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'decline'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let grantId: string
    let expectedRevision: number
    try {
      grantId = parseActionId(grantIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const { detail } = await this.loadAuthenticated(taskId)
      const grant = detail.view.grant
      if (!grant || grant.grantId !== grantId) {
        fail('not_found', 'That account-reading permission does not belong to the current task.')
      }
      if (grant.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The account-reading permission changed since you reviewed it. Review the current card.',
          currentRevision: grant.revision
        })
      }
      const body = kind === 'grant'
        ? { grant_id: grantId, expected_revision: expectedRevision }
        : { grant_id: grantId, expected_revision: expectedRevision, reason: 'user_declined' as const }
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/${kind === 'grant' ? 'grant' : 'revoke'}`, body, TIMEOUTS.write)
      const updated = parseAuthenticated(reply.body)
      if (updated.view.taskId !== taskId) throw new WireError(`authenticated.${kind}`)
      return await this.snapshot(taskId, 0)
    })
  }

  // ---- form planning and exact disclosure approval (Milestone 8b S5) ----------------
  //
  // **None of this changes a website.** Main brokers four trusted steps and never
  // decides any of them:
  //
  //  * `prepareFormPlanning` -- ask the runtime to build the planning scope and show
  //    its card. Carries closed data-ref ids; sends nothing to any provider.
  //  * `grantFormPlanning` / `declineFormPlanning` -- the trusted click. The only way
  //    a provider may ever be shown a form's structure or a masked preview.
  //  * `runFormPlanning` -- ONE planner call to the grant's one recipient, then the
  //    proposal goes to the runtime, which validates it against its own persisted
  //    observation. The provider is read from the grant, never chosen here, and a
  //    failure stops the run: there is no second provider.
  //  * `approveFieldDisclosure` / `rejectFieldDisclosure` -- the trusted click on the
  //    exact manifest card: an action id and the revision shown. No manifest, value,
  //    origin, field or provider ever crosses this boundary from the renderer.
  //
  // Voice and typed sentences reach none of them: they are not on the voice backend.

  private async loadFormPlan(taskId: string): Promise<AgentFormPlanView> {
    const reply = await this.call('GET', `/tasks/${taskId}/authenticated/form`, undefined, TIMEOUTS.read)
    const plan = parseFormPlan(reply.body)
    if (plan.taskId !== taskId) throw new WireError('form_plan.task_id')
    return plan
  }

  /** Show the trusted FORM PLANNING card. Nothing is sent to any provider. */
  prepareFormPlanning(refsValue: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    let refs: AgentProtectedDataKind[]
    try {
      refs = parseProtectedDataRefs(refsValue)
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const { detail } = await this.loadAuthenticated(taskId)
      if (detail.task.kind !== 'authenticated_read') fail('invalid_request', 'That step does not apply to this kind of task.')
      const grant = detail.view.grant
      if (!grant || grant.status !== 'ACTIVE') {
        fail('authenticated_not_granted', 'Allow account reading first. Nothing has been opened or sent.')
      }
      const support = this.requireAuthenticated()
      if (!support.formPlanner || !support.formPlanner.recipients().includes(grant.scope.recipient)) {
        fail('model_unavailable', 'The AI provider you approved is not available for planning. Lumi stopped and sent nothing.')
      }
      const existing = await this.loadFormPlan(taskId)
      if (existing.formCount === 0) {
        // No form has been read yet: read the page once, under the scope the user already allowed.
        const outcome = await this.submitAuthenticatedStep(taskId, { operation: 'observe', tab: 't1' }, detail.view.usage.plannerCalls)
        if (outcome.pauseReason) return await this.snapshot(taskId, 0)
      }
      await this.call('POST', `/tasks/${taskId}/authenticated/form/prepare-scope`, { allowed_data_refs: refs }, TIMEOUTS.write)
      return await this.snapshot(taskId, 0)
    })
  }

  grantFormPlanning(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.formGrantMutation(grantId, expectedRevision, 'grant')
  }

  declineFormPlanning(grantId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.formGrantMutation(grantId, expectedRevision, 'decline')
  }

  private async formGrantMutation(
    grantIdValue: unknown, revisionValue: unknown, kind: 'grant' | 'decline'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let grantId: string
    let expectedRevision: number
    try {
      grantId = parseActionId(grantIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const plan = await this.loadFormPlan(taskId)
      const grant = plan.grant
      if (!grant || grant.grantId !== grantId) fail('not_found', 'That form-planning permission does not belong to the current task.')
      if (grant.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The form-planning permission changed since you reviewed it. Review the current card.',
          currentRevision: grant.revision
        })
      }
      const body = kind === 'grant'
        ? { grant_id: grantId, expected_revision: expectedRevision }
        : { grant_id: grantId, expected_revision: expectedRevision, reason: 'user_declined' }
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/form/${kind === 'grant' ? 'grant' : 'revoke'}`, body, TIMEOUTS.write)
      if (parseFormPlan(reply.body).taskId !== taskId) throw new WireError(`form_plan.${kind}`)
      return await this.snapshot(taskId, 0)
    })
  }

  /**
   * One planner call, one proposal, one validation by the runtime.
   *
   * The recipient is the form-planning grant's. If that provider is not configured
   * any more, fails, or answers outside the contract, this stops with
   * `model_unavailable`: the form structure and the masked previews are not sent
   * to anybody else, and nothing is retried.
   */
  async runFormPlanning(): Promise<AgentResult<AgentTaskSnapshot>> {
    let support: AuthenticatedSupport
    try {
      support = this.requireAuthenticated()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const plan = await this.loadFormPlan(taskId)
      const grant = plan.grant
      if (!grant || grant.status !== 'ACTIVE') {
        fail('authenticated_not_granted', 'Allow form planning on the card first. Nothing was sent to an AI.')
      }
      if (grant.expiresAt && Date.parse(grant.expiresAt) <= Date.now()) {
        fail('authenticated_not_granted', 'That form-planning permission has expired. Nothing was sent to an AI.')
      }
      const recipient = grant.planningRecipient
      if (!support.formPlanner || !support.formPlanner.recipients().includes(recipient)) {
        fail('model_unavailable', 'The AI provider you approved is not available for planning. Lumi stopped and sent nothing to another provider.')
      }
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/form/planning-context`, {}, TIMEOUTS.write)
      const context = parsePlanningContext(reply.body)
      // The runtime built this for the grant's recipient; refuse anything else.
      if (context.recipient !== recipient || context.grantId !== grant.grantId) throw new WireError('planning_context.recipient')
      let decision: FormPlanOutcome['decision']
      try {
        decision = (await support.formPlanner.plan({ context, taskId, recipient })).decision
      } catch {
        fail('model_unavailable', 'The AI provider you approved could not plan the form. Lumi stopped and did not send the form to another provider.')
      }
      if (decision.kind === 'stop') return await this.snapshot(taskId, 0)
      await this.call(
        'POST',
        `/tasks/${taskId}/authenticated/form/propose`,
        { proposal: decision.proposal, provider: recipient },
        TIMEOUTS.write
      )
      return await this.snapshot(taskId, 0)
    })
  }

  approveFieldDisclosure(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.disclosureDecision(actionId, expectedRevision, 'approve')
  }

  rejectFieldDisclosure(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.disclosureDecision(actionId, expectedRevision, 'reject')
  }

  private async disclosureDecision(
    actionIdValue: unknown, revisionValue: unknown, decision: 'approve' | 'reject'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let actionId: string
    let expectedRevision: number
    try {
      actionId = parseActionId(actionIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const card = (await this.loadFormPlan(taskId)).disclosure
      if (!card || card.actionId !== actionId) fail('not_found', 'That form plan does not belong to the current task.')
      if (card.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The form plan changed since you reviewed it. Review the current plan.',
          currentRevision: card.revision
        })
      }
      // An id and a revision. The manifest, the values, the origin and the fields
      // are all resolved by the runtime from what it stored.
      await this.call('POST', `/actions/${actionId}/field-disclosure/${decision}`, { expected_revision: expectedRevision }, TIMEOUTS.write)
      return await this.snapshot(taskId, 0)
    })
  }

  // ---- the network-frozen local draft (Milestone 8b S6) --------------------------------------
  //
  // Lumi fills the form in its own browser with the network frozen, verifies the values are in the
  // fields, and hands the browser to the user. It never submits. Main brokers six trusted clicks and
  // decides none of them; every one is an id and the revision that was on screen. No value, manifest,
  // field, origin, selector, URL, provider or freeze flag ever crosses this boundary, and none of
  // these is reachable from voice or from a typed sentence.
  //
  //  * `startFormPreparationMode` -- reopen the profile headed, return internally to the page, observe.
  //  * `approveFieldDisclosure`    -- (above) the click that FILLS, frozen, in that window.
  //  * `discardFormDraft`          -- destroy the dirty page while frozen, then thaw.
  //  * `prepareFormHandover`       -- open the SECOND exact approval; changes nothing.
  //  * `approveFormHandover`       -- the network is restored for the user. Lumi never submits.
  //  * `rejectFormHandover`, `stopFormPreparation` -- cancel; or discard and close.

  startFormPreparationMode(): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/form/preparation-mode`, {}, TIMEOUTS.prepare)
      if (parseFormPlan(reply.body).taskId !== taskId) throw new WireError('form_plan.preparation_mode')
      return await this.snapshot(taskId, 0)
    })
  }

  stopFormPreparation(): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/form/stop`, {}, TIMEOUTS.prepare)
      if (parseFormPlan(reply.body).taskId !== taskId) throw new WireError('form_plan.stop')
      return await this.snapshot(taskId, 0)
    })
  }

  discardFormDraft(draftId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.draftDecision(draftId, expectedRevision, 'discard')
  }

  prepareFormHandover(draftId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.draftDecision(draftId, expectedRevision, 'handover-request')
  }

  private async draftDecision(
    draftIdValue: unknown, revisionValue: unknown, decision: 'discard' | 'handover-request'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let draftId: string
    let expectedRevision: number
    try {
      draftId = parseActionId(draftIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const draft = (await this.loadFormPlan(taskId)).draft
      if (!draft || draft.draftId !== draftId) fail('not_found', 'That form draft does not belong to the current task.')
      if (draft.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The form draft changed since you reviewed it. Review the current card.',
          currentRevision: draft.revision
        })
      }
      const reply = await this.call('POST', `/form-drafts/${draftId}/${decision}`, { expected_revision: expectedRevision }, TIMEOUTS.prepare)
      if (parseFormPlan(reply.body).taskId !== taskId) throw new WireError(`form_plan.${decision}`)
      return await this.snapshot(taskId, 0)
    })
  }

  approveFormHandover(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.handoverDecision(actionId, expectedRevision, 'approve')
  }

  rejectFormHandover(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.handoverDecision(actionId, expectedRevision, 'reject')
  }

  private async handoverDecision(
    actionIdValue: unknown, revisionValue: unknown, decision: 'approve' | 'reject'
  ): Promise<AgentResult<AgentTaskSnapshot>> {
    let actionId: string
    let expectedRevision: number
    try {
      actionId = parseActionId(actionIdValue)
      expectedRevision = parseExpectedRevision(revisionValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const card = (await this.loadFormPlan(taskId)).handover
      if (!card || card.actionId !== actionId) fail('not_found', 'That handover does not belong to the current task.')
      if (card.revision !== expectedRevision) {
        throw new AgentRequestError({
          code: 'stale_revision',
          message: 'The handover changed since you reviewed it. Review the current card.',
          currentRevision: card.revision
        })
      }
      await this.call('POST', `/actions/${actionId}/form-handover/${decision}`, { expected_revision: expectedRevision }, TIMEOUTS.prepare)
      return await this.snapshot(taskId, 0)
    })
  }

  /** Stop now: withdraw the scope, release the browser, keep the (redacted) evidence. */
  async stopAuthenticated(): Promise<AgentResult<AgentTaskSnapshot>> {
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/authenticated/revoke`, { reason: 'user_stopped' }, TIMEOUTS.write)
      const updated = parseAuthenticated(reply.body)
      if (updated.view.taskId !== taskId) throw new WireError('authenticated.revoke')
      return await this.snapshot(taskId, 0)
    })
  }

  /**
   * Run the bounded account-reading loop under the active scope.
   *
   * One planner call, one step, one redacted observation, then plan again -- and
   * every exit is explicit: the goal, a budget, a refusal, a deterministic pause,
   * or an approved provider that could not answer. Nothing here retries a step,
   * and no exit sends account text to anybody but the grant's recipient.
   */
  async runAuthenticated(): Promise<AgentResult<AgentTaskSnapshot>> {
    let support: AuthenticatedSupport
    try {
      support = this.requireAuthenticated()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const taskId = await this.activeTaskId()
      const { detail } = await this.loadAuthenticated(taskId)
      if (detail.task.kind !== 'authenticated_read') fail('invalid_request', 'That step does not apply to this kind of task.')
      if (detail.view.answer) return await this.snapshot(taskId, 0)
      const recipient = this.readyAuthenticatedRecipient(detail, support)
      return this.runAuthenticatedFrom(taskId, support, recipient, detail, detail.view.usage.plannerCalls, undefined)
    })
  }

  /**
   * Milestone 12 S3: the orchestration Continue-time re-observe nudge for a linked `account_read` step.
   * Unlike `runAuthenticated()`, this targets an explicit task id (never "the active task", exactly like
   * `ProjectService.startProjectRun(taskId)` already does for `project_start`), and it is the one place
   * Lumi is allowed to attempt a step against a task that is *currently* `PAUSED` -- because attempting
   * exactly one fresh, forced `observe` and honestly reporting what it finds IS the re-observation the plan
   * requires. It never assumes the pause resolved just because this was called: the forced step goes through
   * `AuthenticatedReadService.execute_step`'s own unchanged checks (profile status, account fingerprint,
   * revoke epoch, credential-surface detection), and a pause that has not actually cleared is reported right
   * back. If the grant can no longer be used at all (a different account is now signed in, or it expired),
   * the dead grant is revoked so the linked task reaches a clean terminal state instead of being stuck
   * forever on a grant that will never work again -- never silently continuing, and never a new authority:
   * `revoke()` itself is `AuthenticatedReadService`'s own existing, unchanged method.
   */
  async continueAccountRead(taskIdValue: unknown): Promise<AgentResult<AgentTaskSnapshot>> {
    let support: AuthenticatedSupport
    let taskId: string
    try {
      support = this.requireAuthenticated()
      if (typeof taskIdValue !== 'string' || !UUID.test(taskIdValue)) fail('invalid_request', 'That account-reading task reference is invalid.')
      taskId = taskIdValue
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('authenticated', async () => {
      const { detail } = await this.loadAuthenticated(taskId)
      if (detail.task.kind !== 'authenticated_read') fail('invalid_request', 'That step does not apply to this kind of task.')
      if (detail.view.answer) return await this.snapshot(taskId, 0)
      const grant = detail.view.grant
      if (!grant || grant.status !== 'ACTIVE') return await this.snapshot(taskId, 0)
      const recipient = grant.scope.recipient
      if (!support.planner!.recipients().includes(recipient) || !support.answerer!.recipients().includes(recipient)) {
        return await this.snapshot(taskId, 0)
      }
      return this.runAuthenticatedFrom(
        taskId, support, recipient, detail, detail.view.usage.plannerCalls,
        { operation: 'observe', tab: 't1' }, 'grant_unusable'
      )
    })
  }

  /** Shared pre-flight for `runAuthenticated()`: the confirmed, unexpired grant's one pinned, still-configured
   * recipient. Throws (via `fail`) exactly as the inlined checks used to before this was extracted. */
  private readyAuthenticatedRecipient(detail: AuthenticatedDetail, support: AuthenticatedSupport): AgentDisclosureRecipient {
    const grant = detail.view.grant
    if (!grant || grant.status !== 'ACTIVE') {
      fail('authenticated_not_granted', 'Allow account reading on the card first. Nothing has been opened.')
    }
    if (grant.expiresAt && Date.parse(grant.expiresAt) <= Date.now()) {
      fail('authenticated_not_granted', 'That account-reading permission has expired. Nothing was opened.')
    }
    // The provider is the grant's and only the grant's. If it is no longer
    // configured the run stops here; it is never replaced by another.
    const recipient = grant.scope.recipient
    if (!support.planner!.recipients().includes(recipient) || !support.answerer!.recipients().includes(recipient)) {
      fail('model_unavailable', 'The AI provider you approved is not available. Lumi stopped and did not send your account pages to another provider.')
    }
    return recipient
  }

  /**
   * The bounded account-reading loop under an already-confirmed, still-usable scope: one planner call, one
   * step, one redacted observation, then plan again -- and every exit is explicit: the goal, a budget, a
   * refusal, a deterministic pause, or an approved provider that could not answer. Nothing here retries a
   * step, and no exit sends account text to anybody but the grant's recipient.
   *
   * `initialForced`, when given, is attempted even though the task may already be `PAUSED` -- the one
   * exception to "a pause always stops the loop immediately", used only by `continueAccountRead`'s single
   * re-observation. Every later iteration (and every ordinary `runAuthenticated()` call, which never passes
   * `initialForced`) respects an existing pause exactly as before.
   */
  private async runAuthenticatedFrom(
    taskId: string, support: AuthenticatedSupport, recipient: AgentDisclosureRecipient,
    initialDetail: AuthenticatedDetail, initialPlannerCalls: number,
    initialForced: AuthenticatedStepChoice | undefined,
    deadGrantRevokeReason?: 'grant_unusable'
  ): Promise<AgentTaskSnapshot> {
    let detail = initialDetail
    if (detail.view.answer) return await this.snapshot(taskId, 0)
    const grant = detail.view.grant
    if (!grant) return await this.snapshot(taskId, 0)
    const budgets = grant.scope.budgets
    const started = Date.now()
    let plannerCalls = initialPlannerCalls
    let refusals = 0
    let forced = initialForced

    for (let iteration = 0; ; iteration += 1) {
      const view = detail.view
      if (view.pauseReason && !(iteration === 0 && forced !== undefined)) return await this.snapshot(taskId, 0)
      const overBudget = iteration >= budgets.maxSteps + 1 ||
        view.usage.steps >= budgets.maxSteps ||
        view.usage.observations >= budgets.maxObservations ||
        plannerCalls >= budgets.maxPlannerCalls ||
        (Date.now() - started) / 1_000 > budgets.maxActiveSeconds
      if (overBudget) return await this.finishAuthenticated(taskId, support, recipient, 'budget_exhausted', plannerCalls)

      let step: AuthenticatedStepChoice
      if (forced) {
        step = forced
        forced = undefined
      } else {
        plannerCalls += 1
        let decision: AuthenticatedDecision
        try {
          decision = (await support.planner!.next({ objective: view.objective, view, taskId, recipient })).decision
        } catch {
          // The approved provider failed, or answered outside the contract.
          // Stop: the account pages are not sent anywhere else.
          fail('model_unavailable', 'The AI provider you approved could not plan the next step. Lumi stopped and did not send your account pages to another provider.')
        }
        if (decision.kind === 'finish') return await this.finishAuthenticated(taskId, support, recipient, 'goal_reached', plannerCalls)
        if (decision.kind === 'stop') return await this.finishAuthenticated(taskId, support, recipient, decision.stopReason, plannerCalls)
        step = decision.step
      }

      let outcome: AuthenticatedStepOutcome
      try {
        outcome = await this.submitAuthenticatedStep(taskId, step, plannerCalls)
      } catch (error) {
        const agentError = toAgentError(error)
        if (agentError.code === 'authenticated_budget_exhausted') {
          return await this.finishAuthenticated(taskId, support, recipient, 'budget_exhausted', plannerCalls)
        }
        if (agentError.code === 'authenticated_refused' && refusals < AUTHENTICATED_MAX_REFUSALS) {
          refusals += 1
          forced = { operation: 'observe', tab: 't1' }
          ;({ detail } = await this.loadAuthenticated(taskId))
          continue
        }
        if (agentError.code === 'authenticated_refused' || agentError.code === 'authenticated_in_flight' ||
            agentError.code === 'authenticated_unavailable' || agentError.code === 'authenticated_not_granted') {
          // The permission stopped being usable (an account change bumps the
          // epoch), or the profile needs a person. Report the state; ask no model.
          if (agentError.code === 'authenticated_unavailable' || agentError.code === 'authenticated_not_granted') {
            // `authenticated_unavailable` covers `AuthenticatedProfileUnavailableError`'s whole family --
            // `profile_not_authenticated` (still needs login) and `profile_takeover_active` (a sign-in is
            // literally in progress right now) are the ORDINARY, expected shape of a premature Continue
            // press: the grant itself is fine and must stay usable for a later, real re-observe. Only
            // `authenticated_not_granted` (`AuthenticatedGrantNotUsableError`: the account changed, or the
            // grant expired) means the grant itself can never work again, so only that revokes it.
            if (deadGrantRevokeReason !== undefined && agentError.code === 'authenticated_not_granted') {
              // Best-effort: a fresh re-observe found the grant permanently unusable (a different account,
              // or expired). Revoking it turns "stuck forever" into a clean terminal state; if the revoke
              // itself fails, the caller still gets the current, honest snapshot below.
              await this.call(
                'POST', `/tasks/${taskId}/authenticated/revoke`, { reason: deadGrantRevokeReason }, TIMEOUTS.write
              ).catch(() => undefined)
            }
            return await this.snapshot(taskId, 0)
          }
          return await this.finishAuthenticated(taskId, support, recipient, 'blocked', plannerCalls)
        }
        throw error
      }
      detail = { view: outcome.view, task: outcome.task }
      // A deterministic pause: no planner, no provider, no answer.
      if (outcome.pauseReason) return await this.snapshot(taskId, 0)
      if (outcome.outcome !== 'SUCCEEDED') {
        refusals += 1
        if (refusals > AUTHENTICATED_MAX_REFUSALS) {
          return await this.finishAuthenticated(taskId, support, recipient, 'blocked', plannerCalls)
        }
        // A lost read may have reached the site. Look; do not repeat.
        forced = { operation: 'observe', tab: 't1' }
      }
    }
  }

  private async submitAuthenticatedStep(
    taskId: string, step: AuthenticatedStepChoice, plannerCalls: number
  ): Promise<AuthenticatedStepOutcome> {
    const requestId = `req_${randomUUID().replaceAll('-', '')}`
    const reply = await this.call('POST', `/tasks/${taskId}/authenticated/steps`, {
      request_id: requestId,
      step,
      planner_calls: plannerCalls
    }, TIMEOUTS.execute)
    const outcome = parseAuthenticatedStep(reply.body)
    if (outcome.view.taskId !== taskId) throw new WireError('authenticated.step')
    return outcome
  }

  /**
   * Compose one grounded answer from the collected redacted observations and
   * record it. The account is never opened again, and the answer goes to the
   * grant's recipient only.
   */
  private async finishAuthenticated(
    taskId: string,
    support: AuthenticatedSupport,
    recipient: AgentDisclosureRecipient,
    stopReason: AgentResearchStopReason,
    plannerCalls: number
  ): Promise<AgentTaskSnapshot> {
    const { detail } = await this.loadAuthenticated(taskId)
    if (detail.view.answer) return await this.snapshot(taskId, 0)
    const outcome = await support.answerer!.answer({
      objective: detail.view.objective,
      view: detail.view,
      recipient,
      stopReason,
      taskId
    })
    if (outcome.kind === 'unavailable') {
      // The evidence is durable. Nothing was sent to another provider and
      // nothing is recorded, so the answer can be composed later.
      fail('model_unavailable', 'Lumi read your account pages, but the AI provider you approved is not available. It did not send them to another provider.')
    }
    const reply = await this.call('POST', `/tasks/${taskId}/authenticated/answer`, {
      answer: {
        status: outcome.answer.status,
        stop_reason: outcome.answer.stopReason,
        answer: outcome.answer.answer,
        evidence: outcome.answer.evidence
      },
      provider: outcome.provider,
      model: outcome.model,
      planner_calls: plannerCalls
    }, TIMEOUTS.write)
    const recorded = parseAuthenticated(reply.body)
    if (recorded.view.taskId !== taskId || !recorded.view.answer) throw new WireError('authenticated.answer')
    return await this.snapshot(taskId, 0)
  }

  // ---- read-only observation ------------------------------------------------

  async searchAppointments(): Promise<AgentResult<AgentSlotView[]>> {
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/booking/search`, undefined, TIMEOUTS.observe)
      return parseSearch(reply.body, taskId)
    })
  }

  async prepareBooking(slotIdValue: unknown): Promise<AgentResult<AgentActionView>> {
    let slotId: string
    try {
      slotId = parseSlotId(slotIdValue)
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    return this.exclusive('task', async () => {
      const taskId = await this.activeTaskId()
      const reply = await this.call('POST', `/tasks/${taskId}/booking/prepare`, { slot_id: slotId }, TIMEOUTS.observe)
      const action = parseAction(reply.body)
      if (action.taskId !== taskId || action.booking.slotId !== slotId) throw new WireError('prepare.action')
      return action
    })
  }

  // ---- consequential ledger steps ------------------------------------------

  requestApproval(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentActionView>> {
    return this.actionMutation('request', actionId, expectedRevision, 'approval-request', TIMEOUTS.write)
  }

  approveAction(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentActionView>> {
    return this.actionMutation('approve', actionId, expectedRevision, 'approve', TIMEOUTS.write)
  }

  rejectAction(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentActionView>> {
    return this.actionMutation('reject', actionId, expectedRevision, 'reject', TIMEOUTS.write)
  }

  executeAction(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentActionView>> {
    return this.actionMutation('execute', actionId, expectedRevision, 'browser-execution', TIMEOUTS.execute)
  }

  reconcileAction(actionId: unknown, expectedRevision: unknown): Promise<AgentResult<AgentActionView>> {
    return this.actionMutation('reconcile', actionId, expectedRevision, 'browser-reconciliation', TIMEOUTS.reconcile)
  }
}

export function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  // Never forward an arbitrary error message across the bridge.
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}


/** The closed set of saved-detail refs. Anything else -- a name, a value, a path -- is refused. */
export function parseProtectedDataRefs(value: unknown): AgentProtectedDataKind[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > PROTECTED_DATA_KINDS.length) {
    fail('invalid_request', 'Choose which saved details Lumi may plan with.')
  }
  const refs = value as unknown[]
  for (const ref of refs) {
    if (typeof ref !== 'string' || !(PROTECTED_DATA_KINDS as readonly string[]).includes(ref)) {
      fail('invalid_request', 'Lumi does not know that saved detail.')
    }
  }
  if (new Set(refs).size !== refs.length) fail('invalid_request', 'A saved detail can only be chosen once.')
  return refs as AgentProtectedDataKind[]
}
