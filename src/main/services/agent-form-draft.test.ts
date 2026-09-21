import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ActiveTaskStore, AgentTaskController } from './agent-tasks'
import { isAllowedRuntimeRoute } from './agent-runtime-supervisor'
import { parseFormPlan } from './agent-wire'
import { FakeFormRuntime, RAW_MARKERS } from '../testing/fake-form-runtime'
import { AuthenticatedAnswerer } from '../agent/authenticated-answer'
import { AuthenticatedPlanner } from '../agent/authenticated-planner'
import { FormPlanner } from '../agent/form-planner'
import { DiagnosticsLog } from '../agent/diagnostics'
import { ModelRouter, DEFAULT_ROUTES } from '../models/model-router'
import { ScriptedTextProvider } from '../models/scripted-provider'
import type { ModelProvider, ModelRequest, ModelResponse } from '../models/provider'
import type { TextProviderId } from '../../shared/model-contracts'
import { AGENT_IPC_CHANNELS, type AgentFormPlanView, type AgentTaskSnapshot } from '../../shared/agent-contracts'
import { describeFormPlan } from '../../renderer/src/agent-task-view'
import { clearTakeoverReconciliation, isCaptureBlocked, setFormDraftWindowActive, setTakeoverActive } from './capture'
import contract from '../../shared/agent-runtime-contract.json'

/**
 * The network-frozen local form draft (Milestone 8b S6), in Electron main and the trusted card.
 *
 * What is pinned here: the six new runtime routes are exactly the intended ones and nothing
 * nearby or generalised is reachable; every action is an id and a revision; the card says, before
 * the click, that approving FILLS the form with the network frozen; the network comes back only
 * through the second approval; a page the human changed is refused with the network still off;
 * and voice, a typed sentence and the conversation vocabulary reach none of it.
 */

const UUID = '00000000-0000-4000-8000-0000000000aa'
const PROFILE = '00000000-0000-4000-8000-0000000000cc'
const NOW = Date.parse('2026-09-21T10:00:00Z')

const NEW_ROUTES: Array<['POST', string]> = [
  ['POST', `/tasks/${UUID}/authenticated/form/preparation-mode`],
  ['POST', `/tasks/${UUID}/authenticated/form/stop`],
  ['POST', `/form-drafts/${UUID}/discard`],
  ['POST', `/form-drafts/${UUID}/handover-request`],
  ['POST', `/actions/${UUID}/form-handover/approve`],
  ['POST', `/actions/${UUID}/form-handover/reject`]
]

describe('the runtime route allowlist (Milestone 8b S6)', () => {
  it('accepts exactly the six intended routes', () => {
    for (const [method, path] of NEW_ROUTES) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
  })

  it('rejects nearby, parameterised and generalised routes', () => {
    const rejected: Array<['GET' | 'POST' | 'PUT' | 'DELETE', string]> = [
      ['GET', `/form-drafts/${UUID}/discard`], ['PUT', `/form-drafts/${UUID}/discard`],
      ['POST', `/form-drafts/${UUID}/discard/extra`], ['POST', `/form-drafts/${UUID}/handover`],
      ['POST', `/form-drafts/${UUID}/thaw`], ['POST', `/form-drafts/${UUID}/unfreeze`],
      ['POST', `/form-drafts/${UUID}/fill`], ['POST', `/form-drafts/${UUID}/submit`],
      ['POST', `/form-drafts/discard`], ['POST', `/form-drafts/not-a-uuid/discard`],
      ['POST', `/form-drafts/${UUID}/{operation}`], ['POST', `/form-drafts/${UUID}/discard?force=1`],
      ['POST', `/draft/${UUID}/discard`], ['POST', `/drafts/${UUID}/discard`],
      ['POST', `/actions/${UUID}/form-handover/execute`], ['POST', `/actions/${UUID}/form-handover`],
      ['POST', `/actions/${UUID}/form-handover/approve/extra`], ['GET', `/actions/${UUID}/form-handover/approve`],
      ['POST', `/tasks/${UUID}/authenticated/form/preparation-mode/extra`],
      ['POST', `/tasks/${UUID}/authenticated/form/preparation-modes`],
      ['POST', `/tasks/${UUID}/authenticated/form/freeze`], ['POST', `/tasks/${UUID}/authenticated/form/thaw`],
      ['POST', `/tasks/${UUID}/authenticated/form/fill`], ['POST', `/tasks/${UUID}/authenticated/form/submit`],
      ['POST', `/tasks/${UUID}/authenticated/form/(stop|freeze)`]
    ]
    for (const [method, path] of rejected) expect(isAllowedRuntimeRoute(method as never, path), `${method} ${path}`).toBe(false)
  })

  it('still reaches no route that saves a value or a generic draft operation', () => {
    expect(isAllowedRuntimeRoute('PUT' as never, '/protected-values/email')).toBe(false)
    expect(isAllowedRuntimeRoute('POST', `/form-drafts/${UUID}/${'a'.repeat(30)}`)).toBe(false)
  })
})

describe('the wire (what the runtime may say about a draft)', () => {
  const examples = contract.examples as Record<string, unknown>
  it('parses every S6 example the runtime can emit', () => {
    for (const key of ['form_plan_preparing', 'form_plan_draft_prepared', 'form_plan_draft_partial', 'form_plan_handover_waiting', 'form_plan_handed_over']) {
      const parsed = parseFormPlan(examples[key])
      expect(parsed.taskId, key).toBeTruthy()
    }
    expect(parseFormPlan(examples.form_plan_draft_prepared).draft?.status).toBe('PREPARED')
    expect(parseFormPlan(examples.form_plan_draft_partial).draft?.partial).toBe(true)
    expect(parseFormPlan(examples.form_plan_handed_over).handover?.resultCode).toBe('handed_over')
  })

  it('carries ids, statuses and counts only -- never a value, a hash or an origin', () => {
    const serialised = JSON.stringify(parseFormPlan(examples.form_plan_handover_waiting))
    for (const forbidden of ['digest', 'hash', 'value', 'origin', 'fingerprint', 'selector', 'password'])
      expect(serialised.toLowerCase(), forbidden).not.toContain(forbidden)
  })

  it('refuses a draft with a status that does not exist, or a value smuggled in', () => {
    const base = examples.form_plan_draft_prepared as Record<string, Record<string, unknown>>
    expect(() => parseFormPlan({ ...base, draft: { ...base.draft, status: 'RESTORED' } })).toThrow()
    expect(() => parseFormPlan({ ...base, draft: { ...base.draft, value: 'x' } })).toThrow()
    expect(() => parseFormPlan({ ...base, draft: { ...base.draft, field_count: 40 } })).toThrow()
  })
})

const card = (over: Partial<AgentFormPlanView> = {}): AgentFormPlanView => ({
  taskId: UUID, site: 'jobs.example.test', savedDetails: [], formCount: 1, candidateElementCount: 5, preparing: true, ...over
})
const draft = (over: Record<string, unknown> = {}) => ({
  draftId: UUID, revision: 1, status: 'PREPARED' as const, fieldCount: 3, partial: false, site: 'jobs.example.test', ...over
})

describe('the trusted cards say what is true', () => {
  it('offers a preparation window before any planning, because a draft lives only in a visible window', () => {
    const model = describeFormPlan(card({ preparing: false }), true, false, NOW)
    expect(model.stage).toBe('prepare_offer')
    expect(model.controls).toEqual(['start_preparation'])
    expect(JSON.stringify(model)).toContain('Nothing is filled yet')
  })

  it('states the whole promise on the prepared card, in its own words', () => {
    const model = describeFormPlan(card({ draft: draft() }), true, false, NOW)
    expect(model.stage).toBe('draft')
    expect(model.eyebrow).toBe('FORM PREPARED LOCALLY')
    expect(model.title).toBe('Lumi filled and checked 3 fields')
    expect(model.controls).toEqual(['discard_draft', 'request_handover'])
    const text = model.lines.join(' ')
    for (const required of [
      'Nothing was sent while Lumi filled them.', 'Network access is still blocked for this browser page.',
      'Lumi has not submitted anything.', 'These values exist only in this browser window. If Lumi or your computer restarts, they are lost.'
    ]) expect(text).toContain(required)
  })

  it('says a stopped fill needs manual review, and offers only discard or hand-over', () => {
    const model = describeFormPlan(card({ draft: draft({ status: 'STALE', partial: true, fieldCount: 2 }) }), true, false, NOW)
    expect(model.eyebrow).toBe('FORM PARTLY PREPARED')
    expect(model.lines.join(' ')).toContain('this form needs behavior that is unavailable while the network is frozen')
    expect(model.lines.join(' ')).toContain('Nothing was sent')
    expect(model.controls).toEqual(['discard_draft', 'request_handover'])
  })

  it('warns, before the hand-over click, that the page can send once the network returns', () => {
    const handover = { actionId: UUID, revision: 2, actionStatus: 'WAITING_APPROVAL' as const, approvalStatus: 'PENDING' as const, draftId: UUID, fieldCount: 3, partial: false, site: 'jobs.example.test' }
    const model = describeFormPlan(card({ draft: draft(), handover }), true, false, NOW)
    expect(model.stage).toBe('handover')
    expect(model.controls).toEqual(['decline_handover', 'approve_handover'])
    const text = model.lines.join(' ')
    expect(text).toContain('Lumi filled these fields while the browser could not send anything.')
    expect(text).toContain('network access will resume')
    expect(text).toContain('The site may immediately autosave or otherwise receive what is in the form.')
    expect(text).toContain('Lumi will not submit the form.')
  })

  it('never offers Submit, Send or Finish, and never claims a receipt', () => {
    const stages: AgentFormPlanView[] = [
      card({ preparing: false }), card({ draft: draft() }), card({ draft: draft({ status: 'STALE', partial: true }) }),
      card({ draft: draft(), handover: { actionId: UUID, revision: 2, actionStatus: 'WAITING_APPROVAL', approvalStatus: 'PENDING', draftId: UUID, fieldCount: 3, partial: false, site: 'x.example' } }),
      card({ draft: draft({ status: 'HANDED_OVER', revision: 2 }), handover: { actionId: UUID, revision: 5, actionStatus: 'SUCCEEDED', approvalStatus: 'CONSUMED', draftId: UUID, fieldCount: 3, partial: false, site: 'x.example', resultCode: 'handed_over' } })
    ]
    for (const plan of stages) {
      const model = describeFormPlan(plan, true, false, NOW)
      const all = JSON.stringify(model)
      expect(all).not.toMatch(/submitted successfully|accepted it|application (was )?sent|finish application|site (has )?saved/i)
      expect(model.controls.join(' ')).not.toMatch(/submit|send|finish/)
    }
  })

  it('after a hand-over, says only what Lumi actually knows', () => {
    const handover = { actionId: UUID, revision: 5, actionStatus: 'SUCCEEDED' as const, approvalStatus: 'CONSUMED' as const, draftId: UUID, fieldCount: 3, partial: false, site: 'x.example', resultCode: 'handed_over' as const }
    const model = describeFormPlan(card({ draft: draft({ status: 'HANDED_OVER' }), handover }), true, false, NOW)
    expect(model.title).toBe('You took over in the browser window')
    expect(model.lines).toEqual(['Lumi did not submit anything and cannot tell you whether the site accepted or saved it.'])
    expect(model.controls).toEqual([])
  })

  it('refuses to offer a historical S5 plan for approval', () => {
    const model = describeFormPlan(card({ disclosure: { actionId: UUID, revision: 2, actionStatus: 'WAITING_APPROVAL', approvalStatus: 'PENDING', site: 'x.example', fields: [], revealsCountry: false, executable: false } }), true, false, NOW)
    expect(model.stage).toBe('superseded')
    expect(model.controls).toEqual(['decline_disclosure'])
  })
})

const source = (path: string): string => readFileSync(join(process.cwd(), path), 'utf8')

describe('what voice and typed text can reach (Milestone 8b S6)', () => {
  const S6_NAMES = ['startFormPreparationMode', 'stopFormPreparation', 'discardFormDraft', 'prepareFormHandover', 'approveFormHandover', 'rejectFormHandover', 'fillApprovedForm']
  it('names none of them on the voice backend, the voice controller or the conversation vocabulary', () => {
    const voice = source('src/main/services/voice-task-controller.ts')
    for (const name of S6_NAMES) expect(voice, name).not.toContain(name)
    for (const path of ['src/shared/voice-task-contracts.ts', 'src/shared/intent.ts'])
      expect(source(path), path).not.toMatch(/FormDraft|FormHandover|FormPreparation|form-drafts|handover/i)
  })

  it('has each of them only as a trusted IPC channel taking ids and a revision', () => {
    for (const name of S6_NAMES.filter((item) => item !== 'fillApprovedForm')) {
      expect(Object.keys(AGENT_IPC_CHANNELS)).toContain(name)
    }
    const preload = source('src/preload/index.ts')
    expect(preload).toMatch(/discardFormDraft: \(draftId: string, expectedRevision: number\)/)
    expect(preload).toMatch(/approveFormHandover: \(actionId: string, expectedRevision: number\)/)
    // No preload method takes a value, a manifest, an origin, a URL or a freeze flag.
    const bridge = preload.slice(preload.indexOf('startFormPreparationMode'), preload.indexOf('rejectFormHandover') + 200)
    expect(bridge).not.toMatch(/value|manifest|origin|url|selector|freeze|thaw|network/i)
  })
})

// ---- the controller, against the runtime fake ----------------------------------------------------

let directory: string
beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'lumi-form-draft-')) })
afterEach(async () => {
  setFormDraftWindowActive(false)
  clearTakeoverReconciliation()
  await rm(directory, { recursive: true, force: true })
})

class Provider implements ModelProvider {
  readonly capabilities = { json: true, vision: false, contextTokens: 32_000 }
  readonly calls: ModelRequest[] = []
  private readonly inner: ScriptedTextProvider
  constructor(readonly id: TextProviderId, readonly model: string) { this.inner = new ScriptedTextProvider(id, 'rules') }
  configured(): boolean { return true }
  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    return { ...(await this.inner.generate(request)), provider: this.id, model: this.model }
  }
}

function harness(runtime = new FakeFormRuntime({ preparing: false })) {
  const providers = [new Provider('gemini', 'gemini-2.5-flash'), new Provider('openai', 'gpt-5.6-luna')]
  const router = new ModelRouter((id) => providers.find((provider) => provider.id === id), DEFAULT_ROUTES, new DiagnosticsLog())
  const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, undefined, {
    planner: new AuthenticatedPlanner(router), answerer: new AuthenticatedAnswerer(router), formPlanner: new FormPlanner(router)
  })
  return { controller, runtime, providers }
}

async function planned(h: ReturnType<typeof harness>): Promise<AgentTaskSnapshot> {
  const created = await h.controller.createAuthenticatedTask('Help me apply for this job', PROFILE, 'gemini')
  if (!created.ok) throw new Error(created.error.message)
  const grant = created.value.authenticated!.grant!
  await h.controller.grantAuthenticatedScope(grant.grantId, grant.revision)
  const prep = await h.controller.startFormPreparationMode()
  if (!prep.ok) throw new Error(prep.error.message)
  const scoped = await h.controller.prepareFormPlanning(['legal_name', 'email', 'phone', 'country'])
  if (!scoped.ok) throw new Error(scoped.error.message)
  const formGrant = scoped.value.formPlan!.grant!
  await h.controller.grantFormPlanning(formGrant.grantId, formGrant.revision)
  const proposed = await h.controller.runFormPlanning()
  if (!proposed.ok) throw new Error(proposed.error.message)
  return proposed.value
}

describe('the controller (fake runtime)', () => {
  it('fills only after the trusted approval, keeps the network frozen, and hands over only on the second approval', async () => {
    const h = harness()
    const waiting = await planned(h)
    expect(waiting.formPlan?.preparing).toBe(true)
    expect(h.runtime.networkFrozen).toBe(true) // nothing has been lifted
    const disclosure = waiting.formPlan!.disclosure!
    expect(disclosure.executable).toBe(true)

    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    expect(filled.ok).toBe(true)
    if (!filled.ok) return
    const card = filled.value.formPlan!
    expect(card.disclosure?.resultCode).toBe('local_draft_prepared')
    expect(card.draft?.status).toBe('PREPARED')
    expect(h.runtime.networkFrozen).toBe(true) // stays frozen after the fill
    expect(h.runtime.networkRestores).toBe(0)

    // Asking for a handover changes nothing: it only opens the second approval.
    const requested = await h.controller.prepareFormHandover(card.draft!.draftId, card.draft!.revision)
    expect(requested.ok).toBe(true)
    if (!requested.ok) return
    expect(h.runtime.networkFrozen).toBe(true)
    const handover = requested.value.formPlan!.handover!
    const done = await h.controller.approveFormHandover(handover.actionId, handover.revision)
    expect(done.ok).toBe(true)
    if (!done.ok) return
    expect(done.value.formPlan?.handover?.resultCode).toBe('handed_over')
    expect(done.value.formPlan?.draft?.status).toBe('HANDED_OVER')
    expect(h.runtime.networkFrozen).toBe(false)
    expect(h.runtime.networkRestores).toBe(1) // exactly one, and only through the approval
    expect(h.runtime.pageMutations).toBe(0) // Lumi never submitted
  })

  it('discard destroys the draft and the network comes back only through it', async () => {
    const h = harness()
    const waiting = await planned(h)
    const disclosure = waiting.formPlan!.disclosure!
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    const draftCard = filled.value.formPlan!.draft!
    const result = await h.controller.discardFormDraft(draftCard.draftId, draftCard.revision)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.value.formPlan?.draft?.status).toBe('DISCARDED')
    expect(h.runtime.networkFrozen).toBe(false)
    expect(h.runtime.networkRestores).toBe(0) // a discard is not a handover
  })

  it('refuses a stale revision or another id before contacting the runtime', async () => {
    const h = harness()
    const disclosure = (await planned(h)).formPlan!.disclosure!
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    const draftCard = filled.value.formPlan!.draft!
    const before = h.runtime.calls.length
    const stale = await h.controller.discardFormDraft(draftCard.draftId, draftCard.revision + 4)
    expect(stale.ok).toBe(false)
    const other = await h.controller.prepareFormHandover('00000000-0000-4000-8000-0000000000ee', draftCard.revision)
    expect(other.ok).toBe(false)
    expect(h.runtime.calls.slice(before).filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(h.runtime.networkFrozen).toBe(true)
  })

  it('a page the human changed while frozen is refused at handover, and the network stays off', async () => {
    const h = harness()
    const disclosure = (await planned(h)).formPlan!.disclosure!
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    const draftCard = filled.value.formPlan!.draft!
    h.runtime.draftChanged = true // the human typed into the visible window
    const requested = await h.controller.prepareFormHandover(draftCard.draftId, draftCard.revision)
    if (!requested.ok) throw new Error(requested.error.message)
    const handover = requested.value.formPlan!.handover!
    const refused = await h.controller.approveFormHandover(handover.actionId, handover.revision)
    expect(refused.ok).toBe(true)
    if (!refused.ok) return
    expect(refused.value.formPlan?.handover?.resultCode).toBe('handover_refused')
    expect(h.runtime.networkFrozen).toBe(true)
    expect(h.runtime.networkRestores).toBe(0)
    // The old approval cannot be replayed.
    expect((await h.controller.approveFormHandover(handover.actionId, handover.revision + 3)).ok).toBe(false)
  })

  it('refuses planning approval outside preparation mode, and never sends a raw value to anyone', async () => {
    const h = harness()
    const created = await h.controller.createAuthenticatedTask('Help me apply for this job', PROFILE, 'gemini')
    if (!created.ok) throw new Error(created.error.message)
    const grant = created.value.authenticated!.grant!
    await h.controller.grantAuthenticatedScope(grant.grantId, grant.revision)
    const scoped = await h.controller.prepareFormPlanning(['email'])
    if (!scoped.ok) throw new Error(scoped.error.message)
    const formGrant = scoped.value.formPlan!.grant!
    await h.controller.grantFormPlanning(formGrant.grantId, formGrant.revision)
    const refused = await h.controller.runFormPlanning()
    expect(refused.ok).toBe(false) // preparation_mode_required: an S5-era, headless plan is not executable
    const everything = JSON.stringify(h.runtime.calls)
    for (const marker of RAW_MARKERS) expect(everything).not.toContain(marker)
  })

  it('refuses screen capture (which goes to a model) while a preparation window or a draft exists', async () => {
    setTakeoverActive(false)
    setFormDraftWindowActive(false)
    expect(isCaptureBlocked()).toBe(false) // a reconciled, empty takeover state permits capture
    const h = harness()
    const disclosure = (await planned(h)).formPlan!.disclosure!
    expect(isCaptureBlocked()).toBe(true) // the preparation window is open
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    expect(isCaptureBlocked()).toBe(true) // a dirty draft exists
  })

  it('calls no provider after the first write, and no provider ever sees a raw value', async () => {
    const h = harness()
    const disclosure = (await planned(h)).formPlan!.disclosure!
    const calls = (): number => h.providers.reduce((total, provider) => total + provider.calls.length, 0)
    const beforeFill = calls()
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    const draftCard = filled.value.formPlan!.draft!
    const requested = await h.controller.prepareFormHandover(draftCard.draftId, draftCard.revision)
    if (!requested.ok) throw new Error(requested.error.message)
    const handover = requested.value.formPlan!.handover!
    await h.controller.approveFormHandover(handover.actionId, handover.revision)
    // From the fill until the hand-over completed: zero provider calls.
    expect(calls()).toBe(beforeFill)
    const seen = h.providers.map((provider) => provider.calls.map((call) => call.system + ' ' + call.input).join(' ')).join(' ')
    for (const marker of RAW_MARKERS) expect(seen).not.toContain(marker)
  })

  it('sends an id and a revision, and nothing else, on every S6 call', async () => {
    const h = harness()
    const disclosure = (await planned(h)).formPlan!.disclosure!
    const filled = await h.controller.approveFieldDisclosure(disclosure.actionId, disclosure.revision)
    if (!filled.ok) throw new Error(filled.error.message)
    const draftCard = filled.value.formPlan!.draft!
    await h.controller.prepareFormHandover(draftCard.draftId, draftCard.revision)
    for (const call of h.runtime.calls.filter((entry) => /form-drafts|form-handover|preparation-mode/.test(entry.path))) {
      expect(Object.keys((call.body ?? {}) as object).every((key) => key === 'expected_revision')).toBe(true)
    }
  })
})
