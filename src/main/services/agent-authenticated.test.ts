import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ActiveTaskStore, AgentTaskController } from './agent-tasks'
import {
  ACCOUNT_BLOCKS, FakeAuthenticatedRuntime, MARKER_BLOCKS, PRIVATE_MARKER, type SimulatedPause
} from '../testing/fake-authenticated-runtime'
import { AuthenticatedAnswerer } from '../agent/authenticated-answer'
import { AuthenticatedPlanner } from '../agent/authenticated-planner'
import { ResearchPlanner } from '../agent/research-planner'
import { DiagnosticsLog } from '../agent/diagnostics'
import { AgentMemoryStore } from '../agent/agent-memory'
import { buildContext } from '../models/context-builder'
import { ModelRouter, DEFAULT_ROUTES } from '../models/model-router'
import { ModelProviderError, type ModelProvider, type ModelRequest, type ModelResponse } from '../models/provider'
import { ScriptedTextProvider } from '../models/scripted-provider'
import type { TextProviderId } from '../../shared/model-contracts'
import type { AgentDisclosureRecipient, AgentTaskSnapshot } from '../../shared/agent-contracts'

/**
 * Authenticated account reading in Electron main, against the runtime's
 * semantics and against **two real providers' worth of routing**.
 *
 * The properties that matter are the ones the milestone exists for:
 *
 *  - nothing is opened, and nothing leaves the machine, before the trusted Allow;
 *  - the provider is the grant's, once, and every other provider sees zero calls;
 *  - if that provider fails, the run stops -- there is no second provider;
 *  - a deterministic pause (credential surface, other account, unknown account,
 *    departure from the site) ends the run with **no page text sent to anyone**;
 *  - nothing read through an account reaches a later, unrelated prompt, episodic
 *    memory or a diagnostic record.
 */

const QUESTION = 'Which of my repositories are private?'
const PRIVATE_LIST = ['lumi-notes - Private', 'secret-plans - Private', 'dotfiles - Private']

/** A provider that records every request it is sent, and can be made to fail. */
class CapturingProvider implements ModelProvider {
  readonly capabilities = { json: true, vision: false, contextTokens: 32_000 }
  readonly calls: ModelRequest[] = []
  mode: 'ok' | 'fail' = 'ok'
  reply: ((request: ModelRequest) => string) | undefined
  private readonly inner: ScriptedTextProvider

  constructor(readonly id: TextProviderId, readonly model: string) {
    this.inner = new ScriptedTextProvider(id, 'rules')
  }

  configured(): boolean {
    return true
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    if (this.mode === 'fail') throw new ModelProviderError('unavailable', 503)
    if (this.reply) return { text: this.reply(request), provider: this.id, model: this.model, usage: {} }
    const scripted = await this.inner.generate(request)
    return { ...scripted, provider: this.id, model: this.model }
  }

  ofClass(taskClass: string): ModelRequest[] {
    return this.calls.filter((call) => call.taskClass === taskClass)
  }

  everything(): string {
    return this.calls.map((call) => `${call.system}\n${call.input}`).join('\n')
  }
}

interface Harness {
  controller: AgentTaskController
  runtime: FakeAuthenticatedRuntime
  a: CapturingProvider
  b: CapturingProvider
  router: ModelRouter
  diagnostics: DiagnosticsLog
}

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-authenticated-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function harness(options: { runtime?: FakeAuthenticatedRuntime } = {}): Harness {
  const runtime = options.runtime ?? new FakeAuthenticatedRuntime()
  // Two providers whose recipient ids differ: A is `gemini`, B is `openai`.
  const a = new CapturingProvider('gemini', 'gemini-2.5-flash')
  const b = new CapturingProvider('openai', 'gpt-5.6-luna')
  const diagnostics = new DiagnosticsLog()
  const router = new ModelRouter((id) => [a, b].find((provider) => provider.id === id), DEFAULT_ROUTES, diagnostics)
  const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, undefined, {
    planner: new AuthenticatedPlanner(router),
    answerer: new AuthenticatedAnswerer(router)
  })
  return { controller, runtime, a, b, router, diagnostics }
}

async function started(h: Harness, recipient: AgentDisclosureRecipient = 'gemini'): Promise<AgentTaskSnapshot> {
  const created = await h.controller.createAuthenticatedTask(QUESTION, '00000000-0000-4000-8000-0000000000cc', recipient)
  expect(created.ok).toBe(true)
  if (!created.ok) throw new Error('unreachable')
  return created.value
}

async function granted(h: Harness, recipient: AgentDisclosureRecipient = 'gemini'): Promise<AgentTaskSnapshot> {
  const snapshot = await started(h, recipient)
  const grant = snapshot.authenticated?.grant
  expect(grant?.status).toBe('PENDING')
  const confirmed = await h.controller.grantAuthenticatedScope(grant!.grantId, grant!.revision)
  expect(confirmed.ok).toBe(true)
  if (!confirmed.ok) throw new Error('unreachable')
  return confirmed.value
}

describe('starting an authenticated task', () => {
  it('shows the disclosure scope and opens, reads and sends nothing', async () => {
    const h = harness()
    const snapshot = await started(h)
    expect(snapshot.task.kind).toBe('authenticated_read')
    expect(snapshot.task.authenticated?.classification).toBe('account_private')
    const scope = snapshot.authenticated?.grant?.scope
    expect(snapshot.authenticated?.grant?.status).toBe('PENDING')
    expect(scope?.recipient).toBe('gemini')
    expect(scope?.maxTextChars).toBe(4_000)
    expect(scope?.maxBlocks).toBe(60)
    expect(scope?.methods).toEqual(['GET', 'HEAD'])
    expect(scope?.websiteSideEffectsPossible).toBe(true)
    expect(scope?.budgets.maxVisionCalls).toBe(0)
    expect(h.runtime.pagesServed).toBe(0)
    expect(h.runtime.count('POST', /\/authenticated\/steps$/)).toBe(0)
    expect(h.a.calls.length + h.b.calls.length).toBe(0)
  })

  it('refuses a recipient main did not offer, before the runtime or any provider is contacted', async () => {
    const h = harness()
    for (const forged of ['claude', 'https://evil.example', 'GEMINI', '', 'gemini,openai', 'scripted']) {
      const result = await h.controller.createAuthenticatedTask(QUESTION, '00000000-0000-4000-8000-0000000000cc', forged)
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.error.code).toBe('invalid_request')
    }
    expect(h.runtime.calls).toHaveLength(0)
    expect(h.a.calls.length + h.b.calls.length).toBe(0)
  })

  it('refuses a profile that is not signed in, deterministically, and keeps no task', async () => {
    const h = harness({ runtime: new FakeAuthenticatedRuntime({ profileStatus: 'NEEDS_LOGIN' }) })
    const result = await h.controller.createAuthenticatedTask(QUESTION, '00000000-0000-4000-8000-0000000000cc', 'gemini')
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.code).toBe('authenticated_unavailable')
      expect(result.error.message).toContain('Sign in manually')
    }
    expect(h.runtime.pagesServed).toBe(0)
    // The runtime refused before a task existed, so there is nothing to clean up.
    expect(h.runtime.tasks.size).toBe(0)
    expect((await h.controller.loadActiveTask(0))).toEqual({ ok: true, value: null })
    expect(h.a.calls.length + h.b.calls.length).toBe(0)
  })

  it('needs no voice session to create or run a task', async () => {
    const h = harness()
    const snapshot = await granted(h)
    expect(snapshot.authenticated?.grant?.status).toBe('ACTIVE')
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
  })
})

describe('the trusted click', () => {
  it('cannot be skipped: running before Allow opens and sends nothing', async () => {
    const h = harness()
    await started(h)
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    if (!run.ok) expect(run.error.code).toBe('authenticated_not_granted')
    expect(h.runtime.pagesServed).toBe(0)
    expect(h.a.calls.length + h.b.calls.length).toBe(0)
  })

  it('names the grant and revision that were on screen', async () => {
    const h = harness()
    const snapshot = await started(h)
    const grant = snapshot.authenticated!.grant!
    const stale = await h.controller.grantAuthenticatedScope(grant.grantId, grant.revision + 5)
    expect(stale.ok).toBe(false)
    if (!stale.ok) expect(stale.error.code).toBe('stale_revision')
    const wrong = await h.controller.grantAuthenticatedScope('00000000-0000-4000-8000-000000000123', grant.revision)
    expect(wrong.ok).toBe(false)
    expect(h.runtime.count('POST', /\/authenticated\/grant$/)).toBe(0)
  })

  it('declining withdraws the scope and nothing is opened or sent', async () => {
    const h = harness()
    const snapshot = await started(h)
    const grant = snapshot.authenticated!.grant!
    const declined = await h.controller.declineAuthenticatedScope(grant.grantId, grant.revision)
    expect(declined.ok).toBe(true)
    if (declined.ok) expect(declined.value.authenticated?.grant?.status).toBe('REVOKED')
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    expect(h.runtime.pagesServed).toBe(0)
    expect(h.a.calls.length + h.b.calls.length).toBe(0)
  })
})

describe('the fixture question, end to end', () => {
  it('answers which repositories are private, with citations, and only the approved provider is called', async () => {
    const h = harness()
    await granted(h, 'gemini')
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    if (!run.ok) return
    const answer = run.value.authenticated?.answer
    expect(answer?.status).toBe('answered')
    for (const repository of PRIVATE_LIST) expect(answer?.answer).toContain(repository)
    expect(answer?.answer).not.toContain('public-site')
    expect(answer?.provider).toBe('gemini')
    expect(answer?.classification).toBe('account_private')
    expect(answer?.evidence.map((item) => item.quote)).toEqual(PRIVATE_LIST)
    // Every citation names an observation and block that exist in this task.
    for (const item of answer!.evidence) {
      const observation = run.value.authenticated!.observations.find((entry) => entry.ref === item.observation)
      expect(observation?.blocks.find((block) => block.id === item.block)?.text).toContain(item.quote)
    }
    // Provider routing: the approved provider received the private payload
    // exactly as permitted; every other provider received nothing at all.
    expect(h.a.ofClass('authenticated_planning').length).toBeGreaterThan(0)
    expect(h.a.ofClass('authenticated_answer')).toHaveLength(1)
    expect(h.a.everything()).toContain('lumi-notes - Private')
    expect(h.b.calls).toHaveLength(0)
    // Nothing else used a different class for account text.
    expect(h.a.calls.every((call) => call.taskClass === 'authenticated_planning' || call.taskClass === 'authenticated_answer')).toBe(true)
    // No vision, ever.
    expect(h.a.calls.every((call) => call.image === undefined)).toBe(true)
    // The grant is closed and the timeline never says the user approved a step.
    expect(run.value.authenticated?.grant?.status).toBe('COMPLETED')
  })

  it('uses the grant provider, whichever one that is', async () => {
    const h = harness()
    await granted(h, 'openai')
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    expect(h.b.calls.length).toBeGreaterThan(0)
    expect(h.a.calls).toHaveLength(0)
    if (run.ok) expect(run.value.authenticated?.answer?.provider).toBe('openai')
  })
})

describe('one provider, zero failover', () => {
  it('stops with model_unavailable when the approved provider fails to plan, and calls no other', async () => {
    const h = harness()
    await granted(h, 'gemini')
    h.a.mode = 'fail'
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    if (!run.ok) expect(run.error.code).toBe('model_unavailable')
    expect(h.a.calls).toHaveLength(1)
    expect(h.b.calls).toHaveLength(0)
    expect(h.runtime.pagesServed).toBe(0)
    expect(h.runtime.answers.size).toBe(0)
  })

  it('stops with model_unavailable when the approved provider fails to answer, and keeps the evidence', async () => {
    const h = harness()
    await granted(h, 'gemini')
    // Plans normally, then dies just before it is asked to answer.
    const scripted = new ScriptedTextProvider('gemini', 'rules')
    h.a.reply = undefined
    const original = h.a.generate.bind(h.a)
    h.a.generate = async (request: ModelRequest): Promise<ModelResponse> => {
      if (request.taskClass === 'authenticated_answer') {
        h.a.calls.push(request)
        throw new ModelProviderError('unavailable', 503)
      }
      return original(request)
    }
    void scripted
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    if (!run.ok) expect(run.error.code).toBe('model_unavailable')
    expect(h.a.ofClass('authenticated_answer')).toHaveLength(1)
    expect(h.b.calls).toHaveLength(0)
    expect(h.runtime.answers.size).toBe(0)
    // The redacted evidence is durable; no page is opened again to answer later.
    expect(h.runtime.observations.length).toBeGreaterThan(0)
  })

  it('stops when the approved provider is no longer configured, and never substitutes another', async () => {
    const h = harness()
    await granted(h, 'gemini')
    const only = new ModelRouter((id) => (id === 'openai' ? h.b : undefined), DEFAULT_ROUTES)
    const controller = new AgentTaskController(h.runtime, new ActiveTaskStore(directory), undefined, undefined, {
      planner: new AuthenticatedPlanner(only), answerer: new AuthenticatedAnswerer(only)
    })
    const run = await controller.runAuthenticated()
    expect(run.ok).toBe(false)
    if (!run.ok) expect(run.error.code).toBe('model_unavailable')
    expect(h.b.calls).toHaveLength(0)
  })

  it('treats a planner reply carrying a provider, URL or selector as a failure of that provider, not a route to another', async () => {
    const h = harness()
    await granted(h, 'gemini')
    h.a.reply = () => JSON.stringify({ action: 'step', operation: 'observe', tab: 't1', provider: 'openai', url: 'https://evil.example' })
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    if (!run.ok) expect(run.error.code).toBe('model_unavailable')
    expect(h.a.calls).toHaveLength(1)
    expect(h.b.calls).toHaveLength(0)
    expect(h.runtime.pagesServed).toBe(0)
  })
})

describe('deterministic pauses send no page text to any provider', () => {
  const pauses: SimulatedPause[] = ['login_required', 'account_changed', 'account_identity_unknown', 'left_site_scope']

  for (const reason of pauses) {
    it(`${reason}: ends the run, asks no model again, and no account text was ever sent`, async () => {
      const h = harness()
      await granted(h)
      h.runtime.pauseNext = reason
      const run = await h.controller.runAuthenticated()
      expect(run.ok).toBe(true)
      if (!run.ok) return
      expect(run.value.authenticated?.pauseReason).toBe(reason)
      expect(run.value.task.status).toBe('PAUSED')
      // One planner call chose the step that paused; nothing was asked after it.
      expect(h.a.ofClass('authenticated_planning')).toHaveLength(1)
      expect(h.a.ofClass('authenticated_answer')).toHaveLength(0)
      expect(h.b.calls).toHaveLength(0)
      expect(h.runtime.pagesServed).toBe(0)
      expect(h.runtime.answers.size).toBe(0)
      for (const block of ACCOUNT_BLOCKS) expect(h.a.everything()).not.toContain(block)
    })
  }

  it('does not resume by itself: a paused task plans nothing on the next run', async () => {
    const h = harness()
    await granted(h)
    h.runtime.pauseNext = 'login_required'
    await h.controller.runAuthenticated()
    const before = h.a.calls.length
    const again = await h.controller.runAuthenticated()
    expect(again.ok).toBe(true)
    expect(h.a.calls.length).toBe(before)
  })
})

describe('unresolved and refused steps', () => {
  it('looks again after an unknown outcome and never repeats the same request', async () => {
    const h = harness()
    await granted(h)
    h.runtime.nextStepOutcome = 'OUTCOME_UNKNOWN'
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    // The unknown step, then a forced observe -- and each was a distinct request.
    const steps = h.runtime.calls.filter((call) => /\/authenticated\/steps$/.test(call.path))
    expect(steps.length).toBeGreaterThanOrEqual(2)
    expect(new Set(steps.map((call) => (call.body as { request_id: string }).request_id)).size).toBe(steps.length)
    expect((steps[1].body as { step: { operation: string } }).step.operation).toBe('observe')
    if (run.ok) expect(run.value.authenticated?.answer?.status).toBe('answered')
  })

  it('re-observes after a refusal, then stops asking rather than arguing', async () => {
    const h = harness()
    await granted(h)
    h.runtime.refuseNextStep = 'stale_target_ref'
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    const steps = h.runtime.calls.filter((call) => /\/authenticated\/steps$/.test(call.path))
    expect((steps[1].body as { step: { operation: string } }).step.operation).toBe('observe')
  })

  it('stops at the runtime budget with an honest answer from what was read', async () => {
    const h = harness({ runtime: new FakeAuthenticatedRuntime({ maxSteps: 1 }) })
    await granted(h)
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    if (run.ok) expect(run.value.authenticated?.usage.steps).toBeLessThanOrEqual(1)
  })
})

describe('what the planner may say', () => {
  it('cannot be made to submit a step outside the scope', async () => {
    const h = harness({ runtime: new FakeAuthenticatedRuntime({ operations: ['observe'] }) })
    await granted(h)
    h.a.reply = () => JSON.stringify({ action: 'step', operation: 'navigate', tab: 't1', target: 'link', observation: 'o1', ref: 'l1' })
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(false)
    expect(h.runtime.count('POST', /\/authenticated\/steps$/)).toBe(0)
  })
})

describe('the classification firewall', () => {
  async function finishedPrivateTask(): Promise<Harness> {
    const runtime = new FakeAuthenticatedRuntime({ blocks: MARKER_BLOCKS })
    const h = harness({ runtime })
    const created = await h.controller.createAuthenticatedTask(
      'What is the marker in my organization?', '00000000-0000-4000-8000-0000000000cc', 'gemini')
    expect(created.ok).toBe(true)
    if (!created.ok) throw new Error('unreachable')
    const grant = created.value.authenticated!.grant!
    await h.controller.grantAuthenticatedScope(grant.grantId, grant.revision)
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    return h
  }

  it('lets the marker reach only the approved provider, in the two private classes', async () => {
    const h = await finishedPrivateTask()
    expect(h.a.everything()).toContain(PRIVATE_MARKER)
    expect(h.a.calls.every((call) => call.taskClass.startsWith('authenticated_'))).toBe(true)
    expect(h.b.everything()).not.toContain(PRIVATE_MARKER)
  })

  it('keeps the marker out of a later public research prompt', async () => {
    const h = await finishedPrivateTask()
    const before = h.a.calls.length + h.b.calls.length
    const planner = new ResearchPlanner(h.router)
    await planner.next({
      objective: 'Find the Lumi project page',
      taskId: '00000000-0000-4000-8000-0000000000dd',
      view: {
        taskId: '00000000-0000-4000-8000-0000000000dd', objective: 'Find the Lumi project page',
        observations: [], usage: { steps: 0, observations: 0, plannerCalls: 0, activeSeconds: 0, tabs: 0 },
        searchConfigured: true, unresolvedStep: false
      }
    }).catch(() => undefined)
    const publicCalls = [...h.a.calls, ...h.b.calls].slice(before)
    expect(publicCalls.length).toBeGreaterThan(0)
    for (const call of publicCalls) expect(`${call.system}\n${call.input}`).not.toContain(PRIVATE_MARKER)
  })

  it('keeps an authenticated task out of the context built for any other request', async () => {
    const h = await finishedPrivateTask()
    const loaded = await h.controller.loadActiveTask(0)
    expect(loaded.ok && loaded.value?.authenticated?.observations.length).toBeGreaterThan(0)
    if (!loaded.ok || !loaded.value) throw new Error('unreachable')
    const built = buildContext({ rules: 'rules', utterance: 'Book a dermatologist', task: loaded.value }, { maxInputTokens: 8_000 })
    expect(built.input).not.toContain(PRIVATE_MARKER)
    expect(built.input).not.toContain('CURRENT TASK')
    expect(built.sections.find((section) => section.name === 'task_state')?.included).toBe(false)
  })

  it('refuses to summarise an account-private task into episodic memory', async () => {
    const store = new AgentMemoryStore(directory)
    await store.recordEpisode({
      taskId: '00000000-0000-4000-8000-0000000000ee', kind: 'clinic_info',
      summary: `Read the account ${PRIVATE_MARKER}`, sequence: 3, classification: 'account_private'
    })
    expect(await store.episodes()).toEqual([])
    const file = await readFile(join(directory, 'agent-memory.json'), 'utf8').catch(() => '')
    expect(file).not.toContain(PRIVATE_MARKER)
    // A public episode is still recorded.
    await store.recordEpisode({ taskId: '00000000-0000-4000-8000-0000000000ef', kind: 'clinic_info', summary: 'Read clinic info', sequence: 2 })
    expect(await store.episodes()).toHaveLength(1)
  })

  it('records no page text, identifier or answer in diagnostics', async () => {
    const h = await finishedPrivateTask()
    const dumped = JSON.stringify(h.diagnostics.list())
    expect(h.diagnostics.list().length).toBeGreaterThan(0)
    expect(dumped).not.toContain(PRIVATE_MARKER)
    for (const block of ACCOUNT_BLOCKS) expect(dumped).not.toContain(block)
    expect(dumped).not.toContain('Which of my')
  })
})
