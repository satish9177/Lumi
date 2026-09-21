import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ActiveTaskStore, AgentTaskController, parseProtectedDataRefs } from './agent-tasks'
import { FakeFormRuntime, HOSTILE_LABEL, RAW_MARKERS } from '../testing/fake-form-runtime'
import { AuthenticatedAnswerer } from '../agent/authenticated-answer'
import { AuthenticatedPlanner } from '../agent/authenticated-planner'
import { FormPlanner, parseFormPlanDecision } from '../agent/form-planner'
import { DiagnosticsLog } from '../agent/diagnostics'
import { ModelRouter, DEFAULT_ROUTES } from '../models/model-router'
import { ModelProviderError, type ModelProvider, type ModelRequest, type ModelResponse } from '../models/provider'
import { ScriptedTextProvider, type ScriptedBehaviour } from '../models/scripted-provider'
import type { TextProviderId } from '../../shared/model-contracts'
import { PROTECTED_DATA_KINDS, type AgentDisclosureRecipient, type AgentTaskSnapshot } from '../../shared/agent-contracts'

/**
 * Form planning and exact disclosure approval (Milestone 8b S5), in Electron main.
 *
 * The properties that matter: nothing about a form or a saved detail reaches a
 * provider before the trusted Allow; exactly one provider ever sees it and a
 * failure stops the run; no raw saved value is in any provider request; a hostile
 * label cannot widen anything; the renderer supplies an id and a revision and
 * nothing else; and **no website mutation can occur anywhere in the flow**.
 */

const QUESTION = 'Help me apply for this job'
const PROFILE = '00000000-0000-4000-8000-0000000000cc'

class CapturingProvider implements ModelProvider {
  readonly capabilities = { json: true, vision: false, contextTokens: 32_000 }
  readonly calls: ModelRequest[] = []
  mode: 'ok' | 'fail' = 'ok'
  private readonly inner: ScriptedTextProvider

  constructor(readonly id: TextProviderId, readonly model: string, behaviour: ScriptedBehaviour = 'rules') {
    this.inner = new ScriptedTextProvider(id, behaviour)
  }

  configured(): boolean { return true }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    if (this.mode === 'fail') throw new ModelProviderError('unavailable', 503)
    const scripted = await this.inner.generate(request)
    return { ...scripted, provider: this.id, model: this.model }
  }

  ofClass(taskClass: string): ModelRequest[] { return this.calls.filter((call) => call.taskClass === taskClass) }
  everything(): string { return this.calls.map((call) => `${call.system}\n${call.input}`).join('\n') }
}

interface Harness {
  controller: AgentTaskController
  runtime: FakeFormRuntime
  a: CapturingProvider
  b: CapturingProvider
  diagnostics: DiagnosticsLog
}

let directory: string
beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'lumi-form-plan-')) })
afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

function harness(options: { runtime?: FakeFormRuntime; behaviour?: ScriptedBehaviour } = {}): Harness {
  const runtime = options.runtime ?? new FakeFormRuntime()
  const a = new CapturingProvider('gemini', 'gemini-2.5-flash', options.behaviour ?? 'rules')
  const b = new CapturingProvider('openai', 'gpt-5.6-luna')
  const diagnostics = new DiagnosticsLog()
  const router = new ModelRouter((id) => [a, b].find((provider) => provider.id === id), DEFAULT_ROUTES, diagnostics)
  const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, undefined, {
    planner: new AuthenticatedPlanner(router),
    answerer: new AuthenticatedAnswerer(router),
    formPlanner: new FormPlanner(router)
  })
  return { controller, runtime, a, b, diagnostics }
}

/** An account-reading task whose read grant is active, with a form observed. */
async function readable(h: Harness, recipient: AgentDisclosureRecipient = 'gemini'): Promise<AgentTaskSnapshot> {
  const created = await h.controller.createAuthenticatedTask(QUESTION, PROFILE, recipient)
  if (!created.ok) throw new Error(created.error.message)
  const grant = created.value.authenticated!.grant!
  const confirmed = await h.controller.grantAuthenticatedScope(grant.grantId, grant.revision)
  if (!confirmed.ok) throw new Error(confirmed.error.message)
  return confirmed.value
}

async function prepared(h: Harness, refs: string[] = ['legal_name', 'email', 'phone', 'country']): Promise<AgentTaskSnapshot> {
  await readable(h)
  const result = await h.controller.prepareFormPlanning(refs)
  if (!result.ok) throw new Error(result.error.message)
  return result.value
}

async function allowed(h: Harness, refs?: string[]): Promise<AgentTaskSnapshot> {
  const snapshot = await prepared(h, refs)
  const grant = snapshot.formPlan!.grant!
  const result = await h.controller.grantFormPlanning(grant.grantId, grant.revision)
  if (!result.ok) throw new Error(result.error.message)
  return result.value
}

async function waiting(h: Harness): Promise<AgentTaskSnapshot> {
  await allowed(h)
  const result = await h.controller.runFormPlanning()
  if (!result.ok) throw new Error(result.error.message)
  return result.value
}

const providerCalls = (h: Harness): number => h.a.calls.length + h.b.calls.length

describe('the form-planning grant', () => {
  it('shows the planning card and sends nothing to any provider', async () => {
    const h = harness()
    const snapshot = await prepared(h)
    const grant = snapshot.formPlan!.grant!
    expect(grant.status).toBe('PENDING')
    expect(grant.allowedDataRefs).toEqual(['legal_name', 'email', 'phone', 'country'])
    expect(grant.planningRecipient).toBe('gemini')
    expect(providerCalls(h)).toBe(0)
    expect(h.runtime.contextsServed).toBe(0)
  })

  it('refuses to plan, and calls no provider, before the trusted Allow', async () => {
    const h = harness()
    await prepared(h)
    const result = await h.controller.runFormPlanning()
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('authenticated_not_granted')
    expect(providerCalls(h)).toBe(0)
    expect(h.runtime.contextsServed).toBe(0)
  })

  it('accepts only closed data refs -- never a name, a value, a path or a duplicate', async () => {
    const h = harness()
    await readable(h)
    const before = h.runtime.calls.length
    for (const forged of [
      [], ['password'], ['email', 'email'], ['Email'], ['../etc/passwd'], ['https://evil.example'],
      ['SATISH.SECRET@example.test'], 'email', null, [5], PROTECTED_DATA_KINDS.concat(['email'] as never)
    ]) {
      const result = await h.controller.prepareFormPlanning(forged)
      expect(result.ok, JSON.stringify(forged)).toBe(false)
      if (!result.ok) expect(result.error.code).toBe('invalid_request')
    }
    expect(h.runtime.calls.length).toBe(before)
    expect(() => parseProtectedDataRefs([...PROTECTED_DATA_KINDS])).not.toThrow()
  })

  it('needs an allowed account-reading permission first', async () => {
    const h = harness()
    const created = await h.controller.createAuthenticatedTask(QUESTION, PROFILE, 'gemini')
    expect(created.ok).toBe(true)
    const result = await h.controller.prepareFormPlanning(['email'])
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('authenticated_not_granted')
    expect(providerCalls(h)).toBe(0)
  })

  it('confirms only the grant and revision that were on screen', async () => {
    const h = harness()
    const snapshot = await prepared(h)
    const grant = snapshot.formPlan!.grant!
    const stale = await h.controller.grantFormPlanning(grant.grantId, grant.revision + 1)
    expect(stale.ok).toBe(false)
    if (!stale.ok) expect(stale.error.code).toBe('stale_revision')
    const other = await h.controller.grantFormPlanning('00000000-0000-4000-8000-0000000000ee', grant.revision)
    expect(other.ok).toBe(false)
    if (!other.ok) expect(other.error.code).toBe('not_found')
    expect(h.runtime.formGrant?.status).toBe('PENDING')
    expect(providerCalls(h)).toBe(0)
  })

  it('declining authorises nothing', async () => {
    const h = harness()
    const snapshot = await prepared(h)
    const grant = snapshot.formPlan!.grant!
    const declined = await h.controller.declineFormPlanning(grant.grantId, grant.revision)
    expect(declined.ok).toBe(true)
    expect(h.runtime.formGrant?.status).toBe('REVOKED')
    const run = await h.controller.runFormPlanning()
    expect(run.ok).toBe(false)
    expect(providerCalls(h)).toBe(0)
  })
})

describe('the planning provider', () => {
  it('receives exactly the form structure and masked previews, and never a raw saved value', async () => {
    const h = harness()
    await allowed(h)
    const result = await h.controller.runFormPlanning()
    expect(result.ok).toBe(true)
    const requests = h.a.ofClass('form_planning')
    expect(requests).toHaveLength(1)
    const seen = h.a.everything()
    // The rules text names what is forbidden; the *input* is what carries data.
    const input = requests.map((request) => request.input).join(' ')
    for (const marker of RAW_MARKERS) expect(seen).not.toContain(marker)
    expect(seen).toContain('saved detail [data email] E***@e***.test')
    expect(seen).toContain('Full legal name')
    expect(seen).toContain('option "India"')
    for (const absent of ['valueState', 'value_state', 'selector', 'http', 'fingerprint', 'digest', 'https://']) {
      expect(input).not.toContain(absent)
    }
    // Only the allowed refs have previews.
    expect(seen).not.toContain('portfolio')
  })

  it('is exactly one provider: every other provider sees zero calls', async () => {
    const h = harness()
    await waiting(h)
    expect(h.a.calls.length).toBe(1)
    expect(h.b.calls.length).toBe(0)
  })

  it('stops if that provider fails, and never tries another', async () => {
    const h = harness()
    await allowed(h)
    h.a.mode = 'fail'
    const result = await h.controller.runFormPlanning()
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('model_unavailable')
    expect(h.a.calls.length).toBe(1)
    expect(h.b.calls.length).toBe(0)
    expect(h.runtime.proposals).toHaveLength(0)
    expect(h.runtime.disclosure).toBeUndefined()
  })

  it('stops with model_unavailable if the granted provider is not configured', async () => {
    const h = harness()
    await allowed(h)
    const empty = new AgentTaskController(h.runtime, new ActiveTaskStore(directory), undefined, undefined, {
      planner: new AuthenticatedPlanner(new ModelRouter(() => undefined, DEFAULT_ROUTES)),
      answerer: new AuthenticatedAnswerer(new ModelRouter(() => undefined, DEFAULT_ROUTES)),
      formPlanner: new FormPlanner(new ModelRouter((id) => (id === 'openai' ? h.b : undefined), DEFAULT_ROUTES))
    })
    const result = await empty.runFormPlanning()
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('model_unavailable')
    expect(h.b.calls.length).toBe(0)
    expect(h.runtime.contextsServed).toBe(0)
  })

  it('cannot be redirected by a hostile label', async () => {
    const h = harness()
    await allowed(h)
    const result = await h.controller.runFormPlanning()
    expect(result.ok).toBe(true)
    // The hostile label reached the provider as data only, inside the untrusted section...
    const input = h.a.calls[0].input
    expect(input.indexOf(HOSTILE_LABEL)).toBeGreaterThan(input.indexOf('UNTRUSTED_WEBSITE_OBSERVATION'))
    // ...and changed nothing: only the allowed refs, only the one recipient, no extra entries.
    const proposal = h.runtime.proposals[0] as { entries: Array<Record<string, unknown>> }
    expect(proposal.entries.every((entry) => !('value' in entry) && !('origin' in entry))).toBe(true)
    expect(proposal.entries.map((entry) => entry.data_ref).filter(Boolean).sort()).toEqual(['email', 'legal_name', 'phone'])
    expect(h.b.calls.length).toBe(0)
  })

  it('refuses a malformed or hostile reply before anything is proposed', async () => {
    for (const behaviour of ['malformed', 'hostile'] as const) {
      const h = harness({ behaviour })
      await allowed(h)
      const result = await h.controller.runFormPlanning()
      expect(result.ok, behaviour).toBe(false)
      if (!result.ok) expect(result.error.code).toBe('model_unavailable')
      expect(h.runtime.calls.filter((call) => /form\/propose$/.test(call.path))).toHaveLength(0)
      expect(h.b.calls.length).toBe(0)
      await rm(directory, { recursive: true, force: true })
      directory = await mkdtemp(join(tmpdir(), 'lumi-form-plan-'))
    }
  })

  it('leaves ordinary account reading exactly as before: no form inventory, no previews', async () => {
    const h = harness()
    await readable(h)
    const run = await h.controller.runAuthenticated()
    expect(run.ok).toBe(true)
    const seen = h.a.everything()
    for (const absent of ['saved detail', '[data ', 'Full legal name', 'form_planning', 'E***@', 'ending 1234', 'candidate_element']) {
      expect(seen).not.toContain(absent)
    }
    expect(h.a.ofClass('form_planning')).toHaveLength(0)
    expect(h.runtime.contextsServed).toBe(0)
  })
})

describe('the exact disclosure approval', () => {
  it('shows the manifest exactly, without a raw value, and changes nothing until approved', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    expect(card.actionStatus).toBe('WAITING_APPROVAL')
    expect(card.site).toBe('jobs.example.test')
    expect(card.fields.map((field) => field.fieldLabel)).toEqual([
      'Full legal name', 'Email address', 'Phone', 'Country', 'I agree to the terms'
    ])
    const text = JSON.stringify(card)
    for (const marker of RAW_MARKERS) expect(text).not.toContain(marker)
    expect(text).toContain('E***@e***.test')
    // The country here is an option chosen from the form's own list, not a saved detail shown as-is.
    expect(card.revealsCountry).toBe(false)
    expect(h.runtime.approvals).toBe(0)
  })

  it('approves with an id and a revision only, fills locally (frozen), and mutates no page', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    const before = h.runtime.calls.length
    const result = await h.controller.approveFieldDisclosure(card.actionId, card.revision)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.value.formPlan?.disclosure?.resultCode).toBe('local_draft_prepared')
    expect(result.value.formPlan?.disclosure?.approvalStatus).toBe('CONSUMED')
    const approvals = h.runtime.calls.slice(before).filter((call) => /field-disclosure/.test(call.path))
    expect(approvals).toHaveLength(1)
    // The whole body is one key: no manifest, value, origin, field or provider.
    expect(Object.keys(approvals[0].body as object)).toEqual(['expected_revision'])
    expect(h.runtime.pageMutations).toBe(0)
    expect(h.runtime.approvals).toBe(1)
    expect(h.b.calls.length).toBe(0)
  })

  it('refuses a second approval of the same plan', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    expect((await h.controller.approveFieldDisclosure(card.actionId, card.revision)).ok).toBe(true)
    const again = await h.controller.approveFieldDisclosure(card.actionId, card.revision)
    expect(again.ok).toBe(false)
    expect(h.runtime.approvals).toBe(1)
  })

  it('refuses a stale revision or another action id, before contacting the runtime', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    const before = h.runtime.calls.filter((call) => /field-disclosure/.test(call.path)).length
    const stale = await h.controller.approveFieldDisclosure(card.actionId, card.revision + 1)
    expect(stale.ok).toBe(false)
    if (!stale.ok) expect(stale.error.code).toBe('stale_revision')
    const other = await h.controller.approveFieldDisclosure('00000000-0000-4000-8000-0000000000ee', card.revision)
    expect(other.ok).toBe(false)
    for (const bad of ['', 'not-an-id', null, 5, { id: card.actionId }]) {
      expect((await h.controller.approveFieldDisclosure(bad, card.revision)).ok).toBe(false)
    }
    expect((await h.controller.approveFieldDisclosure(card.actionId, 0)).ok).toBe(false)
    expect(h.runtime.calls.filter((call) => /field-disclosure/.test(call.path)).length).toBe(before)
  })

  it('rejecting authorises nothing', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    const result = await h.controller.rejectFieldDisclosure(card.actionId, card.revision)
    expect(result.ok).toBe(true)
    expect(h.runtime.disclosure?.status).toBe('REJECTED')
    expect(h.runtime.approvals).toBe(0)
    expect((await h.controller.approveFieldDisclosure(card.actionId, card.revision + 1)).ok).toBe(false)
  })

  it('reports a changed saved value or a changed account as stable, plain codes', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    h.runtime.savedValueChanged = true
    const changed = await h.controller.approveFieldDisclosure(card.actionId, card.revision)
    expect(changed.ok).toBe(false)
    if (!changed.ok) {
      expect(changed.error.code).toBe('form_plan_stale')
      expect(changed.error.message).toContain('saved detail changed')
    }
    h.runtime.savedValueChanged = false
    h.runtime.accountChanged = true
    const account = await h.controller.approveFieldDisclosure(card.actionId, card.revision)
    expect(account.ok).toBe(false)
    if (!account.ok) expect(account.error.message).toContain('account')
    expect(h.runtime.approvals).toBe(0)
  })

  it('takes nothing else from the caller: no route accepts a manifest, value, origin, field or provider', async () => {
    const h = harness()
    const snapshot = await waiting(h)
    const card = snapshot.formPlan!.disclosure!
    await h.controller.approveFieldDisclosure(card.actionId, card.revision)
    const bodies = h.runtime.calls.filter((call) => /field-disclosure|form\/(grant|revoke|prepare-scope)/.test(call.path))
      .map((call) => Object.keys(call.body as object).sort().join(','))
    for (const keys of bodies) {
      expect(keys).toMatch(/^(expected_revision|allowed_data_refs|expected_revision,grant_id|expected_revision,grant_id,reason)$/)
    }
  })
})

describe('the proposal parser (defence in depth in main)', () => {
  const offered = ['legal_name', 'email', 'phone'] as never
  const reply = (entries: unknown[], extra: Record<string, unknown> = {}): string =>
    JSON.stringify({ action: 'propose', observation: 'o1', form: 'f1', entries, ...extra })

  it('builds the runtime shape from checked primitives', () => {
    const decision = parseFormPlanDecision(reply([
      { element: 'e1', data: 'email' }, { element: 'e2', option: 'op1' }, { element: 'e3', checked: false }
    ]), offered)
    expect(decision).toEqual({
      kind: 'propose', reason: 'no reason given',
      proposal: {
        operation: 'prepare_form', observation: 'o1', form_ref: 'f1',
        entries: [{ element_ref: 'e1', data_ref: 'email' }, { element_ref: 'e2', option_ref: 'op1' }, { element_ref: 'e3', checked: false }]
      }
    })
  })

  it.each([
    ['a value', reply([{ element: 'e1', data: 'email', value: 'a@b.test' }])],
    ['an origin', reply([{ element: 'e1', data: 'email' }], { origin: 'https://evil.test' })],
    ['a provider', reply([{ element: 'e1', data: 'email' }], { provider: 'openai' })],
    ['a selector', reply([{ element: 'e1', data: 'email', selector: '#a' }])],
    ['a data ref that was not offered', reply([{ element: 'e1', data: 'portfolio_url' }])],
    ['a kind that does not exist', reply([{ element: 'e1', data: 'password' }])],
    ['two variants at once', reply([{ element: 'e1', data: 'email', option: 'op1' }])],
    ['a non-boolean checkbox', reply([{ element: 'e1', checked: 'yes' }])],
    ['a duplicate element', reply([{ element: 'e1', data: 'email' }, { element: 'e1', data: 'phone' }])],
    ['an element outside the grammar', reply([{ element: '#email', data: 'email' }])],
    ['no entries', reply([])],
    ['thirteen entries', reply(Array.from({ length: 13 }, (_, i) => ({ element: `e${i + 1}`, checked: true })))],
    ['prose around it', 'Sure! ' + reply([{ element: 'e1', data: 'email' }])],
    ['a step instead of a proposal', JSON.stringify({ action: 'step', operation: 'click' })]
  ])('refuses %s', (_name, text) => {
    expect(() => parseFormPlanDecision(text, offered)).toThrow()
  })

  it('accepts a stop', () => {
    expect(parseFormPlanDecision(JSON.stringify({ action: 'stop', reason: 'nothing fits' }), offered)).toEqual({ kind: 'stop', reason: 'nothing fits' })
  })
})
