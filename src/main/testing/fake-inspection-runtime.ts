import { createHash, randomUUID } from 'node:crypto'
import { RuntimeRestartedError, type RuntimeMethod, type RuntimeReply } from '../services/agent-runtime-supervisor'
import type { RuntimeRequester } from '../services/agent-tasks'

/**
 * Test helper: the runtime's page-inspection routes, in memory, with the
 * semantics the Python tests pin down. `pageReads` counts every time the
 * isolated worker would have opened the page.
 */

type Json = Record<string, unknown>
export const GENERATION = '00000000-0000-4000-8000-0000000000aa'
const WORKER = '00000000-0000-4000-8000-0000000000bb'
export const ORIGIN = 'http://127.0.0.1:8811'
const AT = '2026-09-17T10:00:00+00:00'
const FAR = '2099-01-01T00:00:00+00:00'
export const RATED_BLOCKS = ['lumi_fixture_coder', 'Contest rating', '1,842', 'Global rank', '12,345', 'Problems solved', '367']
const OPEN = ['PROPOSED', 'WAITING_APPROVAL', 'APPROVED', 'EXECUTING', 'RECONCILING']

const digest = (value: unknown): string => createHash('sha256').update(JSON.stringify(value)).digest('hex')

interface TaskRow { id: string; status: string; revision: number; request: Json; events: Json[] }
interface ActionRow {
  id: string; taskId: string; status: string; revision: number; proposal: Json; digest: string
  approval: Json | null; attempts: Json[]; observation: Json | null; answer: Json | null
}

/** The page-inspection routes with the runtime's semantics, in memory. */
export class FakeInspectionRuntime implements RuntimeRequester {
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
