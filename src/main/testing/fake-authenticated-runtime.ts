import { createHash, randomUUID } from 'node:crypto'
import { RuntimeRestartedError, type RuntimeMethod, type RuntimeReply } from '../services/agent-runtime-supervisor'
import type { RuntimeRequester } from '../services/agent-tasks'

/**
 * Test helper: the runtime's authenticated-account-reading routes, in memory,
 * with the semantics the Python tests pin down.
 *
 * It refuses what the real runtime refuses -- a step before the grant is
 * confirmed, a step outside the scope, a stale ref, a replayed request id, a
 * budget that has run out, an answer attributed to a provider that is not the
 * grant's recipient -- and it can simulate the deterministic pauses the worker
 * reports (a credential surface, a different account, an unknown account, a
 * departure from the site). It counts what was *served* (`pagesServed`) so a
 * test can assert that a pause really stopped the work rather than merely
 * reporting it.
 */

type Json = Record<string, unknown>
export const AUTH_GENERATION = '00000000-0000-4000-8000-0000000000aa'
const WORKER = '00000000-0000-4000-8000-0000000000bb'
export const PROFILE_ID = '00000000-0000-4000-8000-0000000000cc'
const AT = '2026-09-20T10:00:00+00:00'
const FAR = '2099-01-01T00:00:00+00:00'

const digest = (value: unknown): string => createHash('sha256').update(JSON.stringify(value)).digest('hex')

/** The signed-in account page, already redacted the way the worker returns it. */
export const ACCOUNT_BLOCKS = [
  'Your repositories',
  'You own 5 repositories.',
  'lumi-notes - Private',
  'public-site - Public',
  'secret-plans - Private',
  'demo-app - Public',
  'dotfiles - Private'
]
/** A page whose text carries a planted marker, for the classification tests. */
export const PRIVATE_MARKER = 'PRIVATE_ACCOUNT_MARKER_92F31'
export const MARKER_BLOCKS = ['Organization', `Marker ${PRIVATE_MARKER}.`, 'There are 17 private repositories.']

interface TaskRow { id: string; status: string; revision: number; request: Json; events: Json[] }
interface GrantRow {
  id: string; taskId: string; status: string; revision: number; scope: Json; digest: string; confirmedAt: string | null
}

export interface FakeAuthenticatedOptions {
  /** Operations the prepared scope lists. Anything else is refused. */
  operations?: string[]
  maxSteps?: number
  /** The profile's status when the task is prepared. */
  profileStatus?: 'AUTHENTICATED' | 'NEEDS_LOGIN'
  /** Blocks served by the first `observe`. */
  blocks?: readonly string[]
  label?: string
  site?: string
}

export type SimulatedPause = 'login_required' | 'account_changed' | 'account_identity_unknown' | 'left_site_scope'

export class FakeAuthenticatedRuntime implements RuntimeRequester {
  tasks = new Map<string, TaskRow>()
  grants = new Map<string, GrantRow>()
  observations: Array<{ id: string; sequence: number; body: Json }> = []
  answers = new Map<string, Json>()
  requests = new Map<string, Json>()
  calls: Array<{ method: RuntimeMethod; path: string; body: unknown }> = []
  /** Pages the (pretend) browser served. A pause must not increase it. */
  pagesServed = 0
  pauseReasonFor = new Map<string, SimulatedPause>()
  /** Set to simulate the next step ending in a deterministic pause. */
  pauseNext: SimulatedPause | undefined
  nextStepOutcome: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN' = 'SUCCEEDED'
  /** Set to make the next step refuse, as a stale ref or scope check would. */
  refuseNextStep: string | undefined
  /**
   * Milestone 12 S3: set to make the next step refuse as `AuthenticatedReadService.execute_step`'s own
   * profile pre-check would (`_check_profile_readable`) -- e.g. `profile_not_authenticated` (the profile
   * reverted to `NEEDS_LOGIN` mid-session, still just needing a person to finish, the grant itself is fine)
   * or `profile_takeover_active` (a sign-in is literally in progress). The grant stays ACTIVE either way --
   * only the profile check fails, before any worker dispatch.
   */
  refuseNextStepAsProfileUnavailable: string | undefined
  /**
   * Milestone 12 S3: set to make the next step refuse as `execute_step`'s own fingerprint/epoch pre-check
   * would (`AuthenticatedGrantNotUsableError`) -- a fresh, right-now check finds the grant dead (a different
   * account, or expired) even though an EARLIER read (e.g. `loadAuthenticated`'s own GET) still reported it
   * ACTIVE. Deliberately independent of `grant.status` itself, to simulate that exact race rather than one
   * where the grant was already visibly dead before the step was even attempted.
   */
  refuseNextStepAsGrantUnusable: string | undefined
  loseNextStepResponse = false
  /** Serve these blocks from the next observation instead of the account page. */
  nextBlocks: readonly string[] | undefined
  private plannerCalls = 0
  private unresolved = false

  constructor(private readonly options: FakeAuthenticatedOptions = {}) {}

  async request(method: RuntimeMethod, path: string, body: unknown): Promise<RuntimeReply> {
    this.calls.push({ method, path, body })
    const reply = this.handle(method, path, (body ?? {}) as Json)
    if (reply instanceof Error) throw reply
    return { status: reply[0], body: reply[1], generation: AUTH_GENERATION }
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

  private scope(recipient: string): Json {
    const operations = this.options.operations ?? ['navigate', 'observe', 'reveal', 'tab', 'history']
    return {
      policy_version: 'authenticated-read-v1',
      site: this.options.site ?? 'github.com',
      allowed_origins: ['https://github.com', 'https://www.github.com'],
      allowed_operations: operations,
      methods: ['GET', 'HEAD'],
      classification: 'account_private',
      allowed: ['read_pages_on_site', 'follow_links_within_site', 'own_account_reading_tabs'],
      forbidden: [
        'sign_in_for_you', 'ask_for_password_or_code', 'type_into_forms', 'submit_anything', 'leave_site',
        'uploads_and_downloads', 'purchases_and_messages', 'non_get_requests'
      ],
      website_side_effects_possible: true,
      disclosure: {
        recipient, max_text_chars: 4_000, max_blocks: 60, identifiers_reduced: true, failover: 'none'
      },
      budgets: {
        max_steps: this.options.maxSteps ?? 12,
        max_observations: 12,
        max_planner_calls: 12,
        max_answer_calls: 2,
        max_tabs: 3,
        max_active_seconds: 300,
        max_vision_calls: 0
      }
    }
  }

  private grant(row: GrantRow): Json {
    return {
      id: row.id, task_id: row.taskId, status: row.status, revision: row.revision,
      policy_version: 'authenticated-read-v1', scope_digest: row.digest, scope: row.scope,
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
      classification: 'account_private',
      profile: {
        id: PROFILE_ID, label: this.options.label ?? 'GitHub - Personal', site: this.options.site ?? 'github.com',
        status: this.options.profileStatus ?? 'AUTHENTICATED'
      },
      grant: grant ? this.grant(grant) : null,
      observations: this.observations.map((row) => row.body),
      answer: answer ?? null,
      usage: {
        steps: this.requests.size,
        observations: this.observations.length,
        planner_calls: this.plannerCalls,
        active_seconds: 1.5,
        tabs: this.observations.length > 0 ? 1 : 0
      },
      pause_reason: this.pauseReasonFor.get(taskId) ?? null,
      unresolved_step: this.unresolved
    }
  }

  /** The form-planning card. Overridden by `FakeFormRuntime`; empty for plain account reading. */
  protected formPlan(taskId: string): Json {
    return {
      task_id: taskId, task_status: 'READY', objective: 'x', site: this.options.site ?? 'github.com',
      saved_details: [], grant: null, disclosure: null, form_count: 0, candidate_element_count: 0, max_fields: 12
    }
  }

  private observation(operation: string, blocks: readonly string[]): Json {
    const sequence = this.observations.length + 1
    const taskId = [...this.tasks.keys()][0]
    const body: Json = {
      id: randomUUID(),
      task_id: taskId,
      action_id: randomUUID(),
      attempt_id: randomUUID(),
      worker_generation: WORKER,
      sequence,
      ref: `o${sequence}`,
      schema_version: 1,
      provenance: 'untrusted_environment',
      classification: 'account_private',
      kind: 'page',
      operation,
      tab: 't1',
      document_epoch: 1,
      host: this.options.site ?? 'github.com',
      title: 'Your repositories',
      settled: true,
      truncated: false,
      observed_at: AT,
      content_hash: digest([sequence, blocks]),
      blocks: blocks.map((text, index) => ({ id: `b${index + 1}`, text })),
      links: [
        { id: 'l1', text: 'Organization', host: this.options.site ?? 'github.com' },
        { id: 'l2', text: 'Billing', host: this.options.site ?? 'github.com' }
      ],
      open_tabs: ['t1'],
      total_text_chars: blocks.join('').length,
      total_link_count: 2,
      redactions: {}
    }
    this.observations.push({ id: body.id as string, sequence, body })
    return body
  }

  private handle(method: RuntimeMethod, path: string, body: Json): [number, unknown] | Error {
    const error = (status: number, code: string, extra: Json = {}): [number, unknown] =>
      [status, { error: { code, message: code, ...extra } }]
    let match: RegExpExecArray | null
    if (method === 'POST' && path === '/tasks') {
      const request = body.request as Json
      // The runtime checks the profile when the task is created, and opens nothing.
      if ((this.options.profileStatus ?? 'AUTHENTICATED') !== 'AUTHENTICATED') {
        return error(409, 'authenticated_profile_unavailable', { reason: 'profile_not_authenticated' })
      }
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
    // Milestone 8b S5: the form-planning card. Empty here; `FakeFormRuntime` fills it.
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/authenticated\/form$/.exec(path))) {
      return this.tasks.has(match[1]) ? [200, this.formPlan(match[1])] : error(404, 'task_not_found')
    }
    if (method === 'GET' && (match = /^\/tasks\/([0-9a-f-]{36})\/authenticated$/.exec(path))) {
      return this.tasks.has(match[1]) ? [200, this.view(match[1])] : error(404, 'task_not_found')
    }
    if ((match = /^\/tasks\/([0-9a-f-]{36})\/authenticated\/(prepare|grant|revoke|steps|answer)$/.exec(path))) {
      if (method !== 'POST') return error(422, 'invalid_request')
      const taskId = match[1]
      const task = this.tasks.get(taskId)
      if (!task) return error(404, 'task_not_found')
      const route = match[2]
      const grant = [...this.grants.values()].find((row) => row.taskId === taskId)
      if (route === 'prepare') {
        if (grant) return [201, this.view(taskId)]
        const status = this.options.profileStatus ?? 'AUTHENTICATED'
        if (status !== 'AUTHENTICATED') return error(409, 'authenticated_profile_unavailable', { reason: 'profile_not_authenticated' })
        const scope = this.scope(String(body.recipient))
        const row: GrantRow = {
          id: randomUUID(), taskId, status: 'PENDING', revision: 1, scope, digest: digest(scope), confirmedAt: null
        }
        this.grants.set(row.id, row)
        this.event(task, 'task.authenticated_scope_requested', { grant_id: row.id })
        return [201, this.view(taskId)]
      }
      if (route === 'grant') {
        if (!grant || grant.id !== body.grant_id) return error(404, 'authenticated_grant_not_found')
        if (grant.status !== 'PENDING') return error(409, 'authenticated_grant_not_usable', { reason: 'it is active' })
        if (grant.revision !== body.expected_revision) return error(409, 'authenticated_grant_not_usable', { reason: 'changed' })
        grant.status = 'ACTIVE'
        grant.revision += 1
        grant.confirmedAt = AT
        this.event(task, 'task.authenticated_scope_granted', { grant_id: grant.id })
        return [200, this.view(taskId)]
      }
      if (route === 'revoke') {
        if (!grant) return error(404, 'authenticated_grant_not_found')
        grant.status = 'REVOKED'
        grant.revision += 1
        grant.confirmedAt = grant.confirmedAt ?? AT
        this.event(task, 'task.authenticated_scope_revoked', { grant_id: grant.id, reason: body.reason })
        return [200, this.view(taskId)]
      }
      if (route === 'answer') {
        if (!grant) return error(404, 'authenticated_grant_not_found')
        if (this.answers.has(taskId)) return error(409, 'authenticated_answer_already_recorded')
        const recipient = ((grant.scope.disclosure as Json).recipient as string)
        if (body.provider !== recipient) return error(422, 'authenticated_step_refused', { reason: 'recipient_mismatch' })
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
          classification: 'account_private', profile_id: PROFILE_ID,
          status: answer.status, stop_reason: answer.stop_reason, answer: answer.answer,
          evidence, provider: body.provider, model: body.model,
          steps_used: this.requests.size, observations_used: this.observations.length,
          planner_calls: body.planner_calls ?? 0, created_at: AT
        })
        grant.status = 'COMPLETED'
        grant.revision += 1
        task.status = 'SUCCEEDED'
        this.event(task, 'task.authenticated_answer_recorded', { answer_status: answer.status })
        return [200, this.view(taskId)]
      }
      // route === 'steps'
      if (!grant) return error(404, 'authenticated_grant_not_found')
      if (grant.status !== 'ACTIVE') return error(409, 'authenticated_grant_not_usable', { reason: 'not active' })
      if (this.refuseNextStepAsProfileUnavailable) {
        const reason = this.refuseNextStepAsProfileUnavailable
        this.refuseNextStepAsProfileUnavailable = undefined
        return error(409, 'authenticated_profile_unavailable', { reason })
      }
      if (this.refuseNextStepAsGrantUnusable) {
        const reason = this.refuseNextStepAsGrantUnusable
        this.refuseNextStepAsGrantUnusable = undefined
        return error(409, 'authenticated_grant_not_usable', { reason })
      }
      const requestId = String(body.request_id ?? '')
      const step = (body.step ?? {}) as Json
      this.plannerCalls = Math.max(this.plannerCalls, Number(body.planner_calls ?? 0))
      const existing = this.requests.get(requestId)
      if (existing) {
        return [201, { authenticated: this.view(taskId), action: existing, observation: null, outcome: 'SUCCEEDED', error_code: null, pause_reason: null, replayed: true }]
      }
      const operations = (grant.scope as Json).allowed_operations as string[]
      if (!operations.includes(String(step.operation))) {
        return [422, { error: { code: 'authenticated_step_refused', message: 'x', reason: 'outside_scope' } }]
      }
      const budgets = ((grant.scope as Json).budgets ?? {}) as Json
      if (this.requests.size >= Number(budgets.max_steps)) {
        return [409, { error: { code: 'authenticated_budget_exhausted', message: 'x', reason: 'max_steps' } }]
      }
      if (this.unresolved && step.operation !== 'observe') {
        return [422, { error: { code: 'authenticated_step_refused', message: 'x', reason: 'observe_required' } }]
      }
      if (this.refuseNextStep) {
        const reason = this.refuseNextStep
        this.refuseNextStep = undefined
        return [422, { error: { code: 'authenticated_step_refused', message: 'x', reason } }]
      }
      const action: Json = {
        id: randomUUID(), task_id: taskId, idempotency_key: `authenticated:${requestId}`,
        tool_name: `authenticated_${String(step.operation)}`,
        risk_tier: 'R1', proposal: { kind: 'authenticated_read_step', classification: 'account_private' },
        proposal_digest: digest(step), status: 'SUCCEEDED', revision: 4, created_at: AT, updated_at: AT,
        approval: null, attempts: []
      }
      this.requests.set(requestId, action)
      if (this.pauseNext) {
        const reason = this.pauseNext
        this.pauseNext = undefined
        this.pauseReasonFor.set(taskId, reason)
        task.status = 'PAUSED'
        this.event(task, 'task.authenticated_paused', { reason })
        return [201, {
          authenticated: this.view(taskId), action, observation: null,
          outcome: 'FAILED', error_code: reason, pause_reason: reason, replayed: false
        }]
      }
      const outcome = this.nextStepOutcome
      this.nextStepOutcome = 'SUCCEEDED'
      if (outcome === 'OUTCOME_UNKNOWN') this.unresolved = true
      let observation: Json | null = null
      if (outcome === 'SUCCEEDED') {
        this.pagesServed += 1
        this.unresolved = false
        const blocks = this.nextBlocks ?? this.options.blocks ?? ACCOUNT_BLOCKS
        this.nextBlocks = undefined
        observation = this.observation(String(step.operation), blocks)
      }
      if (this.loseNextStepResponse) {
        this.loseNextStepResponse = false
        return new RuntimeRestartedError()
      }
      return [201, {
        authenticated: this.view(taskId), action, observation,
        outcome, error_code: outcome === 'SUCCEEDED' ? null : 'timeout_after_submission',
        pause_reason: null, replayed: false
      }]
    }
    return error(422, 'invalid_request')
  }
}
