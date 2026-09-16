import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import contract from '../../shared/agent-runtime-contract.json'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import { ActiveTaskStore, AgentTaskController, type RuntimeRequester } from './agent-tasks'

type Json = Record<string, unknown>
const examples = contract.examples as unknown as Record<string, Json>
const TASK_ID = '00000000-0000-4000-8000-000000000001'
const ACTION_ID = '00000000-0000-4000-8000-000000000002'
const GENERATION = '00000000-0000-4000-8000-000000000005'
const OTHER_TASK = '00000000-0000-4000-8000-0000000000ff'

interface Call { method: RuntimeMethod; path: string; body: unknown }

type Handler = (call: Call) => RuntimeReply | Error

class FakeRuntime implements RuntimeRequester {
  calls: Call[] = []
  constructor(public handler: Handler) {}
  async request(method: RuntimeMethod, path: string, body: unknown, _timeoutMs?: number): Promise<RuntimeReply> {
    const call = { method, path, body }
    this.calls.push(call)
    const reply = this.handler(call)
    if (reply instanceof Error) throw reply
    return reply
  }
}

const ok = (body: unknown, status = 200, generation = GENERATION): RuntimeReply => ({ status, body, generation })

function action(status: string, revision: number, taskId = TASK_ID): Json {
  const value = structuredClone(examples.action_waiting_approval)
  return { ...value, status, revision, task_id: taskId, approval: status === 'WAITING_APPROVAL' ? { ...(value.approval as Json), action_revision: revision } : null }
}

function events(sequences: number[]): Json {
  const all = (examples.events as Json).events as Json[]
  return { task_id: TASK_ID, events: sequences.map((sequence) => ({ ...all[sequence - 1] })) }
}

function standardRuntime(actionStatus = 'WAITING_APPROVAL', revision = 2): FakeRuntime {
  return new FakeRuntime(({ method, path }) => {
    if (method === 'GET' && path === `/tasks/${TASK_ID}`) return ok({ ...examples.task, last_event_sequence: 3 })
    if (method === 'GET' && path === `/tasks/${TASK_ID}/actions?limit=100`) return ok({ task_id: TASK_ID, actions: [action(actionStatus, revision)] })
    if (method === 'GET' && path.startsWith(`/tasks/${TASK_ID}/events?after_sequence=`)) {
      const after = Number(/after_sequence=(\d+)/.exec(path)![1])
      return ok(events([1, 2, 3].filter((sequence) => sequence > after)))
    }
    if (method === 'GET' && path === `/actions/${ACTION_ID}`) return ok(action(actionStatus, revision))
    if (method === 'POST' && path === `/actions/${ACTION_ID}/approve`) return ok(action('APPROVED', revision + 1))
    if (method === 'POST' && path === `/actions/${ACTION_ID}/browser-execution`) return ok(action('SUCCEEDED', revision + 2))
    if (method === 'POST' && path === `/actions/${ACTION_ID}/browser-reconciliation`) return ok(action('SUCCEEDED', revision + 2))
    if (method === 'POST' && path === `/actions/${ACTION_ID}/reject`) return ok(action('REJECTED', revision + 1))
    if (method === 'POST' && path === '/tasks') return ok(examples.task, 201)
    return ok({ error: { code: 'invalid_request' } }, 422)
  })
}

let directory: string
let store: ActiveTaskStore

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-agent-'))
  store = new ActiveTaskStore(directory)
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

describe('AgentTaskController', () => {
  it('restores the persisted task with its ordered timeline', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime()
    const controller = new AgentTaskController(runtime, store)
    const result = await controller.loadActiveTask(0)
    expect(result.ok).toBe(true)
    if (!result.ok || !result.value) throw new Error('expected a snapshot')
    expect(result.value.task.taskId).toBe(TASK_ID)
    expect(result.value.events.map((event) => event.sequence)).toEqual([1, 2, 3])
    expect(result.value.actions[0].booking.price).toBe(800)
    expect(runtime.calls.every((call) => call.method === 'GET')).toBe(true)

    const later = await controller.loadActiveTask(2)
    expect(later.ok && later.value?.events.map((event) => event.sequence)).toEqual([3])
  })

  it('returns no task when none is active, without contacting the runtime', async () => {
    const runtime = standardRuntime()
    expect(await new AgentTaskController(runtime, store).loadActiveTask(0)).toEqual({ ok: true, value: null })
    expect(runtime.calls).toEqual([])
  })

  it('pages events until exhausted and rejects gaps', async () => {
    await store.write(TASK_ID)
    const all = (examples.events as Json).events as Json[]
    const many = Array.from({ length: 450 }, (_, index) => ({ ...all[0], id: index + 1, sequence: index + 1, task_revision: index + 1 }))
    const runtime = new FakeRuntime(({ path }) => {
      if (path === `/tasks/${TASK_ID}`) return ok({ ...examples.task, last_event_sequence: 450 })
      if (path.includes('/actions')) return ok({ task_id: TASK_ID, actions: [] })
      const after = Number(/after_sequence=(\d+)/.exec(path)![1])
      return ok({ task_id: TASK_ID, events: many.filter((event) => event.sequence > after).slice(0, 200) })
    })
    const result = await new AgentTaskController(runtime, store).loadActiveTask(0)
    expect(result.ok && result.value?.events.length).toBe(450)
    expect(runtime.calls.filter((call) => call.path.includes('/events')).map((call) => call.path)).toEqual([
      `/tasks/${TASK_ID}/events?after_sequence=0&limit=200`,
      `/tasks/${TASK_ID}/events?after_sequence=200&limit=200`,
      `/tasks/${TASK_ID}/events?after_sequence=400&limit=200`
    ])

    const gappy = new FakeRuntime(({ path }) => {
      if (path === `/tasks/${TASK_ID}`) return ok({ ...examples.task, last_event_sequence: 3 })
      if (path.includes('/actions')) return ok({ task_id: TASK_ID, actions: [] })
      return ok(events([1, 3]))
    })
    const broken = await new AgentTaskController(gappy, store).loadActiveTask(0)
    expect(broken).toMatchObject({ ok: false, error: { code: 'invalid_response' } })
  })

  it('reports a generation change during loading instead of mixing processes', async () => {
    await store.write(TASK_ID)
    const runtime = new FakeRuntime(({ path }) => {
      if (path === `/tasks/${TASK_ID}`) return ok(examples.task)
      if (path.includes('/actions')) return ok({ task_id: TASK_ID, actions: [] })
      return ok(events([1, 2, 3]), 200, '00000000-0000-4000-8000-0000000000aa')
    })
    expect(await new AgentTaskController(runtime, store).loadActiveTask(0)).toMatchObject({ ok: false, error: { code: 'runtime_restarted' } })
  })

  it('approves by action id and reviewed revision only', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime()
    const result = await new AgentTaskController(runtime, store).approveAction(ACTION_ID, 2)
    expect(result).toMatchObject({ ok: true, value: { status: 'APPROVED', revision: 3 } })
    const post = runtime.calls.find((call) => call.method === 'POST')
    expect(post).toEqual({ method: 'POST', path: `/actions/${ACTION_ID}/approve`, body: { expected_revision: 2 } })
  })

  it('refuses a stale revision before sending the mutation', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime('WAITING_APPROVAL', 3)
    const result = await new AgentTaskController(runtime, store).approveAction(ACTION_ID, 2)
    expect(result).toMatchObject({ ok: false, error: { code: 'stale_revision', currentRevision: 3 } })
    expect(runtime.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('refuses actions that do not belong to the active task', async () => {
    await store.write(TASK_ID)
    const runtime = new FakeRuntime(({ method }) => method === 'GET' ? ok(action('WAITING_APPROVAL', 2, OTHER_TASK)) : ok({}))
    const result = await new AgentTaskController(runtime, store).approveAction(ACTION_ID, 2)
    expect(result).toMatchObject({ ok: false, error: { code: 'not_found' } })
    expect(runtime.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('validates every renderer input before contacting the runtime', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime()
    const controller = new AgentTaskController(runtime, store)
    const bad: unknown[][] = [
      ['../tasks', 2], [`${ACTION_ID}/approve`, 2], [ACTION_ID, 0], [ACTION_ID, '2'], [ACTION_ID, 2.5],
      [{ id: ACTION_ID }, 2], [ACTION_ID, { expected_revision: 2, price: 1 }]
    ]
    for (const [actionId, revision] of bad) {
      expect(await controller.approveAction(actionId, revision)).toMatchObject({ ok: false, error: { code: 'invalid_request' } })
      expect(await controller.executeAction(actionId, revision)).toMatchObject({ ok: false, error: { code: 'invalid_request' } })
    }
    for (const slot of ['../../bookings', '', 'a'.repeat(65), 7, null]) {
      expect(await controller.prepareBooking(slot)).toMatchObject({ ok: false, error: { code: 'invalid_request' } })
    }
    for (const criteria of [null, { specialty: 'x', day: 'Someday' }, { specialty: '<script>', day: '' }, { specialty: '', day: '', price: 5 }]) {
      expect(await controller.createBookingTask(criteria)).toMatchObject({ ok: false, error: { code: 'invalid_request' } })
    }
    for (const after of [-1, 1.5, '0', Number.MAX_SAFE_INTEGER]) {
      expect(await controller.loadActiveTask(after)).toMatchObject({ ok: false, error: { code: 'invalid_request' } })
    }
    expect(runtime.calls).toEqual([])
  })

  it('reports an unconfirmed execution and never retries it', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime('APPROVED', 3)
    runtime.handler = ((base) => (call: Call) => call.method === 'POST' ? new RuntimeRestartedError() : base(call))(runtime.handler)
    const result = await new AgentTaskController(runtime, store).executeAction(ACTION_ID, 3)
    expect(result).toMatchObject({ ok: false, error: { code: 'runtime_restarted' } })
    expect(runtime.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('runs at most one mutation per action at a time', async () => {
    await store.write(TASK_ID)
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const base = standardRuntime('APPROVED', 3)
    const runtime: RuntimeRequester = {
      request: async (method, path, body, timeout) => {
        if (method === 'POST') await gate
        return base.request(method, path, body, timeout)
      }
    }
    const controller = new AgentTaskController(runtime, store)
    const first = controller.executeAction(ACTION_ID, 3)
    await new Promise((resolve) => setTimeout(resolve, 10))
    const second = await controller.executeAction(ACTION_ID, 3)
    const reconcile = await controller.reconcileAction(ACTION_ID, 3)
    expect(second).toMatchObject({ ok: false, error: { code: 'busy' } })
    expect(reconcile).toMatchObject({ ok: false, error: { code: 'busy' } })
    release()
    expect(await first).toMatchObject({ ok: true })
    expect(base.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('will not replace a task whose booking outcome is unresolved', async () => {
    await store.write(TASK_ID)
    for (const status of ['OUTCOME_UNKNOWN', 'RECONCILING', 'EXECUTING']) {
      const runtime = standardRuntime(status, 5)
      const controller = new AgentTaskController(runtime, store)
      expect(await controller.createBookingTask({ specialty: '', day: '' })).toMatchObject({ ok: false, error: { code: 'active_task_unresolved' } })
      expect(await controller.closeActiveTask()).toMatchObject({ ok: false, error: { code: 'active_task_unresolved' } })
      expect(runtime.calls.some((call) => call.method === 'POST')).toBe(false)
    }
    expect(await store.read()).toBe(TASK_ID)
  })

  it('creates a booking task from validated criteria and remembers it', async () => {
    const runtime = standardRuntime()
    const result = await new AgentTaskController(runtime, store).createBookingTask({ specialty: ' Dermatology ', day: 'Saturday' })
    expect(result.ok).toBe(true)
    expect(runtime.calls[0]).toEqual({
      method: 'POST', path: '/tasks',
      body: { request: { type: 'appointment_booking', text: 'Book a clinic appointment', specialty: 'Dermatology', day: 'Saturday' } }
    })
    expect(await store.read()).toBe(TASK_ID)
  })

  it('forgets a task the runtime says does not exist', async () => {
    await store.write(TASK_ID)
    const runtime = new FakeRuntime(() => ok({ error: { code: 'task_not_found', message: 'x' } }, 404))
    expect(await new AgentTaskController(runtime, store).loadActiveTask(0)).toEqual({ ok: true, value: null })
    expect(await store.read()).toBeUndefined()
  })

  it('reports an unavailable runtime without inventing state', async () => {
    await store.write(TASK_ID)
    const runtime = new FakeRuntime(() => new RuntimeUnavailableError())
    expect(await new AgentTaskController(runtime, store).loadActiveTask(0)).toMatchObject({ ok: false, error: { code: 'runtime_unavailable' } })
    expect(await store.read()).toBe(TASK_ID)
  })

  it('prepares by slot id only and checks the returned booking', async () => {
    await store.write(TASK_ID)
    const runtime = standardRuntime()
    runtime.handler = ((base) => (call: Call) => call.path.endsWith('/booking/prepare') ? ok(action('WAITING_APPROVAL', 2), 201) : base(call))(runtime.handler)
    const controller = new AgentTaskController(runtime, store)
    expect(await controller.prepareBooking('slot-a-1830')).toMatchObject({ ok: true })
    expect(runtime.calls[0]).toEqual({ method: 'POST', path: `/tasks/${TASK_ID}/booking/prepare`, body: { slot_id: 'slot-a-1830' } })
    // A reply for a different slot than requested is not accepted.
    expect(await controller.prepareBooking('slot-b-1915')).toMatchObject({ ok: false, error: { code: 'invalid_response' } })
  })
})

describe('ActiveTaskStore', () => {
  it('ignores corrupt or foreign content', async () => {
    const path = join(directory, 'agent-active-task.json')
    for (const content of ['not json', '{"version":2,"taskId":"x"}', '{"version":1,"taskId":"../../etc"}']) {
      await writeFile(path, content)
      expect(await store.read()).toBeUndefined()
    }
    await store.write(TASK_ID)
    expect(JSON.parse(await readFile(path, 'utf8'))).toEqual({ version: 1, taskId: TASK_ID })
    await store.clear()
    expect(await store.read()).toBeUndefined()
  })
})
