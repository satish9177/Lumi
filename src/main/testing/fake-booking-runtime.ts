import { createHash, randomUUID } from 'node:crypto'
import { RuntimeRestartedError, type RuntimeMethod, type RuntimeReply } from '../services/agent-runtime-supervisor'
import type { RuntimeRequester } from '../services/agent-tasks'

/**
 * Test helper: an in-memory model of the runtime's task, booking and
 * clinic-information routes with the semantics the Python tests pin down.
 * Counters are the "external" evidence a test asserts on.
 */

type Json = Record<string, unknown>

export const GENERATION = '00000000-0000-4000-8000-0000000000aa'
const AT = '2026-09-16T10:00:00+00:00'
const FAR = '2099-01-01T00:00:00+00:00'
const OPEN = ['PROPOSED', 'WAITING_APPROVAL', 'APPROVED']
const UNRESOLVED = ['EXECUTING', 'OUTCOME_UNKNOWN', 'RECONCILING']

export interface Slot { slot_id: string; doctor: string; specialty: string; day: string; time: string; price: number; currency: string }

export const CATALOGUE: Slot[] = [
  { slot_id: 'slot-a-1830', doctor: 'Dr A', specialty: 'Dermatology', day: 'Saturday', time: '2026-09-19T18:30:00+05:30', price: 800, currency: 'INR' },
  { slot_id: 'slot-b-1915', doctor: 'Dr B', specialty: 'Dermatology', day: 'Saturday', time: '2026-09-19T19:15:00+05:30', price: 950, currency: 'INR' },
  { slot_id: 'slot-c-1000', doctor: 'Dr C', specialty: 'Dentistry', day: 'Saturday', time: '2026-09-19T10:00:00+05:30', price: 600, currency: 'INR' }
]

export const PROFILES = [
  {
    doctor_id: 'dr-a', doctor: 'Dr A', specialty: 'Dermatology', clinic: 'Lakeview Skin Clinic', address: '12 Lake Road, Hyderabad',
    hours: 'Mon-Sat 10:00-20:00', consultation_fee: 800, currency: 'INR', languages: ['English', 'Telugu', 'Hindi'], walk_ins: false
  },
  {
    doctor_id: 'dr-b', doctor: 'Dr B', specialty: 'Dermatology', clinic: 'Banjara Dermatology Centre', address: '4 Hill Street, Hyderabad',
    hours: 'Tue-Sun 11:00-21:00', consultation_fee: 950, currency: 'INR', languages: ['English', 'Hindi'], walk_ins: true
  }
]

interface TaskRow { id: string; status: string; revision: number; seq: number; request: Json; events: Json[] }
interface ActionRow { id: string; taskId: string; key: string; status: string; revision: number; proposal: Json; digest: string; approval: Json | null; attempts: Json[] }

/**
 * An in-memory model of the runtime's booking routes with the same
 * semantics the Python tests pin down (task lock serialization is implicit:
 * every handler runs synchronously). Counters are the "external" evidence.
 */
export class FakeBookingRuntime implements RuntimeRequester {
  tasks = new Map<string, TaskRow>()
  actions = new Map<string, ActionRow>()
  slots: Slot[] = structuredClone(CATALOGUE)
  executionOutcome: 'SUCCEEDED' | 'OUTCOME_UNKNOWN' = 'SUCCEEDED'
  failNextCreate = false
  profileFees: Record<string, number> = {}
  failNextLookup = false
  counts = { lookups: 0, creates: 0, searches: 0, prepares: 0, approvals: 0, executions: 0, submissions: 0, reconciliations: 0, rejects: 0, criteria: 0, cancels: 0 }
  paths: Array<{ method: RuntimeMethod; path: string; body: unknown }> = []

  async request(method: RuntimeMethod, path: string, body: unknown): Promise<RuntimeReply> {
    this.paths.push({ method, path, body })
    const reply = this.route(method, path, (body ?? {}) as Json)
    if (reply instanceof Error) throw reply
    return { status: reply[0], body: reply[1], generation: GENERATION }
  }

  // ---- json ---------------------------------------------------------------
  taskJson(task: TaskRow): Json {
    return { id: task.id, status: task.status, revision: task.revision, last_event_sequence: task.seq, request: task.request, created_at: AT, updated_at: AT }
  }

  actionJson(action: ActionRow): Json {
    return {
      id: action.id, task_id: action.taskId, idempotency_key: action.key, tool_name: 'commit_booking', risk_tier: 'R2',
      proposal: action.proposal, proposal_digest: action.digest, status: action.status, revision: action.revision,
      created_at: AT, updated_at: AT, approval: action.approval, attempts: action.attempts
    }
  }

  private event(task: TaskRow, type: string, payload: Json, status?: string): void {
    task.revision += 1
    task.seq += 1
    if (status && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(task.status)) task.status = status
    task.events.push({ id: task.seq, task_id: task.id, sequence: task.seq, task_revision: task.revision, event_type: type, payload, created_at: AT })
  }

  private actionEvent(task: TaskRow, action: ActionRow, type: string, extra: Json, status?: string): void {
    this.event(task, type, {
      action_id: action.id, tool_name: 'commit_booking', risk_tier: 'R2', proposal_digest: action.digest,
      action_status: action.status, action_revision: action.revision, ...extra
    }, status)
  }

  private move(action: ActionRow, status: string): void {
    action.status = status
    action.revision += 1
  }

  private criteria(request: Json): Json {
    const pick = (key: string) => (request[key] === undefined ? null : request[key])
    return {
      specialty: request.specialty ?? '', day: request.day ?? '', earliest_time: pick('earliest_time'),
      latest_time: pick('latest_time'), max_price: pick('max_price'), max_price_currency: pick('max_price_currency'),
      date_from: pick('date_from'), date_to: pick('date_to')
    }
  }

  private admits(request: Json, slot: Slot, price = slot.price): boolean {
    const clock = slot.time.slice(11, 16)
    const date = slot.time.slice(0, 10)
    if (typeof request.date_from === 'string' && typeof request.date_to === 'string' &&
        (date < request.date_from || date > request.date_to)) return false
    if (typeof request.earliest_time === 'string' && clock < request.earliest_time) return false
    if (typeof request.latest_time === 'string' && clock > request.latest_time) return false
    if (typeof request.max_price === 'number' && (price > request.max_price || slot.currency !== request.max_price_currency)) return false
    return true
  }

  private error(status: number, code: string, extra: Json = {}): [number, Json] {
    return [status, { error: { code, message: code, ...extra } }]
  }

  private bookings(taskId: string): ActionRow[] {
    return [...this.actions.values()].filter((action) => action.taskId === taskId)
  }

  private reject(task: TaskRow, action: ActionRow, reason: string): void {
    this.move(action, 'REJECTED')
    action.approval = null
    this.counts.rejects += 1
    this.actionEvent(task, action, 'action.rejected', { approval_id: null, reason }, 'READY')
  }

  // ---- routes -------------------------------------------------------------
  private route(method: RuntimeMethod, path: string, body: Json): [number, unknown] | Error {
    let match: RegExpExecArray | null
    if (method === 'POST' && path === '/tasks') {
      if (this.failNextCreate) {
        this.failNextCreate = false
        // The task is created, but the reply is lost.
        this.createTask(body)
        return new RuntimeRestartedError()
      }
      return [201, this.taskJson(this.createTask(body))]
    }
    if ((match = /^\/tasks\/([^/?]+)(.*)$/.exec(path))) {
      const task = this.tasks.get(match[1])
      if (!task) return this.error(404, 'task_not_found')
      const rest = match[2]
      if (method === 'GET' && rest === '') return [200, this.taskJson(task)]
      if (method === 'GET' && rest.startsWith('/events')) {
        const after = Number(/after_sequence=(\d+)/.exec(rest)![1])
        return [200, { task_id: task.id, events: task.events.filter((event) => (event.sequence as number) > after) }]
      }
      if (method === 'GET' && rest.startsWith('/actions')) {
        return [200, { task_id: task.id, actions: this.bookings(task.id).map((action) => this.actionJson(action)) }]
      }
      if (method === 'POST' && rest === '/info/lookup') return this.lookup(task)
      if (method === 'POST' && rest.startsWith('/booking/') && task.request.type !== 'appointment_booking') {
        return this.error(409, 'task_kind_mismatch')
      }
      if (method === 'POST' && rest === '/booking/search') return this.search(task)
      if (method === 'POST' && rest === '/booking/prepare') return this.prepare(task, String(body.slot_id))
      if (method === 'POST' && rest === '/booking/criteria') return this.revise(task, body)
      if (method === 'POST' && rest === '/booking/cancel') return this.cancel(task, body)
    }
    if ((match = /^\/actions\/([^/]+)(?:\/(.+))?$/.exec(path))) {
      const action = this.actions.get(match[1])
      if (!action) return this.error(404, 'action_not_found')
      const task = this.tasks.get(action.taskId)!
      if (method === 'GET') return [200, this.actionJson(action)]
      if (body.expected_revision !== undefined && body.expected_revision !== action.revision) {
        return this.error(409, 'stale_action_revision', { current_revision: action.revision })
      }
      switch (match[2]) {
        case 'approval-request':
          if (action.status !== 'PROPOSED') return this.error(409, 'invalid_action_transition')
          this.requestApproval(task, action)
          return [200, this.actionJson(action)]
        case 'approve':
          if (action.status !== 'WAITING_APPROVAL' || !action.approval) return this.error(409, 'invalid_action_transition')
          this.move(action, 'APPROVED')
          action.approval = { ...action.approval, status: 'APPROVED', action_revision: action.revision, approved_at: AT }
          this.counts.approvals += 1
          this.actionEvent(task, action, 'action.approved', { approval_id: action.approval.id }, 'READY')
          return [200, this.actionJson(action)]
        case 'reject':
          if (!OPEN.includes(action.status)) return this.error(409, 'invalid_action_transition')
          this.reject(task, action, 'user')
          return [200, this.actionJson(action)]
        case 'browser-execution': {
          if (action.status !== 'APPROVED' || action.approval?.status !== 'APPROVED') return this.error(409, 'approval_not_usable')
          if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(task.status)) return this.error(409, 'task_not_accepting_actions')
          this.counts.executions += 1
          const attemptId = randomUUID()
          this.move(action, 'EXECUTING')
          const approvalId = action.approval.id
          action.approval = null
          action.attempts.push({ id: attemptId, action_id: action.id, attempt_number: 1, approval_id: approvalId, runtime_generation: GENERATION, started_at: AT, finished_at: null, outcome: null, result: null, error_code: null })
          this.actionEvent(task, action, 'action.execution_started', { attempt_id: attemptId, attempt_number: 1, approval_id: approvalId }, 'EXECUTING')
          this.counts.submissions += 1
          const attempt = action.attempts[0]
          attempt.finished_at = AT
          attempt.outcome = this.executionOutcome
          if (this.executionOutcome === 'SUCCEEDED') {
            attempt.result = { status: 'OK', submitted: true, booking_id: 'BK-0001', receipt: { booking_id: 'BK-0001', doctor: action.proposal.doctor, price: action.proposal.price, currency: 'INR' } }
            this.move(action, 'SUCCEEDED')
            this.actionEvent(task, action, 'action.succeeded', { attempt_id: attemptId, attempt_number: 1, outcome: 'SUCCEEDED' }, 'READY')
          } else {
            attempt.error_code = 'runtime_restart'
            this.move(action, 'OUTCOME_UNKNOWN')
            this.actionEvent(task, action, 'action.outcome_unknown', { attempt_id: attemptId, attempt_number: 1, outcome: 'OUTCOME_UNKNOWN', reason: 'runtime_restart' }, 'OUTCOME_UNKNOWN')
          }
          return [200, this.actionJson(action)]
        }
        case 'browser-reconciliation':
          if (action.status !== 'OUTCOME_UNKNOWN') return this.error(409, 'invalid_action_transition')
          this.counts.reconciliations += 1
          this.move(action, 'RECONCILING')
          this.actionEvent(task, action, 'action.reconciliation_started', {}, 'RECONCILING')
          this.move(action, 'SUCCEEDED')
          this.actionEvent(task, action, 'action.reconciled', {
            result: 'SUCCEEDED', reason: 'reconciliation',
            evidence: { lookup: 'FOUND', booking_id: 'BK-0001', booking_count: 1 }
          }, 'READY')
          return [200, this.actionJson(action)]
      }
    }
    return this.error(422, 'invalid_request')
  }

  private createTask(body: Json): TaskRow {
    this.counts.creates += 1
    const task: TaskRow = { id: randomUUID(), status: 'CREATED', revision: 1, seq: 1, request: body.request as Json, events: [] }
    task.events.push({ id: 1, task_id: task.id, sequence: 1, task_revision: 1, event_type: 'task.created', payload: { status: 'CREATED' }, created_at: AT })
    this.tasks.set(task.id, task)
    return task
  }

  private search(task: TaskRow): [number, unknown] {
    this.counts.searches += 1
    const { request } = task
    const observed = this.slots.filter((slot) =>
      (!request.specialty || slot.specialty === request.specialty) && (!request.day || slot.day === request.day))
    const admitted = observed.filter((slot) => this.admits(request, slot))
      .map(({ day: _day, ...slot }) => slot)
    this.event(task, 'task.search_completed', {
      criteria: this.criteria(request), slots: admitted, observed_count: observed.length, excluded_count: observed.length - admitted.length
    })
    return [200, { task_id: task.id, slots: admitted }]
  }

  private requestApproval(task: TaskRow, action: ActionRow): void {
    this.move(action, 'WAITING_APPROVAL')
    action.approval = {
      id: randomUUID(), action_id: action.id, action_revision: action.revision, proposal_digest: action.digest,
      status: 'PENDING', created_at: AT, expires_at: FAR, approved_at: null, rejected_at: null, consumed_at: null
    }
    this.actionEvent(task, action, 'action.approval_requested', { approval_ttl_seconds: 300 }, 'WAITING_APPROVAL')
  }

  private prepare(task: TaskRow, slotId: string): [number, unknown] {
    this.counts.prepares += 1
    const existing = this.bookings(task.id)
    if (existing.some((action) => !['FAILED', 'REJECTED'].includes(action.status))) return this.error(409, 'action_already_open')
    const slot = this.slots.find((candidate) => candidate.slot_id === slotId)
    if (!slot) return this.error(409, 'booking_slot_unavailable')
    if (!this.admits(task.request, slot)) return this.error(409, 'booking_criteria_mismatch')
    const proposal = { site: 'appointment_fixture', slot_id: slot.slot_id, doctor: slot.doctor, time: slot.time, price: slot.price, currency: slot.currency }
    const action: ActionRow = {
      id: randomUUID(), taskId: task.id, key: `commit_booking-${existing.length + 1}`, status: 'PROPOSED', revision: 1,
      proposal, digest: createHash('sha256').update(JSON.stringify(proposal)).digest('hex'), approval: null, attempts: []
    }
    this.actions.set(action.id, action)
    this.actionEvent(task, action, 'action.proposed', { idempotency_key: action.key, requires_approval: true })
    this.requestApproval(task, action)
    return [201, this.actionJson(action)]
  }

  private guard(task: TaskRow): [number, unknown] | undefined {
    const bookings = this.bookings(task.id)
    if (bookings.some((action) => UNRESOLVED.includes(action.status))) return this.error(409, 'task_has_unresolved_action')
    if (bookings.some((action) => action.status === 'SUCCEEDED')) return this.error(409, 'task_already_booked')
    return undefined
  }

  private revise(task: TaskRow, body: Json): [number, unknown] {
    this.counts.criteria += 1
    if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(task.status)) return this.error(409, 'task_not_accepting_actions')
    if (body.expected_revision !== task.revision) return this.error(409, 'stale_revision', { current_revision: task.revision })
    const refused = this.guard(task)
    if (refused) return refused
    const fields = body.criteria as Json
    const request: Json = {
      type: task.request.type, text: task.request.text, source: task.request.source,
      voice_turn_id: task.request.voice_turn_id, request_id: task.request.request_id
    }
    for (const [key, value] of Object.entries(fields)) if (value !== '' && value !== null && value !== undefined) request[key] = value
    for (const key of Object.keys(request)) if (request[key] === undefined) delete request[key]
    const specialtyChanged = (task.request.specialty ?? '') !== (request.specialty ?? '')
    const invalidated = this.bookings(task.id).filter((action) => OPEN.includes(action.status) &&
      (specialtyChanged || !this.admits(request, { ...CATALOGUE[0], time: String(action.proposal.time), price: Number(action.proposal.price), currency: String(action.proposal.currency) })))
    task.request = request
    this.event(task, 'task.criteria_updated', { criteria: this.criteria(request), invalidated_action_ids: invalidated.map((action) => action.id), reason: 'criteria_changed' })
    for (const action of invalidated) this.reject(task, action, 'criteria_changed')
    return [200, { task: this.taskJson(task), invalidated_action_ids: invalidated.map((action) => action.id) }]
  }

  private lookup(task: TaskRow): [number, unknown] | Error {
    this.counts.lookups += 1
    if (task.request.type !== 'clinic_info') return this.error(409, 'task_kind_mismatch')
    if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(task.status)) return this.error(409, 'task_not_accepting_actions')
    if (this.failNextLookup) {
      this.failNextLookup = false
      return this.error(503, 'browser_observation_failed')
    }
    const { request } = task
    const profiles = PROFILES.filter((profile) =>
      (!request.specialty || profile.specialty === request.specialty) && (!request.doctor || profile.doctor === request.doctor))
      .map((profile) => ({ ...profile, consultation_fee: this.profileFees[profile.doctor_id] ?? profile.consultation_fee }))
    const query = { specialty: request.specialty ?? '', doctor: request.doctor ?? '', topic: request.topic ?? 'overview' }
    this.event(task, 'task.info_lookup_completed', { query, profiles, observed_count: profiles.length })
    return [200, { task: this.taskJson(task), profiles }]
  }

  private cancel(task: TaskRow, body: Json): [number, unknown] {
    this.counts.cancels += 1
    if (body.expected_revision !== task.revision) return this.error(409, 'stale_revision', { current_revision: task.revision })
    const refused = this.guard(task)
    if (refused) return refused
    const open = this.bookings(task.id).filter((action) => OPEN.includes(action.status))
    for (const action of open) this.reject(task, action, 'task_cancelled')
    const from = task.status
    task.status = 'CANCELLED'
    this.event(task, 'task.cancelled', { from_status: from, to_status: 'CANCELLED', rejected_action_ids: open.map((action) => action.id) })
    return [200, { task: this.taskJson(task), rejected_action_ids: open.map((action) => action.id) }]
  }
}

