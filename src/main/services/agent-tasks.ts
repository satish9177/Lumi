import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  BOOKING_DAYS,
  UNRESOLVED_ACTION_STATUSES,
  type AgentActionView,
  type AgentBookingCriteria,
  type AgentError,
  type AgentEventView,
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
import {
  WireError,
  isRecord,
  parseAction,
  parseActionList,
  parseCancellation,
  parseCriteriaRevision,
  parseEventPage,
  parseSearch,
  parseTask,
  projectRuntimeError,
  type CriteriaRevision,
  type TaskCancellation
} from './agent-wire'

/**
 * The trusted domain client for durable agent tasks, owned by Electron main.
 *
 * It exposes booking-task operations and nothing else: there is no generic
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
const CRITERIA_KEYS = new Set(['specialty', 'day', 'earliestTime', 'latestTime', 'maxPrice', 'maxPriceCurrency'])
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
  reconcile: 180_000
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
  return criteria
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
      : {})
  }
}

/**
 * Where a task came from. Only main constructs this (the voice controller);
 * the renderer's create-task IPC passes criteria and nothing else.
 */
export interface TaskOrigin {
  source: 'voice'
  turnId: string
  utterance: string
}

function originFields(origin: TaskOrigin | undefined): Record<string, string> {
  if (!origin) return { text: 'Book a clinic appointment' }
  if (!TURN_ID.test(origin.turnId)) fail('invalid_request', 'That voice turn is invalid.')
  const utterance = origin.utterance.trim().slice(0, MAX_UTTERANCE)
  return { text: utterance || 'Book a clinic appointment', source: origin.source, voice_turn_id: origin.turnId }
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

export class AgentTaskController {
  private readonly inFlight = new Set<string>()

  constructor(
    private readonly runtime: RuntimeRequester,
    private readonly store: ActiveTaskStore
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

  private async loadActions(taskId: string): Promise<{ actions: AgentActionView[]; generation: string }> {
    const reply = await this.call('GET', `/tasks/${taskId}/actions?limit=100`, undefined, TIMEOUTS.read)
    return { actions: parseActionList(reply.body, taskId), generation: reply.generation }
  }

  private async snapshot(taskId: string, afterSequence: number): Promise<AgentTaskSnapshot> {
    const taskReply = await this.call('GET', `/tasks/${taskId}`, undefined, TIMEOUTS.read)
    const task = parseTask(taskReply.body)
    if (task.taskId !== taskId) throw new WireError('task.id')
    const { actions, generation } = await this.loadActions(taskId)
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
    return { runtimeGeneration: generation, task, actions, events }
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

  async closeActiveTask(): Promise<AgentResult<null>> {
    return this.exclusive('task', async () => {
      await this.assertNoUnresolvedAction()
      await this.store.clear()
      return null
    })
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
