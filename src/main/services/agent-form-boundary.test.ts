import { readFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS, PROTECTED_DATA_KINDS, type AgentFormPlanView } from '../../shared/agent-contracts'
import { registerAgentIpc, type AgentIpcDependencies } from './agent-ipc'
import { FakeFormRuntime } from '../testing/fake-form-runtime'
import { ActiveTaskStore, AgentTaskController } from './agent-tasks'
import { WireError, parseFormPlan, parsePlanningContext, projectRuntimeError } from './agent-wire'
import { describeDisclosureCard, describeFormPlan, describeEvent } from '../../renderer/src/agent-task-view'
import { buildContext } from '../models/context-builder'
import { AgentMemoryStore } from '../agent/agent-memory'
import { mkdtemp } from 'node:fs/promises'
import { ModelRouter, DEFAULT_ROUTES } from '../models/model-router'
import { ScriptedTextProvider } from '../models/scripted-provider'
import { AuthenticatedPlanner } from '../agent/authenticated-planner'
import { AuthenticatedAnswerer } from '../agent/authenticated-answer'
import { FormPlanner } from '../agent/form-planner'

/**
 * The form-planning boundary (Milestone 8b S5), checked structurally: what the
 * renderer can send, what voice can reach, what the wire refuses, and what the
 * trusted cards say. These fail if a convenient shortcut appears -- a manifest in
 * an IPC payload, a voice-side approval, a renderer that masks or holds a value,
 * a button that says "Fill".
 */

const ROOT = join(__dirname, '..', '..', '..')
const read = (relative: string): string => readFileSync(join(ROOT, relative), 'utf8')
const FORM_NAMES = [
  'prepareFormPlanning', 'grantFormPlanning', 'declineFormPlanning', 'runFormPlanning',
  'approveFieldDisclosure', 'rejectFieldDisclosure'
] as const

describe('what the renderer can send', () => {
  const contracts = read('src/shared/agent-contracts.ts')
  const start = contracts.indexOf('prepareFormPlanning: (')
  const end = contracts.indexOf('listPreferences: () =>')
  const api = contracts.slice(start, end).replace(/\/\*[\s\S]*?\*\//g, '')

  it('offers exactly six channels', () => {
    const names = Object.keys(AGENT_IPC_CHANNELS).filter((name) => /FormPlanning|FieldDisclosure/.test(name)).sort()
    expect(names).toEqual([...FORM_NAMES].sort())
  })

  it('takes only closed ids and revisions -- never a manifest, value, origin, field, selector or provider', () => {
    const parameters = [...api.matchAll(/(?:\w*FormPlanning|\w*FieldDisclosure): \(([^)]*)\) =>/g)]
      .flatMap((match) => match[1].split(',').map((part) => part.trim().split(':')[0]).filter(Boolean))
    expect(new Set(parameters)).toEqual(new Set(['allowedDataRefs', 'grantId', 'expectedRevision', 'actionId']))
    expect(api).toContain('approveFieldDisclosure: (actionId: string, expectedRevision: number)')
    expect(api).toContain('rejectFieldDisclosure: (actionId: string, expectedRevision: number)')
    expect(api).toContain('prepareFormPlanning: (allowedDataRefs: AgentProtectedDataKind[])')
    for (const forbidden of ['manifest', 'value', 'origin', 'field:', 'selector', 'provider', 'url', 'digest']) {
      expect(api.toLowerCase()).not.toContain(forbidden)
    }
  })

  it('has a preload that forwards exactly those primitives', () => {
    const preload = read('src/preload/index.ts')
    const block = preload.slice(preload.indexOf('prepareFormPlanning'), preload.indexOf('listPreferences'))
    for (const name of FORM_NAMES) expect(block).toContain(`AGENT_IPC_CHANNELS.${name}`)
    const parameters = [...block.matchAll(/(?:\w*FormPlanning|\w*FieldDisclosure): \(([^)]*)\) =>/g)]
      .flatMap((match) => match[1].split(',').map((part) => part.trim().split(':')[0]).filter(Boolean))
    expect(new Set(parameters)).toEqual(new Set(['allowedDataRefs', 'grantId', 'expectedRevision', 'actionId']))
    expect(block).not.toMatch(/ipcRenderer\.send\(/)
  })

  it('checks the sender on every channel', () => {
    const handlers = new Map<string, (event: never, ...args: unknown[]) => unknown>()
    let checked = 0
    const controller = new AgentTaskController(new FakeFormRuntime(), new ActiveTaskStore(mkdtempSync(join(tmpdir(), 'lumi-form-ipc-'))))
    registerAgentIpc({
      ipcMain: { handle: (channel, listener) => { handlers.set(channel, listener) } },
      assertTrustedSender: () => { checked += 1 },
      controller,
      voice: { handle: async () => ({ ok: false, error: { code: 'request_failed', message: 'x' } }) } as unknown as AgentIpcDependencies['voice'],
      runtimeStatus: () => ({ state: 'running' }),
      restartRuntime: async () => ({ state: 'running' })
    } as AgentIpcDependencies)
    for (const name of FORM_NAMES) {
      const handler = handlers.get(AGENT_IPC_CHANNELS[name])
      expect(handler, name).toBeDefined()
      const before = checked
      void handler!({} as never)
      expect(checked, name).toBe(before + 1)
    }
  })

  it('has no generic channel that could carry a command', () => {
    expect(Object.keys(AGENT_IPC_CHANNELS).filter((name) => /^(execute|command|dispatch|invoke|run|send)$/i.test(name))).toEqual([])
    const preload = read('src/preload/index.ts')
    const block = preload.slice(preload.indexOf('prepareFormPlanning'), preload.indexOf('listPreferences'))
    expect(block).not.toMatch(/ipcRenderer\.(send|sendSync|postMessage)\(/)
  })
})

describe('what voice and typed text can reach', () => {
  const source = read('src/main/services/voice-task-controller.ts')
  const backend = source.slice(source.indexOf('export type VoiceTaskBackend'), source.indexOf('const TURN_ID'))

  it('has no form-planning or disclosure method on the voice backend', () => {
    for (const name of FORM_NAMES) expect(backend, name).not.toContain(name)
    expect(backend).not.toMatch(/FormPlanning|FieldDisclosure|prepare_form|form_prepare/)
  })

  it('never names any of them anywhere in the voice controller', () => {
    for (const name of FORM_NAMES) expect(source, name).not.toContain(name)
    expect(source).not.toMatch(/FormPlanning|FieldDisclosure|disclosure_approval|approve_disclosure/)
  })

  it('exposes none of them to the conversation tool vocabulary', () => {
    for (const path of ['src/shared/voice-task-contracts.ts', 'src/shared/intent.ts']) {
      const contract = read(path)
      expect(contract, path).not.toMatch(/FormPlanning|FieldDisclosure|prepare_form|form_prepare|disclosure/i)
    }
  })

  it('cannot reach the controller methods from an IPC voice or chat channel', () => {
    const ipc = read('src/main/services/agent-ipc.ts')
    const voiceHandler = ipc.slice(ipc.indexOf('voiceTask'), ipc.indexOf('voiceTask') + 400)
    for (const name of FORM_NAMES) expect(voiceHandler, name).not.toContain(name)
  })
})

describe('what the wire refuses', () => {
  const plan = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
    task_id: '00000000-0000-4000-8000-00000000000a', task_status: 'READY', objective: 'x', site: 'jobs.example.test',
    saved_details: [{ data_ref: 'email', kind: 'email', preview: 'E***@e***.test', updated_at: '2026-09-20T10:00:00+00:00' }],
    grant: null, disclosure: null, form_count: 1, candidate_element_count: 3, max_fields: 12, ...overrides
  })

  it('accepts what the runtime emits (its own contract examples)', () => {
    const contract = JSON.parse(read('src/shared/agent-runtime-contract.json')) as { examples: Record<string, unknown> }
    for (const key of ['form_plan_none', 'form_plan_pending', 'form_plan_active', 'form_plan_waiting_approval', 'form_plan_prepared_nothing']) {
      const parsed = parseFormPlan(contract.examples[key])
      expect(parsed.savedDetails.length, key).toBeGreaterThan(0)
    }
    expect(parseFormPlan(contract.examples.form_plan_prepared_nothing).disclosure?.resultCode).toBe('prepared_nothing')
    expect(parseFormPlan(contract.examples.form_plan_pending).grant?.status).toBe('PENDING')
    const context = parsePlanningContext(contract.examples.planning_context)
    expect(context.forms[0].elements[0].accessibleName).toBe('Email address')
  })

  it.each([
    'value', 'value_digest', 'manifest_digest', 'element_identity_hash', 'account_fingerprint', 'recipient_origin', 'origin', 'selector'
  ])('refuses a plan payload carrying %s anywhere', (key) => {
    const contract = JSON.parse(read('src/shared/agent-runtime-contract.json')) as { examples: Record<string, Record<string, unknown>> }
    const waiting = structuredClone(contract.examples.form_plan_waiting_approval) as { disclosure: { fields: Array<Record<string, unknown>> } }
    waiting.disclosure.fields[0][key] = 'x'
    expect(() => parseFormPlan(waiting)).toThrow(WireError)
    expect(() => parseFormPlan(plan({ [key]: 'x' }))).toThrow(WireError)
  })

  it('refuses an unknown kind, a ninth data ref and a result code that is not prepared_nothing', () => {
    expect(() => parseFormPlan(plan({ saved_details: [{ data_ref: 'password', kind: 'password', preview: 'x', updated_at: '2026-09-20T10:00:00+00:00' }] }))).toThrow(WireError)
    expect(() => parseFormPlan(plan({ saved_details: Array.from({ length: 9 }, () => ({ data_ref: 'email', kind: 'email', preview: 'x', updated_at: '2026-09-20T10:00:00+00:00' })) }))).toThrow(WireError)
    expect(PROTECTED_DATA_KINDS).toHaveLength(8)
  })

  it('maps the stable refusal codes to plain, truthful messages', () => {
    const changed = projectRuntimeError(409, { error: { code: 'form_prepare_state_changed', message: 'x', reason: 'protected_value_changed' } })
    expect(changed.code).toBe('form_plan_stale')
    expect(changed.message).toContain('saved detail changed')
    expect(changed.message).toContain('Nothing was changed')
    const refused = projectRuntimeError(422, { error: { code: 'form_prepare_refused', message: 'x', reason: 'data_ref_not_allowed' } })
    expect(refused.code).toBe('form_plan_refused')
    const unknown = projectRuntimeError(422, { error: { code: 'form_prepare_refused', message: 'x', reason: 'something_new' } })
    expect(unknown.message).toBe('Lumi refused that form plan. Nothing was changed.')
  })
})

describe('the trusted cards', () => {
  const NOW = Date.parse('2026-09-20T10:00:00Z')
  const base: AgentFormPlanView = {
    taskId: '00000000-0000-4000-8000-00000000000a', site: 'jobs.example.test',
    savedDetails: [
      { dataRef: 'email', kind: 'email', preview: 'E***@e***.test', updatedAt: '2026-09-20T10:00:00+00:00' },
      { dataRef: 'country', kind: 'country', preview: 'India', updatedAt: '2026-09-20T10:00:00+00:00' }
    ],
    formCount: 1, candidateElementCount: 5
  }
  const grant = (status: 'PENDING' | 'ACTIVE') => ({
    grantId: '00000000-0000-4000-8000-0000000000aa', status, revision: 1, allowedDataRefs: ['email', 'country'] as never,
    planningRecipient: 'openai' as const, maxFields: 12, ...(status === 'ACTIVE' ? { expiresAt: '2099-01-01T00:00:00+00:00' } : {})
  })
  const disclosure = (over: Record<string, unknown> = {}) => ({
    actionId: '00000000-0000-4000-8000-0000000000bb', revision: 2, actionStatus: 'WAITING_APPROVAL' as const,
    approvalStatus: 'PENDING' as const, site: 'jobs.example.test', formLabel: 'Application', revealsCountry: false,
    fields: [
      { kind: 'saved_detail' as const, fieldLabel: 'Email address', controlType: 'email', dataRef: 'email' as const, preview: 'E***@e***.test' },
      { kind: 'option' as const, fieldLabel: 'Country', controlType: 'select_single', optionLabel: 'India' },
      { kind: 'checkbox' as const, fieldLabel: 'I agree to the terms', controlType: 'checkbox', checked: true }
    ], ...over
  })

  it('offers saved details first, then the planning permission with an honest disclosure', () => {
    const offer = describeFormPlan(base, true, false, NOW)
    expect(offer.stage).toBe('offer')
    expect(offer.controls).toEqual(['plan_form'])
    expect(offer.offerable.map((item) => item.preview)).toEqual(['E***@e***.test', 'India'])

    const permission = describeFormPlan({ ...base, grant: grant('PENDING') }, true, false, NOW)
    expect(permission.stage).toBe('permission')
    expect(permission.eyebrow).toBe('PLAN FORM PREPARATION')
    expect(permission.controls).toEqual(['decline_form_planning', 'allow_form_planning'])
    expect(permission.permission?.provider).toBe('OpenAI')
    expect(permission.permission?.notSent).toEqual(['Raw saved values will NOT be sent to the AI.'])
    expect(permission.permission?.sent.join(' ')).toContain('masked previews')
    expect(permission.permission?.cannotAct).toBe('The AI cannot type into the form or submit it.')
    // A country cannot be masked, and the card says so rather than claiming otherwise.
    expect(permission.permission?.countryNotice).toContain('sent as written')
    const withoutCountry = describeFormPlan({ ...base, grant: { ...grant('PENDING'), allowedDataRefs: ['email'] } }, true, false, NOW)
    expect(withoutCountry.permission?.countryNotice).toBeUndefined()
  })

  it('shows the exact manifest with truthful wording -- never "Fill"', () => {
    const view = { ...base, grant: grant('ACTIVE'), disclosure: disclosure() }
    const model = describeFormPlan(view, true, false, NOW)
    expect(model.stage).toBe('approval')
    expect(model.eyebrow).toBe('PREPARE THIS FORM')
    expect(model.controls).toEqual(['approve_disclosure', 'decline_disclosure'])
    const text = JSON.stringify(model)
    expect(text).toContain('Lumi cannot submit this form.')
    expect(text).toContain('does not change the page')
    expect(text).not.toMatch(/\bfill\b/i)
    const card = describeDisclosureCard(view.disclosure)
    expect(card.site).toBe('jobs.example.test')
    expect(card.formLabel).toBe('Application')
    expect(card.rows).toEqual([
      { savedLabel: 'Saved email', detail: 'E***@e***.test', fieldLabel: 'Email address' },
      { savedLabel: 'Option', detail: 'India', fieldLabel: 'Country' },
      { savedLabel: 'Checked', detail: 'tick', fieldLabel: 'I agree to the terms' }
    ])
  })

  it('says nothing was changed once the approval is spent', () => {
    const model = describeFormPlan({ ...base, grant: grant('ACTIVE'), disclosure: disclosure({ actionStatus: 'SUCCEEDED', approvalStatus: 'CONSUMED', resultCode: 'prepared_nothing' }) }, true, false, NOW)
    expect(model.stage).toBe('prepared')
    expect(model.controls).toEqual([])
    expect(model.title).toContain('nothing was changed')
    expect(model.lines.join(' ')).toContain('cannot be used again')
  })

  it('offers no control on a closed task or an expired grant', () => {
    expect(describeFormPlan({ ...base, grant: grant('PENDING') }, true, true, NOW).controls).toEqual([])
    expect(describeFormPlan({ ...base, grant: grant('ACTIVE'), disclosure: disclosure() }, true, true, NOW).controls).toEqual([])
    const expired = { ...base, grant: { ...grant('ACTIVE'), expiresAt: '2026-09-20T09:00:00+00:00' } }
    expect(describeFormPlan(expired, false, false, NOW).controls).toEqual([])
  })

  it('words the timeline for the disclosure action, not as a booking', () => {
    const event = (type: string, over: Record<string, unknown> = {}) => ({
      sequence: 1, type, taskRevision: 1, createdAt: '2026-09-20T10:00:00+00:00', toolName: 'prepare_form', ...over
    }) as never
    expect(describeEvent(event('action.proposed'))).toBe('Form plan prepared — nothing has been changed')
    expect(describeEvent(event('action.approved'))).toBe('You approved this form plan')
    expect(describeEvent(event('action.succeeded'))).toBe('Form plan approved — nothing was prepared in the page')
    expect(describeEvent(event('action.rejected', { reason: 'user_declined' }))).toBe('You declined this form plan')
    for (const type of ['action.proposed', 'action.approved', 'action.execution_started', 'action.succeeded']) {
      expect(describeEvent(event(type))).not.toMatch(/book/i)
    }
  })

  it('never masks and never holds a raw value in the renderer', () => {
    for (const path of ['src/renderer/src/agent-task-view.ts', 'src/renderer/src/components/AgentTaskPanel.tsx']) {
      const source = read(path)
      const section = source.slice(source.indexOf('Milestone 8b S5'))
      expect(section, path).not.toMatch(/\.replace\(\s*\/[^/]*\/[a-z]*,\s*'\*+'/)
      expect(section, path).not.toMatch(/'\*\*\*'|"\*\*\*"|padEnd|repeat\(/)
      expect(section, path).not.toMatch(/rawValue|savedValue|value_digest|manifestDigest|accountFingerprint/)
    }
  })
})

describe('the classification firewall', () => {
  it('never puts a form plan, a masked preview or a manifest into another request context or memory', async () => {
    const runtime = new FakeFormRuntime()
    const directory = mkdtempSync(join(tmpdir(), 'lumi-form-fw-'))
    const router = new ModelRouter((id) => (id === 'gemini' ? Object.assign(new ScriptedTextProvider('gemini', 'rules'), { model: 'gemini-2.5-flash' }) : undefined), DEFAULT_ROUTES)
    const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, undefined, {
      planner: new AuthenticatedPlanner(router), answerer: new AuthenticatedAnswerer(router), formPlanner: new FormPlanner(router)
    })
    const created = await controller.createAuthenticatedTask('Help me apply', '00000000-0000-4000-8000-0000000000cc', 'gemini')
    expect(created.ok ? 'ok' : created.error.message).toBe('ok')
    const snapshot = created.ok ? created.value : undefined
    expect(snapshot?.formPlan?.savedDetails.length).toBeGreaterThan(0)
    const built = buildContext({ rules: 'rules', utterance: 'what is on my screen', task: snapshot! }, { maxInputTokens: 8_000 })
    const seen = [built.system, built.input].join(' ')
    for (const forbidden of ['E***@e***.test', 'saved legal name', 'ending 1234', 'Full legal name', 'prepare_form', 'jobs.example.test']) {
      expect(seen).not.toContain(forbidden)
    }
    expect(built.sections.find((section) => section.name === 'task_state')?.included).toBe(false)
    // Episodic memory refuses account-private material outright.
    const memory = new AgentMemoryStore(await mkdtemp(join(tmpdir(), 'lumi-form-mem-')))
    await memory.recordEpisode({
      taskId: snapshot!.task.taskId, kind: 'authenticated_read', summary: 'E***@e***.test', sequence: 1, classification: 'account_private'
    } as never)
    expect(JSON.stringify(await memory.episodes())).not.toContain('E***@e***.test')
  })
})
