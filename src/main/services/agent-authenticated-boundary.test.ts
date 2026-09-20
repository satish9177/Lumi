import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS } from '../../shared/agent-contracts'
import { registerAgentIpc, type AgentIpcDependencies } from './agent-ipc'
import { FakeAuthenticatedRuntime } from '../testing/fake-authenticated-runtime'
import { ActiveTaskStore, AgentTaskController } from './agent-tasks'
import { authenticatedObservationLines } from '../agent/authenticated-planner'
import {
  WireError, parseAuthenticated, parseAuthenticatedStep, parseTask, projectRuntimeError
} from './agent-wire'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

/**
 * The account-reading boundary, checked structurally: what the renderer can
 * send, what voice can reach, and what the wire refuses. These are the tests
 * that would fail if a convenient shortcut appeared -- a URL parameter, a
 * provider-name string, a voice-side grant.
 */

const ROOT = join(__dirname, '..', '..', '..')
const read = (relative: string): string => readFileSync(join(ROOT, relative), 'utf8')

describe('what the renderer can send', () => {
  const contracts = read('src/shared/agent-contracts.ts')
  const start = contracts.indexOf('getAuthenticatedOptions: () =>')
  const end = contracts.indexOf('listPreferences: () =>')
  const api = contracts.slice(start, end)

  it('offers exactly six channels for account reading', () => {
    const names = Object.keys(AGENT_IPC_CHANNELS).filter((name) => /Authenticated/.test(name)).sort()
    expect(names).toEqual([
      'createAuthenticatedTask', 'declineAuthenticatedScope', 'getAuthenticatedOptions',
      'grantAuthenticatedScope', 'runAuthenticated', 'stopAuthenticated'
    ])
  })

  it('takes only opaque ids, revisions and the question -- never an address, selector, cookie, path or scope', () => {
    expect(api).toContain('createAuthenticatedTask: (objective: string, profileId: string, recipientId: string)')
    expect(api).toContain('grantAuthenticatedScope: (grantId: string, expectedRevision: number)')
    expect(api).toContain('declineAuthenticatedScope: (grantId: string, expectedRevision: number)')
    expect(api).toContain('runAuthenticated: () =>')
    expect(api).toContain('stopAuthenticated: () =>')
    // Every parameter of every account-reading method, by name. Comments say what
    // these do *not* take, so they are stripped first.
    const code = api.replace(/\/\*[\s\S]*?\*\//g, '')
    const parameters = [...code.matchAll(/\w+Authenticated\w*: \(([^)]*)\) =>|\w*Authenticated\w*: \(([^)]*)\) =>/g)]
      .flatMap((match) => (match[1] ?? match[2] ?? '').split(',').map((part) => part.trim().split(':')[0]).filter(Boolean))
    expect(new Set(parameters)).toEqual(new Set(['objective', 'profileId', 'recipientId', 'grantId', 'expectedRevision']))
  })

  it('has a preload that forwards exactly those primitives', () => {
    const preload = read('src/preload/index.ts')
    const block = preload.slice(preload.indexOf('getAuthenticatedOptions'), preload.indexOf('listPreferences'))
    expect(block).toContain('createAuthenticatedTask: (objective: string, profileId: string, recipientId: string) =>')
    expect(block).toContain('ipcRenderer.invoke(AGENT_IPC_CHANNELS.createAuthenticatedTask, objective, profileId, recipientId)')
    expect(block).toContain('ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantAuthenticatedScope, grantId, expectedRevision)')
    const parameters = [...block.matchAll(/\w*Authenticated\w*: \(([^)]*)\) =>/g)]
      .flatMap((match) => match[1].split(',').map((part) => part.trim().split(':')[0]).filter(Boolean))
    expect(new Set(parameters)).toEqual(new Set(['objective', 'profileId', 'recipientId', 'grantId', 'expectedRevision']))
  })

  it('checks the sender on every authenticated channel', () => {
    const handlers = new Map<string, (event: never, ...args: unknown[]) => unknown>()
    let checked = 0
    const runtime = new FakeAuthenticatedRuntime()
    const controller = new AgentTaskController(runtime, new ActiveTaskStore(mkdtempSync(join(tmpdir(), 'lumi-ipc-'))))
    registerAgentIpc({
      ipcMain: { handle: (channel, listener) => { handlers.set(channel, listener) } },
      assertTrustedSender: () => { checked += 1 },
      controller,
      voice: { handle: async () => ({ ok: false, error: { code: 'request_failed', message: 'x' } }) } as unknown as AgentIpcDependencies['voice'],
      runtimeStatus: () => ({ state: 'running' }),
      restartRuntime: async () => ({ state: 'running' })
    } as AgentIpcDependencies)
    for (const name of ['createAuthenticatedTask', 'grantAuthenticatedScope', 'declineAuthenticatedScope', 'runAuthenticated', 'stopAuthenticated', 'getAuthenticatedOptions'] as const) {
      const handler = handlers.get(AGENT_IPC_CHANNELS[name])
      expect(handler, name).toBeDefined()
      const before = checked
      void handler!({} as never)
      expect(checked, name).toBe(before + 1)
    }
  })
})

describe('what voice can reach', () => {
  const source = read('src/main/services/voice-task-controller.ts')
  const backend = source.slice(source.indexOf('export type VoiceTaskBackend'), source.indexOf('const TURN_ID'))

  it('has no grant, run, stop, create or option method for account reading', () => {
    expect(backend).toContain("'createClinicInfoTask'")
    for (const name of ['grantAuthenticatedScope', 'declineAuthenticatedScope', 'createAuthenticatedTask', 'runAuthenticated', 'stopAuthenticated', 'getAuthenticatedOptions']) {
      expect(backend, name).not.toContain(name)
    }
    expect(backend).not.toMatch(/uthenticated/)
  })

  it('never names a grant method anywhere in the voice controller', () => {
    for (const name of ['grantAuthenticatedScope', 'declineAuthenticatedScope', 'createAuthenticatedTask', 'runAuthenticated']) {
      expect(source, name).not.toContain(name)
    }
  })

  it('records account-private tasks as such, so memory can refuse them', () => {
    expect(source).toContain("classification: snapshot.task.kind === 'authenticated_read' ? 'account_private' : 'public'")
  })
})

describe('what the wire refuses', () => {
  function viewFrom(runtime: FakeAuthenticatedRuntime): Record<string, unknown> {
    const task = [...runtime.tasks.values()][0]
    return (runtime as unknown as { view(id: string): Record<string, unknown> }).view(task.id)
  }

  async function prepared(): Promise<Record<string, unknown>> {
    const runtime = new FakeAuthenticatedRuntime()
    const created = await runtime.request('POST', '/tasks', {
      request: { type: 'authenticated_read', classification: 'account_private', objective: 'Which of my repositories are private?', profile_id: '00000000-0000-4000-8000-0000000000cc' }
    })
    const task = created.body as { id: string }
    await runtime.request('POST', `/tasks/${task.id}/authenticated/prepare`, { recipient: 'gemini' })
    await runtime.request('POST', `/tasks/${task.id}/authenticated/grant`, {
      grant_id: [...runtime.grants.keys()][0], expected_revision: 1
    })
    await runtime.request('POST', `/tasks/${task.id}/authenticated/steps`, {
      request_id: 'req_00000001', step: { operation: 'observe', tab: 't1' }, planner_calls: 1
    })
    return JSON.parse(JSON.stringify(viewFrom(runtime))) as Record<string, unknown>
  }

  it('accepts what the runtime emits', async () => {
    const parsed = parseAuthenticated(await prepared())
    expect(parsed.view.classification).toBe('account_private')
    expect(parsed.view.grant?.scope.budgets.maxVisionCalls).toBe(0)
    expect(parsed.view.grant?.scope.recipient).toBe('gemini')
    expect(parsed.view.observations).toHaveLength(1)
  })

  it('S4: a form inventory in a runtime observation never reaches the view or a provider prompt', async () => {
    const MARKER = 'FORM_FIELD_SECRET_MARKER_0AD'
    const view = await prepared() as Record<string, any>
    // What a v2 stored observation holds locally. S4 does not send it on the wire,
    // and a wire that ever did must still not become provider text.
    view.observations[0].form_epoch = 3
    view.observations[0].inventory = {
      forms: [{ ref: 'f1', label: MARKER }],
      frames: [{ ref: 'fr0' }],
      elements: [{ element_ref: 'e1', form_ref: 'f1', frame_ref: 'fr0', role: 'textbox', control_type: 'text',
        accessible_name: MARKER, value_state: 'filled', required: true, enabled: true, visible: true,
        read_only: false, max_length: null, option_refs: [{ ref: 'op1', label: MARKER }], submit_like: false }],
      truncated: false
    }
    const parsed = parseAuthenticated(view)
    const prompt = authenticatedObservationLines(parsed.view).join('\n')
    expect(prompt).not.toContain(MARKER)
    expect(prompt).not.toContain('accessible_name')
    expect(JSON.stringify(parsed.view)).not.toContain(MARKER)
    expect(JSON.stringify(parsed.view)).not.toContain('inventory')
  })

  it.each([
    ['a link that carries an address', (view: Record<string, any>) => { view.observations[0].links[0].url = 'https://evil.example/?token=x' }],
    ['a link that carries an href', (view: Record<string, any>) => { view.observations[0].links[0].href = '/x' }],
    ['an observation not classified account_private', (view: Record<string, any>) => { view.observations[0].classification = 'public' }],
    ['an observation without its untrusted provenance', (view: Record<string, any>) => { view.observations[0].provenance = 'trusted' }],
    ['a scope that allows a vision call', (view: Record<string, any>) => { view.grant.scope.budgets.max_vision_calls = 1 }],
    ['a scope that hides the website side effects', (view: Record<string, any>) => { view.grant.scope.website_side_effects_possible = false }],
    ['a scope with failover', (view: Record<string, any>) => { view.grant.scope.disclosure.failover = 'any' }],
    ['a scope without reduced identifiers', (view: Record<string, any>) => { view.grant.scope.disclosure.identifiers_reduced = false }],
    ['a scope with a second recipient', (view: Record<string, any>) => { view.grant.scope.disclosure.recipient = ['gemini', 'openai'] }],
    ['a scope naming an unknown provider', (view: Record<string, any>) => { view.grant.scope.disclosure.recipient = 'https://evil.example' }],
    ['a scope with an unknown field', (view: Record<string, any>) => { view.grant.scope.selector = 'a' }],
    ['a scope with a non-read method', (view: Record<string, any>) => { view.grant.scope.methods = ['GET', 'POST'] }],
    ['a scope with an unknown operation', (view: Record<string, any>) => { view.grant.scope.allowed_operations = ['click'] }],
    ['an unclassified view', (view: Record<string, any>) => { view.classification = 'public' }],
    ['an unknown pause reason', (view: Record<string, any>) => { view.pause_reason = 'because' }],
    ['an observation of another task', (view: Record<string, any>) => { view.observations[0].task_id = '00000000-0000-4000-8000-0000000000ff' }],
    ['unsequenced blocks', (view: Record<string, any>) => { view.observations[0].blocks[0].id = 'b9' }]
  ])('rejects %s', async (_name, mutate) => {
    const view = await prepared()
    mutate(view as Record<string, any>)
    expect(() => parseAuthenticated(view)).toThrow(WireError)
  })

  it('refuses a task that is not classified account_private', () => {
    expect(() => parseTask({
      id: '00000000-0000-4000-8000-0000000000aa', status: 'CREATED', revision: 1, last_event_sequence: 1,
      request: { type: 'authenticated_read', objective: 'x', profile_id: '00000000-0000-4000-8000-0000000000cc' },
      created_at: '2026-09-20T10:00:00+00:00', updated_at: '2026-09-20T10:00:00+00:00'
    })).toThrow(WireError)
  })

  it('refuses a step reply for a research tool', async () => {
    const view = await prepared()
    expect(() => parseAuthenticatedStep({
      authenticated: view, action: { tool_name: 'research_observe' }, observation: null,
      outcome: 'SUCCEEDED', error_code: null, pause_reason: null, replayed: false
    })).toThrow(WireError)
  })

  it('projects each profile refusal to its own deterministic, actionable message', () => {
    const message = (reason: string): string =>
      projectRuntimeError(409, { error: { code: 'authenticated_profile_unavailable', message: 'x', reason } }).message
    expect(message('profile_not_authenticated')).toContain('Sign in manually')
    expect(message('login_required')).toContain('Nothing was sent to an AI')
    expect(message('account_fingerprint_unknown')).toContain('will not read it')
    expect(message('account_changed')).toContain('review a new permission card')
    expect(message('profile_takeover_active')).toContain('sign-in window')
    expect(message('profile_deleted')).toContain('deleted')
    expect(projectRuntimeError(409, { error: { code: 'authenticated_profile_unavailable', message: 'x', reason: 'unmapped' } }).code)
      .toBe('authenticated_unavailable')
  })
})
