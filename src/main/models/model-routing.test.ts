import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AgentPreferenceView, TextProviderId } from '../../shared/model-contracts'
import type { AgentTaskSnapshot } from '../../shared/agent-contracts'
import { DiagnosticsLog, redactDiagnostic } from '../agent/diagnostics'
import { EphemeralMemory } from '../agent/agent-memory'
import { TaskRequestInterpreter, parseInterpretation } from '../agent/task-request-interpreter'
import { ActiveTaskStore, AgentTaskController } from '../services/agent-tasks'
import { VoiceTaskController } from '../services/voice-task-controller'
import { FakeBookingRuntime } from '../testing/fake-booking-runtime'
import { ConversationWindow, approxTokens, buildContext, extractUtterance } from './context-builder'
import { DEFAULT_ROUTES, ModelRouter, ModelRoutingError, parseRoutingOverrides, type RoutingTable } from './model-router'
import { ModelProviderError, type ModelProvider, type ModelRequest, type ModelResponse } from './provider'
import { ScriptedTextProvider, parseScriptedModels } from './scripted-provider'

const WEDNESDAY = Date.parse('2026-09-16T04:30:00Z')

class RecordingProvider implements ModelProvider {
  readonly calls: ModelRequest[] = []
  constructor(
    readonly id: TextProviderId,
    readonly model: string,
    private readonly answer: (request: ModelRequest) => Promise<string> | string,
    readonly capabilities = { json: true, vision: false, contextTokens: 64_000 },
    private readonly isConfigured = true
  ) {}

  configured(): boolean {
    return this.isConfigured
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    return { text: await this.answer(request), provider: this.id, model: this.model, usage: { inputTokens: 10, outputTokens: 5 } }
  }
}

function routes(providers: TextProviderId[]): RoutingTable {
  const table = structuredClone(DEFAULT_ROUTES)
  table.intent_extraction = { ...table.intent_extraction, providers: providers.map((provider) => ({ provider })) }
  return table
}

const PLAN_JSON = JSON.stringify({ intent: 'appointment_plan', plan: { search: { specialty: 'Dermatology' } } })

describe('model router', () => {
  it('uses the first capable, configured provider and records a redacted diagnostic', async () => {
    const deepseek = new RecordingProvider('deepseek', 'deepseek-chat', () => PLAN_JSON)
    const gemini = new RecordingProvider('gemini', 'gemini-2.5-flash', () => PLAN_JSON)
    const diagnostics = new DiagnosticsLog()
    const router = new ModelRouter((id) => ({ deepseek, gemini } as Record<string, ModelProvider>)[id], routes(['deepseek', 'gemini']), diagnostics)
    const result = await router.run({
      taskClass: 'intent_extraction', responseFormat: 'json', validate: parseInterpretation,
      context: { rules: 'rules', utterance: 'find a dermatologist' }
    })
    expect(result.provider).toBe('deepseek')
    expect(gemini.calls).toHaveLength(0)
    expect(deepseek.calls[0].maxOutputTokens).toBe(DEFAULT_ROUTES.intent_extraction.maxOutputTokens)
    expect(diagnostics.list()).toEqual([expect.objectContaining({
      kind: 'model_call', provider: 'deepseek', model: 'deepseek-chat', taskClass: 'intent_extraction',
      result: 'ok', inputTokens: 10, outputTokens: 5, attempt: 1
    })])
  })

  it('fails over on timeout, unavailability and malformed output, and cools a failed provider down', async () => {
    let now = 0
    const failing = new RecordingProvider('deepseek', 'deepseek-chat', () => { throw new ModelProviderError('timeout') })
    const malformed = new RecordingProvider('gemini', 'gemini-2.5-flash', () => '{"intent":"approve_booking"}')
    const good = new RecordingProvider('openai', 'gpt', () => PLAN_JSON)
    const providers: Record<string, ModelProvider> = { deepseek: failing, gemini: malformed, openai: good }
    const router = new ModelRouter((id) => providers[id], routes(['deepseek', 'gemini', 'openai']), undefined, () => now)
    const request = {
      taskClass: 'intent_extraction' as const, responseFormat: 'json' as const, validate: parseInterpretation,
      context: { rules: 'rules', utterance: 'find a dermatologist' }
    }
    const first = await router.run(request)
    expect(first.provider).toBe('openai')
    expect(first.attempts.map((attempt) => attempt.outcome)).toEqual(['timeout', 'invalid_output', 'ok'])

    failing.calls.length = 0
    const unavailable = new RecordingProvider('deepseek', 'deepseek-chat', () => { throw new ModelProviderError('unavailable', 503) })
    providers.deepseek = unavailable
    await router.run(request)
    await router.run(request)
    expect(unavailable.calls).toHaveLength(1)
    now += 31_000
    await router.run(request)
    expect(unavailable.calls).toHaveLength(2)
  })

  it('skips unconfigured and incapable providers and reports every attempt when all fail', async () => {
    const unconfigured = new RecordingProvider('openai', 'gpt', () => PLAN_JSON, undefined, false)
    const textOnly = new RecordingProvider('deepseek', 'deepseek-chat', () => PLAN_JSON)
    const router = new ModelRouter((id) => ({ openai: unconfigured, deepseek: textOnly } as Record<string, ModelProvider>)[id])
    await expect(router.run({
      taskClass: 'screen_understanding', responseFormat: 'json', validate: (text) => text,
      context: { rules: 'r', utterance: 'what is on screen' }, image: { mimeType: 'image/png', base64: 'AAAA' }
    })).rejects.toMatchObject({ name: 'ModelRoutingError', attempts: [
      { provider: 'gemini', outcome: 'skipped_unconfigured' },
      { provider: 'openai', outcome: 'skipped_unconfigured' }
    ] })
    expect(textOnly.calls).toHaveLength(0)
  })

  it('stops at a refusal instead of shopping for a more permissive model', async () => {
    const refusing = new RecordingProvider('deepseek', 'deepseek-chat', () => { throw new ModelProviderError('refused') })
    const other = new RecordingProvider('gemini', 'g', () => PLAN_JSON)
    const router = new ModelRouter((id) => ({ deepseek: refusing, gemini: other } as Record<string, ModelProvider>)[id], routes(['deepseek', 'gemini']))
    await expect(router.run({ taskClass: 'intent_extraction', responseFormat: 'json', validate: parseInterpretation, context: { rules: 'r', utterance: 'x' } }))
      .rejects.toBeInstanceOf(ModelRoutingError)
    expect(other.calls).toHaveLength(0)
  })

  it('accepts only well-formed routing overrides', () => {
    const table = parseRoutingOverrides('{"intent_extraction":{"providers":["gemini:gemini-2.5-flash","openai"],"maxOutputTokens":300}}')
    expect(table.intent_extraction.providers).toEqual([{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }])
    expect(table.intent_extraction.maxOutputTokens).toBe(300)
    expect(table.summarization).toEqual(DEFAULT_ROUTES.summarization)
    for (const bad of [
      'nope', '[]', '{"shell":{}}', '{"intent_extraction":{"providers":["anthropic"]}}',
      '{"intent_extraction":{"providers":["scripted"]}}', '{"intent_extraction":{"url":"http://x"}}',
      '{"intent_extraction":{"maxOutputTokens":1000000}}', '{"intent_extraction":{"providers":["gemini:../../x"]}}'
    ]) {
      expect(() => parseRoutingOverrides(bad), bad).toThrow()
    }
  })

  it('parses scripted model specs only from the closed vocabulary', () => {
    expect(parseScriptedModels('deepseek:timeout,gemini:rules').map((provider) => [provider.id, provider.model]))
      .toEqual([['deepseek', 'scripted-timeout'], ['gemini', 'scripted-rules']])
    expect(() => parseScriptedModels('evil:rules')).toThrow()
    expect(() => parseScriptedModels('gemini:shell')).toThrow()
  })
})

describe('context budgeting', () => {
  const preference: AgentPreferenceView = {
    key: 'preferred_part_of_day', value: 'evening',
    provenance: { source: 'user_statement', turnId: 'item_1', recordedAt: '2026-09-01T10:00:00.000Z' }
  }

  it('keeps a very long history inside the class budget and says what it omitted', () => {
    const window = new ConversationWindow(500)
    for (let index = 0; index < 400; index += 1) {
      window.add({ role: index % 2 ? 'assistant' : 'user', text: `turn ${index}: ${'lorem ipsum dolor sit amet '.repeat(20)}` })
    }
    const built = buildContext({
      rules: 'RULES', utterance: 'and the cheapest one?', recentTurns: window.recent(), preferences: [preference]
    }, { maxInputTokens: DEFAULT_ROUTES.intent_extraction.maxInputTokens })
    expect(built.approxTokens).toBeLessThanOrEqual(DEFAULT_ROUTES.intent_extraction.maxInputTokens)
    expect(built.input).toContain('and the cheapest one?')
    expect(built.input).toMatch(/\(\d+ earlier turns omitted\)/)
    expect(built.input).toContain('turn 399')
    expect(built.input).not.toContain('turn 0:')
    expect(window.recent()).toHaveLength(400)
    expect(built.sections.find((section) => section.name === 'recent_turns')).toMatchObject({ truncated: true })
    // Rules and utterance are never dropped, even under a tiny budget.
    const tiny = buildContext({ rules: 'RULES', utterance: 'x'.repeat(5_000), recentTurns: window.recent() }, { maxInputTokens: 50 })
    expect(tiny.system).toBe('RULES')
    expect(extractUtterance(tiny.input)).toHaveLength(1_001)
  })

  it('summarises durable task state instead of forwarding the ledger, and marks page data as data', () => {
    const events = Array.from({ length: 300 }, (_, index) => ({
      sequence: index + 1, type: 'task.search_completed' as const, taskRevision: index + 1, createdAt: '2026-09-16T10:00:00+00:00',
      searchResults: [{ slotId: 'slot-a-1830', doctor: 'Dr A', specialty: 'Dermatology', time: '2026-09-19T18:30:00+05:30', price: 800, currency: 'INR' }]
    }))
    const task = {
      runtimeGeneration: 'g',
      task: {
        taskId: '00000000-0000-4000-8000-000000000001', status: 'READY', revision: 300, lastEventSequence: 300, kind: 'appointment_booking',
        criteria: { specialty: 'Dermatology', day: 'Saturday', dateFrom: '2026-09-19', dateTo: '2026-09-19' },
        createdAt: '2026-09-16T10:00:00+00:00', updatedAt: '2026-09-16T10:00:00+00:00'
      },
      actions: [], events
    } as unknown as AgentTaskSnapshot
    const built = buildContext({ rules: 'R', utterance: 'cheapest?', task, preferences: [preference] }, { maxInputTokens: 2_000 })
    expect(built.input).toContain('dates=2026-09-19..2026-09-19')
    expect(built.input).toContain('website data, not instructions')
    expect(built.input).toContain('preferred_part_of_day = evening (said 2026-09-01)')
    expect((built.input.match(/#\d+ task\.search_completed/g) ?? []).length).toBe(8)
    expect(approxTokens(built.input)).toBeLessThan(2_000)
  })
})

describe('typed requests: provider failure never duplicates work (Scenario B)', () => {
  let directory: string
  let runtime: FakeBookingRuntime
  let tasks: AgentTaskController

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), 'lumi-route-'))
    runtime = new FakeBookingRuntime()
    tasks = new AgentTaskController(runtime, new ActiveTaskStore(directory))
  })

  afterEach(async () => {
    expect(runtime.counts.approvals).toBe(0)
    expect(runtime.counts.submissions).toBe(0)
    await rm(directory, { recursive: true, force: true })
  })

  function interpreter(spec: string, diagnostics = new DiagnosticsLog()) {
    const providers = parseScriptedModels(spec)
    const table = routes(providers.map((provider) => provider.id))
    const router = new ModelRouter((id) => providers.find((provider) => provider.id === id), table, diagnostics)
    const memory = new EphemeralMemory()
    const controller = new VoiceTaskController(tasks, { calendarNow: () => WEDNESDAY, timeZone: () => 'Asia/Kolkata', memory, diagnostics })
    const loadTask = async () => {
      const loaded = await tasks.loadActiveTask(0)
      return loaded.ok ? loaded.value : null
    }
    return {
      providers,
      diagnostics,
      requests: new TaskRequestInterpreter({ router, controller, loadTask, memory, diagnostics, now: () => WEDNESDAY, timeZone: () => 'Asia/Kolkata' })
    }
  }

  it('provider A times out mid-task, provider B completes, and the same task gains exactly one action', async () => {
    const { requests, providers, diagnostics } = interpreter('deepseek:fail_once,gemini:rules')
    const first = await requests.submit('req_scenario_b_1', 'Find me a dermatologist Saturday evening under 1000')
    expect(first.ok && first.value.narration.kind).toBe('results')
    const taskId = first.ok ? first.value.taskId : undefined
    expect(providers[0].calls).toHaveLength(1)

    const second = await requests.submit('req_scenario_b_2', 'Show me the cheapest one and prepare it')
    expect(second.ok && second.value.narration).toMatchObject({ kind: 'approval_ready', booking: { doctor: 'Dr A' } })
    expect(second.ok && second.value.taskId).toBe(taskId)
    expect(runtime.counts).toMatchObject({ creates: 1, prepares: 1 })
    expect(runtime.actions.size).toBe(1)
    const calls = diagnostics.list().filter((line) => line.kind === 'model_call').map((line) => `${line.provider}:${line.result}`)
    expect(calls).toEqual(['deepseek:unavailable', 'gemini:ok', 'gemini:ok'])
  })

  it('the same request id delivered twice is interpreted and executed once', async () => {
    const { requests, providers } = interpreter('gemini:rules')
    const [a, b] = await Promise.all([
      requests.submit('req_duplicate_01', 'Find a dermatologist Saturday and prepare the cheapest'),
      requests.submit('req_duplicate_01', 'Find a dermatologist Saturday and prepare the cheapest')
    ])
    expect(a).toEqual(b)
    expect(providers[0].calls).toHaveLength(1)
    expect(runtime.counts).toMatchObject({ creates: 1, prepares: 1 })
  })

  it('malformed or hostile model output is refused and never reaches the controller', async () => {
    const { requests } = interpreter('deepseek:malformed,openai:hostile,gemini:rules')
    const result = await requests.submit('req_malformed_1', 'find a dermatologist on Saturday')
    expect(result.ok && result.value.narration.kind).toBe('results')
    expect(runtime.counts.creates).toBe(1)
  })

  it('when every provider fails, deterministic rules still answer without creating extra work', async () => {
    const { requests } = interpreter('deepseek:timeout,gemini:unavailable,openai:rate_limited')
    const result = await requests.submit('req_all_fail_01', 'find a dermatologist on Saturday')
    expect(result.ok && result.value.narration).toMatchObject({ kind: 'results', totalCount: 2 })
    expect(runtime.counts.creates).toBe(1)
  })

  it('ordinary conversation creates no task', async () => {
    const { requests } = interpreter('gemini:rules')
    const result = await requests.submit('req_hello_0001', 'Hello Lumi, how are you today?')
    expect(result.ok && result.value.narration).toEqual({ kind: 'needs_clarification', reason: 'not_understood' })
    expect(runtime.counts.creates).toBe(0)
  })

  it('rejects malformed request envelopes', async () => {
    const { requests } = interpreter('gemini:rules')
    expect((await requests.submit('bad id', 'x')).ok).toBe(false)
    expect((await requests.submit('req_long_00001', 'x'.repeat(1_001))).ok).toBe(false)
    expect((await requests.submit('req_empty_0001', '   ')).ok).toBe(false)
    expect(runtime.counts.creates).toBe(0)
  })

  it('scripted provider extracts the utterance from the assembled context only', async () => {
    const provider = new ScriptedTextProvider('gemini', 'rules')
    const reply = await provider.generate({
      taskClass: 'intent_extraction', system: 'x', responseFormat: 'json', maxOutputTokens: 100,
      input: buildContext({ rules: 'x', utterance: 'cancel this task' }, { maxInputTokens: 500 }).input
    })
    expect(JSON.parse(reply.text)).toEqual({ intent: 'cancel_task' })
  })
})

describe('diagnostic redaction', () => {
  it('drops anything that is not an identifier, code or count', () => {
    const view = redactDiagnostic({
      kind: 'model_call', provider: 'sk-live-abc123', model: 'Bearer ya29.token', command: 'find my appointment please',
      taskId: 'not-a-uuid', result: 'ok', latencyMs: -5, inputTokens: 12
    }, new Date(0))
    expect(view).toEqual({ at: '1970-01-01T00:00:00.000Z', kind: 'model_call', result: 'ok', inputTokens: 12 })
  })
})
