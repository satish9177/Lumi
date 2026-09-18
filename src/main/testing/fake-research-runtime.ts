import { createHash, randomUUID } from 'node:crypto'
import { RuntimeRestartedError, type RuntimeMethod, type RuntimeReply } from '../services/agent-runtime-supervisor'
import type { RuntimeRequester } from '../services/agent-tasks'

/**
 * Test helper: the runtime's public-research routes, in memory, with the
 * semantics the Python tests pin down.
 *
 * It refuses exactly what the real runtime refuses, because that is what these
 * tests are for: a step before the grant is confirmed, a step outside the
 * scope, a stale ref, a replayed request id, a budget that has run out. It
 * also counts what would have left the machine -- `searches` and `pageOpens`
 * -- so a test can assert that a refusal really stopped the work rather than
 * merely reporting it.
 */

type Json = Record<string, unknown>
export const GENERATION = '00000000-0000-4000-8000-0000000000aa'
const WORKER = '00000000-0000-4000-8000-0000000000bb'
const AT = '2026-09-18T10:00:00+00:00'
const FAR = '2099-01-01T00:00:00+00:00'

const digest = (value: unknown): string => createHash('sha256').update(JSON.stringify(value)).digest('hex')

/** The fixture "web": a hub page whose link leads to the page with the fact. */
export const HUB_BLOCKS = ['Projects named Lumi', 'This directory does not list project statistics.']
export const PROJECT_BLOCKS = [
  'lumi-desktop',
  'Purpose: A safe floating AI desktop companion for Windows',
  'Contributors 7'
]

interface TaskRow { id: string; status: string; revision: number; request: Json; events: Json[] }
interface GrantRow { id: string; taskId: string; status: string; revision: number; scope: Json; digest: string; confirmedAt: string | null }
interface ObservationRow { id: string; sequence: number; body: Json }

export interface FakeResearchOptions {
  /** Operations the prepared scope lists. Anything else is refused. */
  operations?: string[]
  maxSteps?: number
  searchConfigured?: boolean
}

export class FakeResearchRuntime implements RuntimeRequester {
  tasks = new Map<string, TaskRow>()
  grants = new Map<string, GrantRow>()
  observations: ObservationRow[] = []
  answers = new Map<string, Json>()
  requests = new Map<string, Json>()
  calls: Array<{ method: RuntimeMethod; path: string; body: unknown }> = []
  searches = 0
  pageOpens = 0
  session: string | null = null
  /** Set to make the next step's reply go missing after it was performed. */
  loseNextStepResponse = false
  /** Set to make the next step report an unknown outcome. */
  nextStepOutcome: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN' = 'SUCCEEDED'
  /** Set to make the next navigate refuse, as a stale ref would. */
  refuseNextNavigate: string | null = null
  /** Serve this page from the next navigate instead of the fixture hub/project. */
  nextPage?: { url: string; title: string; blocks: readonly string[] }

  constructor(private readonly options: FakeResearchOptions = {}) {}

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
    return {
      id: row.id, status: row.status, revision: row.revision, last_event_sequence: row.events.length,
      request: row.request, created_at: AT, updated_at: AT
    }
  }

  private event(row: TaskRow, type: string, payload: Json = {}): void {
    row.revision += 1
    row.events.push({
      id: row.events.length + 1, task_id: row.id, sequence: row.events.length + 1,
      task_revision: row.revision, event_type: type, payload, created_at: AT
    })
  }

  private scope(): Json {
    const operations = this.options.operations ?? ['public_search', 'navigate', 'observe', 'scroll', 'history', 'tab']
    return {
      schema_version: 1,
      kind: 'public_research',
      policy_version: 'public-research-v1',
      allowed_operations: operations,
      allowed: ['public_search', 'public_https_navigation', 'follow_public_links', 'read_page_text', 'task_owned_tabs'],
      forbidden: [
        'login', 'forms_and_typing', 'uploads_and_downloads', 'purchases_and_payments',
        'messages', 'files', 'private_network', 'non_get_requests'
      ],
      schemes: ['https'],
      methods: ['GET', 'HEAD'],
      hosts: 'any_public',
      budgets: {
        max_steps: this.options.maxSteps ?? 20,
        max_observations: 30,
        max_planner_calls: 20,
        max_tabs: 5,
        max_active_seconds: 300,
        max_model_input_tokens: 60_000,
        max_model_output_tokens: 8_000,
        max_vision_calls: 2
      },
      disclosure: { recipients: ['scripted'], max_text_chars: 10_000 },
      seeds: []
    }
  }

  private grant(row: GrantRow): Json {
    return {
      id: row.id, task_id: row.taskId, status: row.status, revision: row.revision,
      policy_version: 'public-research-v1', scope_digest: row.digest, scope: row.scope,
      created_at: AT, confirmed_at: row.confirmedAt,
      expires_at: row.status === 'ACTIVE' ? FAR : null,
      revoked_at: row.status === 'REVOKED' ? AT : null,
      completed_at: row.status === 'COMPLETED' ? AT : null
    }
  }

  private view(taskId: string): Json {
    const task = this.tasks.get(taskId)!
    const grant = [...this.grants.values()].find((row) => row.taskId === taskId)
    const answer = this.answers.get(taskId)
    return {
      task: this.task(task),
      objective: task.request.objective,
      grant: grant ? this.grant(grant) : null,
      session: this.session
        ? { id: this.session, status: 'OPEN', worker_generation: WORKER, created_at: AT, closed_at: null }
        : null,
      observations: this.observations.map((row) => row.body),
      answer: answer ?? null,
      usage: {
        steps: this.requests.size,
        observations: this.observations.length,
        planner_calls: this.plannerCalls,
        active_seconds: 1.5,
        tabs: this.observations.length > 0 ? 1 : 0
      },
      search_configured: this.options.searchConfigured !== false,
      unresolved_step: this.unresolved
    }
  }

  private plannerCalls = 0
  /** Set when a step ended with an outcome the runtime cannot stand behind. */
  private unresolved = false

  private observation(kind: string, operation: string, extra: Json): Json {
    const sequence = this.observations.length + 1
    const body: Json = {
      id: randomUUID(),
      task_id: [...this.tasks.keys()][0],
      action_id: randomUUID(),
      attempt_id: randomUUID(),
      session_id: kind === 'search_results' ? null : this.session,
      worker_generation: kind === 'search_results' ? null : WORKER,
      sequence,
      ref: `o${sequence}`,
      schema_version: 1,
      provenance: 'untrusted_environment',
      kind,
      operation,
      tab: kind === 'search_results' ? null : 't1',
      document_epoch: 1,
      query: null,
      requested_url: null,
      final_url: null,
      final_host: null,
      redirects: [],
      title: '',
      settled: true,
      truncated: false,
      observed_at: AT,
      content_hash: digest([kind, sequence, extra]),
      blocks: [],
      links: [],
      results: [],
      open_tabs: kind === 'search_results' ? [] : ['t1'],
      total_text_chars: 0,
      total_link_count: 0,
      ...extra
    }
    this.observations.push({ id: body.id as string, sequence, body })
    return body
  }

  private page(url: string, title: string, blocks: readonly string[], links: Array<[string, string]>): Json {
    return this.observation('page', 'navigate', {
      final_url: url,
      final_host: new URL(url).hostname,
      requested_url: url,
      title,
      blocks: blocks.map((text, index) => ({ id: `b${index + 1}`, text })),
      // The runtime serialises a link as id/text/host; the desktop parser
      // renames the id to a ref. No address crosses this boundary.
      links: links.map(([text], index) => ({ id: `l${index + 1}`, text, host: new URL(url).hostname })),
      total_text_chars: blocks.join('').length,
      total_link_count: links.length
    })
  }

  private handle(method: RuntimeMethod, path: string, body: Json): [number, unknown] | Error {
    const error = (status: number, code: string, extra: Json = {}): [number, unknown] =>
      [status, { error: { code, message: code, ...extra } }]
    let match: RegExpExecArray | null
    if (method === 'POST' && path === '/tasks') {
      const request = body.request as Json
      const row: TaskRow = { id: randomUUID(), status: 'CREATED', revision: 1, request, events: [] }
      row.events.push({ id: 1, task_id: row.id, sequence: 1, task_revision: 1, event_type: 'task.created', payload: {}, created_at: AT })
      this.tasks.set(row.id, row)
      return [201, this.task(row)]
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})$/.exec(path))) {
      const row = this.tasks.get(match[1])
      return row ? [200, this.task(row)] : error(404, 'task_not_found')
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/actions\?limit=100$/.exec(path))) {
      return [200, { task_id: match[1], actions: [] }]
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/events\?after_sequence=(\d+)&limit=\d+$/.exec(path))) {
      const row = this.tasks.get(match[1])!
      return [200, { task_id: row.id, events: row.events.filter((event) => (event.sequence as number) > Number(match![2])) }]
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/research$/.exec(path))) {
      return this.tasks.has(match[1]) ? [200, this.view(match[1])] : error(404, 'task_not_found')
    }
    if ((match = /^\/tasks\/([0-9a-f-]{36})\/research\/(prepare|grant|revoke|steps|answer)$/.exec(path))) {
      if (method !== 'POST') return error(422, 'invalid_request')
      const taskId = match[1]
      const task = this.tasks.get(taskId)
      if (!task) return error(404, 'task_not_found')
      const route = match[2]
      const grant = [...this.grants.values()].find((row) => row.taskId === taskId)
      if (route === 'prepare') {
        if (grant) return [201, this.view(taskId)]
        const scope = this.scope()
        const row: GrantRow = {
          id: randomUUID(), taskId, status: 'PENDING', revision: 1, scope, digest: digest(scope), confirmedAt: null
        }
        this.grants.set(row.id, row)
        this.event(task, 'task.research_scope_requested', { grant_id: row.id })
        return [201, this.view(taskId)]
      }
      if (route === 'grant') {
        if (!grant || grant.id !== body.grant_id) return error(404, 'research_grant_not_found')
        if (grant.status !== 'PENDING') return error(409, 'research_grant_not_usable')
        if (grant.revision !== body.expected_revision) return error(409, 'research_grant_not_usable')
        grant.status = 'ACTIVE'
        grant.revision += 1
        grant.confirmedAt = AT
        this.session = randomUUID()
        this.event(task, 'task.research_scope_granted', { grant_id: grant.id })
        return [200, this.view(taskId)]
      }
      if (route === 'revoke') {
        if (!grant) return error(404, 'research_grant_not_found')
        grant.status = 'REVOKED'
        grant.revision += 1
        grant.confirmedAt = grant.confirmedAt ?? AT
        this.session = null
        this.event(task, 'task.research_scope_revoked', { grant_id: grant.id, reason: body.reason })
        return [200, this.view(taskId)]
      }
      if (route === 'answer') {
        if (!grant) return error(404, 'research_grant_not_found')
        if (this.answers.has(taskId)) return error(409, 'research_answer_already_recorded')
        const answer = body.answer as Json
        const evidence = (answer.evidence ?? []) as Array<Json>
        for (const item of evidence) {
          const observation = this.observations.find((row) => row.body.ref === item.observation)
          const blocks = (observation?.body.blocks ?? []) as Array<Json>
          const block = blocks.find((candidate) => candidate.id === item.block)
          if (!block) return [422, { error: { code: 'research_answer_not_grounded', message: 'x', reason: 'unknown_block' } }]
          if (!String(block.text).toLowerCase().includes(String(item.quote).toLowerCase())) {
            return [422, { error: { code: 'research_answer_not_grounded', message: 'x', reason: 'quote_not_in_block' } }]
          }
        }
        this.answers.set(taskId, {
          status: answer.status, stop_reason: answer.stop_reason, answer: answer.answer,
          evidence, provider: body.provider, model: body.model,
          steps_used: this.requests.size, observations_used: this.observations.length,
          planner_calls: body.planner_calls ?? 0, created_at: AT
        })
        grant.status = 'COMPLETED'
        grant.revision += 1
        this.session = null
        task.status = 'SUCCEEDED'
        this.event(task, 'task.research_answer_recorded', { answer_status: answer.status })
        return [200, this.view(taskId)]
      }
      // route === 'steps'
      if (!grant) return error(404, 'research_grant_not_found')
      if (grant.status === 'PENDING') return error(409, 'research_grant_not_usable')
      if (grant.status !== 'ACTIVE') return error(409, 'research_grant_not_usable')
      const requestId = String(body.request_id ?? '')
      const step = (body.step ?? {}) as Json
      this.plannerCalls = Math.max(this.plannerCalls, Number(body.planner_calls ?? 0))
      const existing = this.requests.get(requestId)
      if (existing) {
        return [201, { research: this.view(taskId), action: existing, observation: null, outcome: 'SUCCEEDED', error_code: null, replayed: true }]
      }
      const operations = (grant.scope as Json).allowed_operations as string[]
      if (!operations.includes(String(step.operation))) {
        return [422, { error: { code: 'research_step_refused', message: 'x', reason: 'outside_scope' } }]
      }
      const budgets = ((grant.scope as Json).budgets ?? {}) as Json
      if (this.requests.size >= Number(budgets.max_steps)) {
        return [409, { error: { code: 'research_budget_exhausted', message: 'x', reason: 'max_steps' } }]
      }
      if (step.operation === 'navigate' && this.refuseNextNavigate) {
        const reason = this.refuseNextNavigate
        this.refuseNextNavigate = null
        return [422, { error: { code: 'research_step_refused', message: 'x', reason } }]
      }
      const action: Json = {
        id: randomUUID(), task_id: taskId, idempotency_key: `research:${requestId}`,
        tool_name: `research_${step.operation === 'public_search' ? 'search' : String(step.operation)}`,
        risk_tier: 'R1', proposal: { kind: 'public_research_step' }, proposal_digest: digest(step),
        status: 'SUCCEEDED', revision: 4, created_at: AT, updated_at: AT, approval: null, attempts: []
      }
      this.requests.set(requestId, action)
      const outcome = this.nextStepOutcome
      this.nextStepOutcome = 'SUCCEEDED'
      if (outcome === 'OUTCOME_UNKNOWN') this.unresolved = true
      let observation: Json | null = null
      if (outcome === 'SUCCEEDED') {
        if (step.operation === 'public_search') {
          this.searches += 1
          observation = this.observation('search_results', 'public_search', {
            query: step.query,
            results: [
              { id: 'r1', title: 'Lumi projects directory', host: 'example.com', snippet: 'An index of projects named Lumi' },
              { id: 'r2', title: 'lumi-coffee-grinder', host: 'example.com', snippet: 'Stars: 9,912' }
            ]
          })
        } else if (step.operation === 'navigate') {
          this.pageOpens += 1
          const target = (step.target ?? {}) as Json
          observation = this.nextPage
            ? this.page(this.nextPage.url, this.nextPage.title, this.nextPage.blocks, [])
            : target.kind === 'result'
              ? this.page('https://example.com/research/hub', 'Projects named Lumi', HUB_BLOCKS, [['lumi-desktop — desktop companion', 'x']])
              : this.page('https://example.com/research/project', 'lumi-desktop', PROJECT_BLOCKS, [])
          this.nextPage = undefined
        } else {
          observation = this.observation('tab_state', String(step.operation), {})
        }
      }
      if (this.loseNextStepResponse) {
        this.loseNextStepResponse = false
        return new RuntimeRestartedError()
      }
      return [201, {
        research: this.view(taskId), action, observation,
        outcome, error_code: outcome === 'SUCCEEDED' ? null : 'navigation_blocked', replayed: false
      }]
    }
    return error(422, 'invalid_request')
  }
}
