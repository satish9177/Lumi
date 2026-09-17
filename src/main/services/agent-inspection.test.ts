import { createHash, randomUUID } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { PageAnswerer } from '../agent/page-answer'
import { PublicUrlPolicy } from '../agent/public-url-policy'
import { TaskRequestInterpreter, extractInspectionRequest } from '../agent/task-request-interpreter'
import { DEFAULT_ROUTES, ModelRouter, type RoutingTable } from '../models/model-router'
import { ScriptedTextProvider, type ScriptedBehaviour } from '../models/scripted-provider'
import { RuntimeRestartedError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import { ActiveTaskStore, AgentTaskController, type PageInspectionSupport, type RuntimeRequester } from './agent-tasks'
import { VoiceTaskController } from './voice-task-controller'

type Json = Record<string, unknown>
const GENERATION = '00000000-0000-4000-8000-0000000000aa'
const WORKER = '00000000-0000-4000-8000-0000000000bb'
const ORIGIN = 'http://127.0.0.1:8811'
const AT = '2026-09-17T10:00:00+00:00'
const FAR = '2099-01-01T00:00:00+00:00'
const RATED_BLOCKS = ['lumi_fixture_coder', 'Contest rating', '1,842', 'Global rank', '12,345', 'Problems solved', '367']
const OPEN = ['PROPOSED', 'WAITING_APPROVAL', 'APPROVED', 'EXECUTING', 'RECONCILING']

const digest = (value: unknown): string => createHash('sha256').update(JSON.stringify(value)).digest('hex')

interface TaskRow { id: string; status: string; revision: number; request: Json; events: Json[] }
interface ActionRow {
  id: string; taskId: string; status: string; revision: number; proposal: Json; digest: string
  approval: Json | null; attempts: Json[]; observation: Json | null; answer: Json | null
}

/** The page-inspection routes with the runtime's semantics, in memory. */
class FakeInspectionRuntime implements RuntimeRequester {
  tasks = new Map<string, TaskRow>()
  actions = new Map<string, ActionRow>()
  calls: Array<{ method: RuntimeMethod; path: string; body: unknown }> = []
  pageReads = 0
  pageBlocks = RATED_BLOCKS
  executionOutcome: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN' = 'SUCCEEDED'
  loseNextExecutionResponse = false

  async request(method: RuntimeMethod, path: string, body: unknown): Promise<RuntimeReply> {
    this.calls.push({ method, path, body })
    const reply = this.handle(method, path, (body ?? {}) as Json)
    if (reply instanceof Error) throw reply
    return { status: reply[0], body: reply[1], generation: GENERATION }
  }

  count(method: RuntimeMethod, pattern: RegExp): number {
    return this.calls.filter((call) => call.method === method && pattern.test(call.path)).length
  }

  private task(row: TaskRow): Json {
    return { id: row.id, status: row.status, revision: row.revision, last_event_sequence: row.events.length, request: row.request, created_at: AT, updated_at: AT }
  }

  private event(row: TaskRow, type: string, payload: Json = {}): void {
    row.revision += 1
    row.events.push({ id: row.events.length + 1, task_id: row.id, sequence: row.events.length + 1, task_revision: row.revision, event_type: type, payload, created_at: AT })
  }

  private action(row: ActionRow): Json {
    return {
      id: row.id, task_id: row.taskId, idempotency_key: 'inspect_public_page-1', tool_name: 'inspect_public_page', risk_tier: 'R1',
      proposal: row.proposal, proposal_digest: row.digest, status: row.status, revision: row.revision,
      created_at: AT, updated_at: AT, approval: row.approval, attempts: row.attempts
    }
  }

  private inspection(row: ActionRow): Json {
    return { action: this.action(row), observation: row.observation, answer: row.answer }
  }

  private move(row: ActionRow, status: string, type: string): void {
    row.status = status
    row.revision += 1
    const task = this.tasks.get(row.taskId)!
    this.event(task, type, { action_id: row.id, action_status: status, action_revision: row.revision })
  }

  private handle(method: RuntimeMethod, path: string, body: Json): [number, unknown] | Error {
    const error = (status: number, code: string, extra: Json = {}): [number, unknown] => [status, { error: { code, message: code, ...extra } }]
    let match: RegExpExecArray | null
    if (method === 'POST' && path === '/tasks') {
      const request = body.request as Json
      const row: TaskRow = { id: randomUUID(), status: 'CREATED', revision: 1, request, events: [] }
      row.events.push({ id: 1, task_id: row.id, sequence: 1, task_revision: 1, event_type: 'task.created', payload: { status: 'CREATED' }, created_at: AT })
      this.tasks.set(row.id, row)
      return [201, this.task(row)]
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})$/.exec(path))) {
      const row = this.tasks.get(match[1])
      return row ? [200, this.task(row)] : error(404, 'task_not_found')
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/actions\?limit=100$/.exec(path))) {
      return [200, { task_id: match[1], actions: [...this.actions.values()].filter((row) => row.taskId === match![1]).map((row) => this.action(row)) }]
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/events\?after_sequence=(\d+)&limit=\d+$/.exec(path))) {
      const row = this.tasks.get(match[1])!
      return [200, { task_id: row.id, events: row.events.filter((event) => (event.sequence as number) > Number(match![2])) }]
    }
    if (method === 'POST' && (match = /^\/tasks\/([0-9a-f-]{36})\/inspection\/prepare$/.exec(path))) {
      const task = this.tasks.get(match[1])!
      const disclosure = body.disclosure as Json
      const url = task.request.url as string
      const proposal = {
        schema_version: 1, operation: 'inspect_public_page', effect: 'public_read', url, host: new URL(url).hostname,
        question: task.request.question, policy_version: 'public-url-v1',
        limits: { max_blocks: 200, max_text_chars: 12_000, max_links: 20, max_redirects: 5 }, disclosure
      }
      const existing = [...this.actions.values()].filter((row) => row.taskId === task.id)
      const open = existing.find((row) => OPEN.includes(row.status))
      if (open) {
        return open.digest === digest(proposal) && open.status === 'WAITING_APPROVAL' ? [201, this.action(open)] : error(409, 'action_already_open')
      }
      const row: ActionRow = {
        id: randomUUID(), taskId: task.id, status: 'PROPOSED', revision: 1, proposal, digest: digest(proposal),
        approval: null, attempts: [], observation: null, answer: null
      }
      this.actions.set(row.id, row)
      this.event(task, 'action.proposed', { action_id: row.id })
      this.move(row, 'WAITING_APPROVAL', 'action.approval_requested')
      row.approval = { id: randomUUID(), action_id: row.id, action_revision: row.revision, proposal_digest: row.digest, status: 'PENDING', created_at: AT, expires_at: FAR, approved_at: null, rejected_at: null, consumed_at: null }
      task.status = 'WAITING_APPROVAL'
      return [201, this.action(row)]
    }
    if ((match = /^\/actions\/([0-9a-f-]{36})(?:\/(approve|reject|browser-execution|inspection|inspection\/answer))?$/.exec(path))) {
      const row = this.actions.get(match[1])
      if (!row) return error(404, 'action_not_found')
      const route = match[2]
      if (method === 'GET' && route === 'inspection') return [200, this.inspection(row)]
      if (method === 'POST' && (route === 'approve' || route === 'reject' || route === 'browser-execution')) {
        if (body.expected_revision !== row.revision) return error(409, 'stale_action_revision', { current_revision: row.revision })
      }
      if (method === 'POST' && route === 'approve') {
        if (row.status !== 'WAITING_APPROVAL') return error(409, 'invalid_action_transition')
        this.move(row, 'APPROVED', 'action.approved')
        row.approval = { ...(row.approval as Json), status: 'APPROVED', action_revision: row.revision, approved_at: AT }
        return [200, this.action(row)]
      }
      if (method === 'POST' && route === 'reject') {
        this.move(row, 'REJECTED', 'action.rejected')
        row.approval = null
        return [200, this.action(row)]
      }
      if (method === 'POST' && route === 'browser-execution') {
        if (row.status !== 'APPROVED') return error(409, 'invalid_action_transition')
        row.approval = null
        this.move(row, 'EXECUTING', 'action.execution_started')
        this.pageReads += 1
        const attempt: Json = { id: randomUUID(), action_id: row.id, attempt_number: 1, approval_id: randomUUID(), runtime_generation: GENERATION, started_at: AT, finished_at: AT, outcome: this.executionOutcome, result: {}, error_code: this.executionOutcome === 'FAILED' ? 'redirect_blocked' : null }
        row.attempts = [attempt]
        if (this.executionOutcome === 'SUCCEEDED') {
          const blocks = this.pageBlocks.map((text, index) => ({ id: `b${index + 1}`, text }))
          row.observation = {
            id: randomUUID(), task_id: row.taskId, action_id: row.id, attempt_id: attempt.id, dispatch_id: randomUUID(), worker_generation: WORKER,
            schema_version: 1, provenance: 'untrusted_environment', requested_url: row.proposal.url, final_url: row.proposal.url, redirects: [],
            title: 'Profile', document_epoch: 1, settled: true, truncated: false, observed_at: AT, content_hash: digest(blocks),
            blocks, links: [], total_text_chars: 40, total_link_count: 0
          }
        }
        this.move(row, this.executionOutcome, `action.${this.executionOutcome === 'OUTCOME_UNKNOWN' ? 'outcome_unknown' : this.executionOutcome.toLowerCase()}`)
        if (this.loseNextExecutionResponse) {
          this.loseNextExecutionResponse = false
          return new RuntimeRestartedError()
        }
        return [200, this.action(row)]
      }
      if (method === 'POST' && route === 'inspection/answer') {
        const observation = row.observation
        if (!observation || body.observation_id !== observation.id) return error(409, 'observation_not_available')
        if (body.content_hash !== observation.content_hash) return error(409, 'stale_observation')
        if (!row.answer) {
          const answer = body.answer as Json
          row.answer = { observation_id: observation.id, content_hash: observation.content_hash, ...answer, provider: body.provider, model: body.model, answered_at: AT }
          this.event(this.tasks.get(row.taskId)!, 'task.page_answer_recorded', { answer_status: answer.status })
        }
        return [200, this.inspection(row)]
      }
    }
    return error(422, 'invalid_request')
  }
}

function routerWith(behaviours: ScriptedBehaviour[]): ModelRouter {
  const providers = behaviours.map((behaviour, index) => new ScriptedTextProvider((['gemini', 'openai', 'deepseek'] as const)[index], behaviour))
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  table.page_answer = { ...table.page_answer, providers: providers.map((provider) => ({ provider: provider.id })) }
  return new ModelRouter((id) => providers.find((provider) => provider.id === id), table)
}

let directory: string
let runtime: FakeInspectionRuntime
let support: PageInspectionSupport

function controller(behaviours: ScriptedBehaviour[] = ['rules']): AgentTaskController {
  support = { policy: new PublicUrlPolicy({ allowedHosts: ['github.com'], testOrigins: [ORIGIN] }), answerer: new PageAnswerer(routerWith(behaviours)) }
  return new AgentTaskController(runtime, new ActiveTaskStore(directory), support)
}

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-inspection-'))
  runtime = new FakeInspectionRuntime()
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function unwrap<T>(result: { ok: true; value: T } | { ok: false; error: { code: string; message: string } }): T {
  if (!result.ok) throw new Error(`${result.error.code}: ${result.error.message}`)
  return result.value
}

describe('page inspection through Electron main', () => {
  it('prepares a card from a canonical URL, opens nothing, and binds the disclosure', async () => {
    const tasks = controller()
    const snapshot = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated#top`, '  What is my   contest rating? '))
    expect(snapshot.task.kind).toBe('page_inspection')
    expect(snapshot.task.inspection).toEqual({ url: `${ORIGIN}/profiles/rated`, host: '127.0.0.1', question: 'What is my contest rating?' })
    expect(snapshot.inspection).toMatchObject({
      status: 'WAITING_APPROVAL',
      proposal: { url: `${ORIGIN}/profiles/rated`, host: '127.0.0.1', question: 'What is my contest rating?', recipients: ['scripted'], maxTextChars: 12_000 },
      approval: { status: 'PENDING' }
    })
    expect(runtime.pageReads).toBe(0)
    expect(runtime.calls.find((call) => call.path.endsWith('/inspection/prepare'))?.body).toEqual({
      disclosure: { recipients: ['scripted'], max_text_chars: 12_000 }
    })
  })

  it.each([
    ['file:///C:/Windows/win.ini', 'Only https web pages can be inspected.'],
    ['https://localhost/admin', 'Local and private network addresses cannot be inspected.'],
    ['https://169.254.169.254/latest/meta-data/', 'Addresses that are raw IP numbers cannot be inspected.'],
    ['https://leetcode.com/u/someone/', 'That website is not on Lumi’s list of sites it may inspect.'],
    ['javascript:alert(1)', 'Only https web pages can be inspected.']
  ])('refuses %s before contacting the runtime', async (url, message) => {
    const result = await controller().createPageInspection(url, 'What is my rating?')
    expect(result).toEqual({ ok: false, error: { code: 'destination_not_allowed', message } })
    expect(runtime.calls).toEqual([])
  })

  it('offers nothing when unconfigured or when no model could answer', async () => {
    const unconfigured = new AgentTaskController(runtime, new ActiveTaskStore(directory), { policy: new PublicUrlPolicy() })
    expect((await unconfigured.createPageInspection('https://github.com/', 'What?')).ok).toBe(false)
    const noModel = new AgentTaskController(runtime, new ActiveTaskStore(directory), { policy: new PublicUrlPolicy({ allowedHosts: ['github.com'] }) })
    const result = await noModel.createPageInspection('https://github.com/', 'What?')
    expect(result.ok ? undefined : result.error.code).toBe('inspection_unavailable')
    expect(runtime.calls).toEqual([])
  })

  it('reads once only after the trusted approval, and answers from the stored observation', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    // Executing the card as shown (before approval) is refused by the ledger.
    expect((await tasks.executeInspection(card.actionId, card.revision)).ok).toBe(false)
    expect(runtime.pageReads).toBe(0)

    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    expect(approved.status).toBe('APPROVED')
    const done = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(runtime.pageReads).toBe(1)
    expect(done.status).toBe('SUCCEEDED')
    expect(done.answer).toMatchObject({
      status: 'answered', answer: 'Contest rating: 1,842.', provider: 'scripted',
      evidence: [{ block: 'b2', quote: 'Contest rating' }, { block: 'b3', quote: '1,842' }]
    })
    expect(done.observation).toMatchObject({ finalUrl: `${ORIGIN}/profiles/rated`, blockCount: 7 })
    // The renderer view carries metadata and quoted evidence, never the page's blocks.
    expect(JSON.stringify(done)).not.toContain('Global rank')

    const answerBody = runtime.calls.find((call) => call.path.endsWith('/inspection/answer'))!.body as Json
    expect(answerBody.content_hash).toBe((runtime.actions.get(card.actionId)!.observation as Json).content_hash)

    // The consumed approval cannot read the page again.
    expect((await tasks.executeInspection(card.actionId, done.revision)).ok).toBe(false)
    expect(runtime.pageReads).toBe(1)
  })

  it('a stale revision is refused in main before any runtime mutation', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const result = await tasks.approveInspection(card.actionId, card.revision - 1)
    expect(result).toMatchObject({ ok: false, error: { code: 'stale_revision', currentRevision: card.revision } })
    expect(runtime.count('POST', /\/approve$/)).toBe(0)
  })

  it('a duplicate typed request, even after a main restart, shows the same card instead of a second task', async () => {
    const origin = { source: 'text' as const, turnId: 'req_duplicate01', utterance: 'x' }
    const first = unwrap(await controller().createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?', origin))
    // A new controller over the same profile directory: main restarted.
    const again = unwrap(await controller().createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?', origin))
    expect(again.task.taskId).toBe(first.task.taskId)
    expect(again.inspection?.actionId).toBe(first.inspection?.actionId)
    expect(runtime.count('POST', /^\/tasks$/)).toBe(1)
  })

  it('a lost execution response is not retried; after restart the stored observation is answered without reopening the page', async () => {
    const tasks = controller(['timeout'])
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    runtime.loseNextExecutionResponse = true
    const lost = await tasks.executeInspection(card.actionId, approved.revision)
    expect(lost).toMatchObject({ ok: false, error: { code: 'runtime_restarted' } })
    expect(runtime.count('POST', /\/browser-execution$/)).toBe(1)

    // Main restarts with a working model. Durable state shows the read happened.
    const restarted = controller(['rules'])
    const snapshot = unwrap(await restarted.loadActiveTask(0))!
    expect(snapshot.inspection).toMatchObject({ status: 'SUCCEEDED' })
    expect(snapshot.inspection?.answer).toBeUndefined()
    const answered = unwrap(await restarted.answerInspection(card.actionId))
    expect(answered.answer?.status).toBe('answered')
    expect(runtime.pageReads).toBe(1)
    expect(runtime.count('POST', /\/browser-execution$/)).toBe(1)
  })

  it('when no model answers after the read, the page is not reopened and the answer can be produced later', async () => {
    const tasks = controller(['unavailable'])
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    const read = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(read.status).toBe('SUCCEEDED')
    expect(read.answer).toBeUndefined()
    const retry = await tasks.answerInspection(card.actionId)
    expect(retry).toMatchObject({ ok: false, error: { code: 'answer_unavailable' } })
    expect(runtime.pageReads).toBe(1)
  })

  it('a missing value is recorded as not found, never guessed', async () => {
    runtime.pageBlocks = ['lumi_fixture_coder', 'Global rank', '12,345', 'Problems solved', '367', 'No contest history yet.']
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/unrated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    const done = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(done.answer).toMatchObject({ status: 'not_found', answer: 'Could not verify this from the inspected page.', evidence: [] })
  })

  it('an unknown outcome stays unknown; a repeat is a new card needing a new approval', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    runtime.executionOutcome = 'OUTCOME_UNKNOWN'
    const unknown = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(unknown.status).toBe('OUTCOME_UNKNOWN')
    expect(runtime.count('POST', /\/inspection\/answer$/)).toBe(0)
    const again = unwrap(await tasks.inspectPageAgain()).inspection!
    expect(again.actionId).not.toBe(card.actionId)
    expect(again).toMatchObject({ status: 'WAITING_APPROVAL', approval: { status: 'PENDING' } })
    expect(runtime.actions.get(card.actionId)!.status).toBe('OUTCOME_UNKNOWN')
  })

  it('cannot act on an inspection of another task', async () => {
    const tasks = controller()
    const first = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    await tasks.closeActiveTask()
    unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/unrated`, 'rating?'))
    const result = await tasks.approveInspection(first.actionId, first.revision)
    expect(result).toMatchObject({ ok: false, error: { code: 'not_found' } })
    expect(runtime.count('POST', /\/approve$/)).toBe(0)
  })
})

describe('speech and text cannot approve an inspection', () => {
  it.each(['yes', 'approve', 'go ahead', 'approve it and open the page', 'yes do it'])('"%s" only points at the card', async (utterance) => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const voice = new VoiceTaskController(tasks)
    const interpreter = new TaskRequestInterpreter({
      controller: voice,
      inspections: tasks,
      loadTask: async () => unwrap(await tasks.loadActiveTask(0))
    })
    const outcome = unwrap(await interpreter.submit('req_approve0001', utterance))
    expect(outcome.focus).toBe('approval_card')
    expect(outcome.narration).toEqual({ kind: 'inspection', host: '127.0.0.1', state: 'approval_required' })
    expect(runtime.count('POST', /\/(approve|browser-execution)$/)).toBe(0)
    expect(runtime.actions.get(card.actionId)!.status).toBe('WAITING_APPROVAL')
  })

  it('a voice command that tries to approve is refused outright', async () => {
    const tasks = controller()
    unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?'))
    const voice = new VoiceTaskController(tasks)
    for (const command of [
      { kind: 'approve_inspection', turn: { turnId: 'item_1', utterance: 'yes' } },
      { kind: 'execute_action', turn: { turnId: 'item_2', utterance: 'yes' } },
      { kind: 'inspect_page', turn: { turnId: 'item_3', utterance: 'x' }, url: 'https://github.com/' }
    ]) {
      const result = await voice.handle(command, 'voice')
      expect(result.ok).toBe(false)
    }
    expect(runtime.count('POST', /\/(approve|browser-execution)$/)).toBe(0)
  })

  it('a typed request with one URL prepares a card and never opens the page', async () => {
    const tasks = controller()
    const interpreter = new TaskRequestInterpreter({
      controller: new VoiceTaskController(tasks),
      inspections: tasks,
      loadTask: async () => unwrap(await tasks.loadActiveTask(0))
    })
    const outcome = unwrap(await interpreter.submit('req_inspect0001', `Open ${ORIGIN}/profiles/rated and tell me my contest rating`))
    expect(outcome).toMatchObject({ focus: 'approval_card', narration: { kind: 'inspection', state: 'awaiting_approval' }, taskKind: 'page_inspection' })
    expect(runtime.pageReads).toBe(0)
    expect(extractInspectionRequest('compare https://github.com/a and https://github.com/b')).toBeUndefined()
    expect(extractInspectionRequest('what is on https://github.com/satish9177/Lumi?')).toEqual({ url: 'https://github.com/satish9177/Lumi', question: 'what is on' })
  })
})
