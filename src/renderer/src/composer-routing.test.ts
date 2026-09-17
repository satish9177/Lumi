import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  Notification: { isSupported: () => false },
  nativeImage: { createFromPath: () => undefined },
  shell: { openExternal: vi.fn(async () => undefined), openPath: vi.fn() }
}))

import { shell } from 'electron'
import {
  AGENT_IPC_CHANNELS,
  type AgentInspectionView,
  type AgentResult,
  type AgentTaskSnapshot,
  type TypedRequestRoute
} from '../../shared/agent-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import { PageAnswerer } from '../../main/agent/page-answer'
import { PublicUrlPolicy } from '../../main/agent/public-url-policy'
import { TaskRequestInterpreter } from '../../main/agent/task-request-interpreter'
import { DEFAULT_ROUTES, ModelRouter, type RoutingTable } from '../../main/models/model-router'
import { ScriptedTextProvider } from '../../main/models/scripted-provider'
import { registerAgentIpc } from '../../main/services/agent-ipc'
import { ActiveTaskStore, AgentTaskController, type RuntimeRequester } from '../../main/services/agent-tasks'
import type { LocalStore } from '../../main/services/store'
import { executeConfirmedTool } from '../../main/services/tools'
import { VoiceTaskController } from '../../main/services/voice-task-controller'
import { FakeBookingRuntime } from '../../main/testing/fake-booking-runtime'
import { FakeInspectionRuntime, ORIGIN } from '../../main/testing/fake-inspection-runtime'
import { describeOutcome } from './agent-task-view'
import { describeInspectionForConversation, realtimeConversation, submitComposerRequest } from './composer-routing'

/**
 * The main composer, end to end across the bridge: renderer routing module ->
 * the fixed agent IPC channel -> main's TaskRequestInterpreter -> the durable
 * controller -> the (fake) runtime. The realtime conversation is a scripted
 * stand-in that behaves like the legacy model did: given an address it calls
 * the real `open_url` executor, and it can reach Telegram and capture. Any
 * request that reaches it is counted.
 */

const WEDNESDAY = Date.parse('2026-09-16T04:30:00Z')
const EXAMPLE = 'Inspect https://example.com and tell me what this page is for.'

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-composer-'))
  vi.mocked(shell.openExternal).mockClear()
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function router(): ModelRouter {
  const provider = new ScriptedTextProvider('gemini', 'rules')
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  table.page_answer = { ...table.page_answer, providers: [{ provider: 'gemini' }] }
  table.intent_extraction = { ...table.intent_extraction, providers: [{ provider: 'gemini' }] }
  return new ModelRouter((id) => (id === 'gemini' ? provider : undefined), table)
}

/** Main as index.ts assembles it, behind the real IPC registration. */
function desktop(runtime: RuntimeRequester, options: { interpreter?: boolean } = {}) {
  const model = router()
  const tasks = new AgentTaskController(runtime, new ActiveTaskStore(directory), {
    policy: new PublicUrlPolicy({ allowedHosts: ['example.com', 'github.com'], testOrigins: [ORIGIN] }),
    answerer: new PageAnswerer(model)
  })
  const voice = new VoiceTaskController(tasks, { calendarNow: () => WEDNESDAY, timeZone: () => 'Asia/Kolkata' })
  const interpreter = new TaskRequestInterpreter({
    router: model,
    controller: voice,
    inspections: tasks,
    loadTask: async () => {
      const loaded = await tasks.loadActiveTask(0)
      return loaded.ok ? loaded.value : null
    },
    now: () => WEDNESDAY,
    timeZone: () => 'Asia/Kolkata'
  })
  const handlers = new Map<string, (event: never, ...args: unknown[]) => unknown>()
  registerAgentIpc({
    ipcMain: { handle: (channel, listener) => { handlers.set(channel, listener) } },
    assertTrustedSender: () => undefined,
    controller: tasks,
    voice,
    ...(options.interpreter === false ? {} : { text: interpreter }),
    runtimeStatus: () => ({ state: 'running' }),
    restartRuntime: async () => ({ state: 'running' })
  })
  const invoke = <T>(channel: string, ...args: unknown[]): Promise<T> =>
    Promise.resolve(handlers.get(channel)!({ trusted: true } as never, ...args) as T)

  // The preload bridge methods the renderer uses.
  const agent = {
    routeTypedRequest: (requestId: string, text: string) => invoke<TypedRequestRoute>(AGENT_IPC_CHANNELS.routeTypedRequest, requestId, text),
    loadActiveTask: () => invoke<AgentResult<AgentTaskSnapshot | null>>(AGENT_IPC_CHANNELS.loadActiveTask, 0),
    approveInspection: (actionId: string, revision: number) =>
      invoke<AgentResult<AgentInspectionView>>(AGENT_IPC_CHANNELS.approveInspection, actionId, revision),
    executeInspection: (actionId: string, revision: number) =>
      invoke<AgentResult<AgentInspectionView>>(AGENT_IPC_CHANNELS.executeInspection, actionId, revision)
  }

  // The legacy realtime conversation, as it behaved before routing existed.
  const legacy = { telegram: vi.fn(), capture: vi.fn() }
  const conversation: string[] = []
  // No voice session exists until something connects one, exactly as when
  // Lumi has just started or voice has been paused.
  const session = { connects: 0, connectFails: false, client: undefined as { sendUserRequest: (text: string) => Promise<void> } | undefined }
  const ensureConnected = async (): Promise<void> => {
    session.connects += 1
    if (session.connectFails) throw new Error('Lumi could not reach the voice service.')
    session.client = { sendUserRequest: async (text) => { await legacyConversation(text) } }
  }
  const legacyConversation = async (text: string): Promise<void> => {
    conversation.push(text)
    const url = /https?:\/\/\S+/.exec(text)?.[0]
    if (url) {
      await executeConfirmedTool({} as LocalStore, {
        id: 'legacy-open', toolName: 'open_url', reason: 'Open it.', requiresConfirmation: true, arguments: { url }
      })
    }
    if (/telegram/i.test(text)) legacy.telegram()
    if (/screen|page/i.test(text)) legacy.capture()
  }
  // The app's own conversation step: connect, then send.
  const converse = realtimeConversation({
    ensureConnected,
    client: () => session.client,
    appendUserLine: () => undefined
  })

  // What LifeLensApp shows: the transcript and the focused agent surface.
  const transcript: string[] = []
  const focus: string[] = []
  const agentHandled = (text: string, result: AgentResult<VoiceTaskOutcome>): void => {
    transcript.push(`You: ${text}`)
    if (!result.ok) {
      transcript.push(result.error.message)
      return
    }
    transcript.push(describeOutcome(result.value))
    if (result.value.focus !== 'none') focus.push(result.value.focus)
  }
  let sequence = 0
  const send = (text: string) => submitComposerRequest(text, {
    route: agent.routeTypedRequest,
    converse,
    agentHandled,
    newRequestId: () => `req_composer_${(sequence += 1).toString().padStart(4, '0')}`
  })

  /** The trusted card's Approve and inspect button, as AgentTaskPanel runs it. */
  const approveCard = async (): Promise<AgentResult<AgentInspectionView>> => {
    const loaded = await agent.loadActiveTask()
    const card = loaded.ok ? loaded.value?.inspection : undefined
    if (!card) throw new Error('no inspection card on screen')
    const approved = await agent.approveInspection(card.actionId, card.revision)
    if (!approved.ok) return approved
    const done = await agent.executeInspection(card.actionId, approved.value.revision)
    if (done.ok) {
      const line = describeInspectionForConversation(done.value)
      if (line) transcript.push(line)
    }
    return done
  }

  return { agent, send, approveCard, conversation, legacy, transcript, focus, session }
}

function noLegacyEffects(app: ReturnType<typeof desktop>): void {
  expect(app.conversation).toEqual([])
  expect(shell.openExternal).not.toHaveBeenCalled()
  expect(app.legacy.telegram).not.toHaveBeenCalled()
  expect(app.legacy.capture).not.toHaveBeenCalled()
  // An agent-owned request neither needs nor starts a voice session.
  expect(app.session.connects).toBe(0)
}

describe('a page-inspection request typed in the main composer', () => {
  it('creates the durable inspection and focuses its card; realtime and open_url are never reached', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)

    expect(await app.send(EXAMPLE)).toBe('agent')

    const loaded = await app.agent.loadActiveTask()
    const snapshot = loaded.ok ? loaded.value : null
    expect(snapshot?.task.kind).toBe('page_inspection')
    expect(snapshot?.inspection).toMatchObject({
      status: 'WAITING_APPROVAL',
      proposal: { url: 'https://example.com/', host: 'example.com', question: 'Inspect and tell me what this page is for.' }
    })
    expect(runtime.count('POST', /^\/tasks$/)).toBe(1)
    expect(app.focus).toEqual(['approval_card'])
    expect(app.transcript).toEqual([
      `You: ${EXAMPLE}`,
      'Prepared an inspection of example.com. Nothing is opened until you press Approve and inspect.'
    ])
    expect(runtime.pageReads).toBe(0)
    noLegacyEffects(app)
  })

  it('the GitHub request from manual testing takes the same path', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    expect(await app.send('Inspect https://github.com/satish9177/Lumi and tell me what this project does.')).toBe('agent')
    expect(app.focus).toEqual(['approval_card'])
    expect([...runtime.tasks.values()][0].request).toMatchObject({ url: 'https://github.com/satish9177/Lumi' })
    noLegacyEffects(app)
  })

  it.each(['yes', 'approve', 'go ahead', 'yes, approve it and open the page'])('typed "%s" only points at the card', async (text) => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    await app.send(EXAMPLE)

    expect(await app.send(text)).toBe('agent')
    expect(app.transcript.at(-1)).toBe('Nothing was approved. Review the card for example.com and press Approve and inspect yourself.')
    expect(app.focus).toEqual(['approval_card', 'approval_card'])
    expect(runtime.count('POST', /\/(approve|browser-execution)$/)).toBe(0)
    expect([...runtime.actions.values()][0].status).toBe('WAITING_APPROVAL')
    expect(runtime.pageReads).toBe(0)
    noLegacyEffects(app)
  })

  it('request -> card -> trusted click -> one isolated read -> grounded answer in the conversation', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    await app.send(`${ORIGIN}/profiles/rated What is my contest rating?`)
    expect(runtime.pageReads).toBe(0)

    const done = await app.approveCard()
    expect(done.ok).toBe(true)
    const view = done.ok ? done.value : undefined
    // One click: the answer step ran automatically from the stored observation.
    expect(view).toMatchObject({ status: 'SUCCEEDED', answer: { status: 'answered' } })
    expect(view?.answer?.answer).toContain('1,842')
    expect(runtime.pageReads).toBe(1)
    expect(runtime.count('POST', /\/inspection\/answer$/)).toBe(1)
    expect(app.transcript.at(-1)).toMatch(/^From the inspected page on 127\.0\.0\.1: .*1,842/)
    noLegacyEffects(app)
  })

  it('a failed read is reported and never falls through to open_url', async () => {
    const runtime = new FakeInspectionRuntime()
    runtime.executionOutcome = 'FAILED'
    const app = desktop(runtime)
    await app.send(`${ORIGIN}/profiles/rated What is my contest rating?`)

    const done = await app.approveCard()
    expect(done.ok && done.value.status).toBe('FAILED')
    expect(app.transcript.at(-1)).toMatch(/^Lumi could not read 127\.0\.0\.1\. .*Nothing was answered\.$/)
    expect(runtime.pageReads).toBe(1)
    noLegacyEffects(app)
  })

  it.each([
    ['Inspect https://leetcode.com/u/someone/ and tell me my rating', 'That website is not on Lumi’s list of sites it may inspect.'],
    ['What is on https://localhost/admin ?', 'Local and private network addresses cannot be inspected.'],
    ['Open https://169.254.169.254/latest/meta-data/ for me', 'Addresses that are raw IP numbers cannot be inspected.']
  ])('a refused address is still owned by the agent: %s', async (text, message) => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    expect(await app.send(text)).toBe('agent')
    expect(app.transcript).toEqual([`You: ${text}`, message])
    expect(app.focus).toEqual([])
    expect(runtime.calls).toEqual([])
    noLegacyEffects(app)
  })

  it('without a configured interpreter an address is refused, not passed to the conversation', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime, { interpreter: false })
    expect(await app.send(EXAMPLE)).toBe('agent')
    expect(app.transcript.at(-1)).toBe('Page inspection needs a configured text model. Nothing was opened.')
    noLegacyEffects(app)
    expect(await app.send('Hello')).toBe('conversation')
    expect(app.conversation).toEqual(['Hello'])
  })

  it('control: the scripted conversation really would open the browser if a request reached it', async () => {
    const app = desktop(new FakeInspectionRuntime())
    await submitComposerRequest(EXAMPLE, {
      route: async () => ({ handled: false }),
      converse: async (text) => { app.conversation.push(text); await executeConfirmedTool({} as LocalStore, { id: 'x', toolName: 'open_url', reason: 'r', requiresConfirmation: true, arguments: { url: 'https://example.com' } }) },
      agentHandled: () => undefined
    })
    expect(shell.openExternal).toHaveBeenCalledTimes(1)
  })
})

describe('ordinary conversation is not swallowed', () => {
  it.each(['Hello', 'Explain what Kafka is', 'Help me write an email', 'yes', 'check the weather for me', 'what is the status of my order'])(
    '"%s" with no agent task reaches realtime exactly once',
    async (text) => {
      const runtime = new FakeBookingRuntime()
      const app = desktop(runtime)
      expect(await app.send(text)).toBe('conversation')
      expect(app.conversation).toEqual([text])
      expect(app.transcript).toEqual([])
      expect(runtime.counts.creates).toBe(0)
    }
  )

  it('"Hello" while an inspection card is open still reaches realtime and changes nothing', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    await app.send(EXAMPLE)
    const before = runtime.calls.filter((call) => call.method === 'POST').length
    expect(await app.send('Hello')).toBe('conversation')
    expect(app.conversation).toEqual(['Hello'])
    expect(runtime.calls.filter((call) => call.method === 'POST').length).toBe(before)
  })

  it('if main cannot be asked, nothing is sent anywhere', async () => {
    const converse = vi.fn(async () => undefined)
    const agentHandled = vi.fn()
    await expect(submitComposerRequest('Hello', {
      route: async () => { throw new Error('bridge unavailable') },
      converse,
      agentHandled
    })).rejects.toThrow('bridge unavailable')
    expect(converse).not.toHaveBeenCalled()
    expect(agentHandled).not.toHaveBeenCalled()
  })
})

describe('routing semantics at the main boundary', () => {
  it('fails closed on an invalid request reference', async () => {
    const app = desktop(new FakeInspectionRuntime())
    for (const requestId of ['', 'short', 'has space in it', 'x'.repeat(65)]) {
      expect(await app.agent.routeTypedRequest(requestId, 'Hello')).toEqual({
        handled: true, result: { ok: false, error: { code: 'invalid_request', message: 'That request reference is invalid.' } }
      })
    }
  })

  it('leaves over-long ordinary text to the conversation but refuses an over-long address request', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    const long = 'Please summarise this. '.repeat(60)
    expect(await app.agent.routeTypedRequest('req_long_text_01', long)).toEqual({ handled: false })
    const route = await app.agent.routeTypedRequest('req_long_text_02', `${long} https://example.com`)
    expect(route).toMatchObject({ handled: true, result: { ok: false, error: { code: 'invalid_request' } } })
    expect(runtime.calls).toEqual([])
  })

  it('answers a replayed request id once: one task, the same route', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    const first = await app.agent.routeTypedRequest('req_replayed_0001', EXAMPLE)
    const again = await app.agent.routeTypedRequest('req_replayed_0001', EXAMPLE)
    expect(again).toEqual(first)
    expect(runtime.count('POST', /^\/tasks$/)).toBe(1)
  })
})

describe('appointment requests typed in the main composer', () => {
  it('still go to the durable agent, not realtime', async () => {
    const runtime = new FakeBookingRuntime()
    const app = desktop(runtime)
    expect(await app.send('Find me a dermatologist on Saturday')).toBe('agent')
    expect(runtime.counts.creates).toBe(1)
    expect(app.transcript.at(-1)).toBe('Found 2 matching appointments.')
    expect(app.focus).toEqual(['task'])

    // With the task open, a follow-up belongs to it too.
    expect(await app.send('take the cheapest one and prepare it')).toBe('agent')
    expect(app.transcript.at(-1)).toMatch(/^Prepared Dr A, .*Nothing is booked until you press Approve and book\./)
    expect(app.focus.at(-1)).toBe('approval_card')
    expect(runtime.counts.approvals).toBe(0)
    expect(runtime.counts.executions).toBe(0)
    noLegacyEffects(app)
  })
})

describe('typed requests do not depend on voice', () => {
  it('a page inspection is created with no voice session, and none is started', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    expect(app.session.client).toBeUndefined()

    expect(await app.send(EXAMPLE)).toBe('agent')

    const loaded = await app.agent.loadActiveTask()
    expect(loaded.ok && loaded.value?.task.kind).toBe('page_inspection')
    expect(loaded.ok && loaded.value?.inspection?.status).toBe('WAITING_APPROVAL')
    expect(app.focus).toEqual(['approval_card'])
    // Never connected, never sent, nothing opened.
    expect(app.session.connects).toBe(0)
    noLegacyEffects(app)
  })

  it('an appointment request works with no voice session either', async () => {
    const runtime = new FakeBookingRuntime()
    const app = desktop(runtime)
    expect(await app.send('Find me a dermatologist on Saturday')).toBe('agent')
    expect(runtime.counts.creates).toBe(1)
    expect(app.session.connects).toBe(0)
  })

  it('an unhandled request connects voice first, then sends exactly once', async () => {
    const app = desktop(new FakeInspectionRuntime())
    expect(await app.send('Hello')).toBe('conversation')
    expect(app.session.connects).toBe(1)
    expect(app.conversation).toEqual(['Hello'])
  })

  it('a voice connection failure affects only the conversation request', async () => {
    const runtime = new FakeInspectionRuntime()
    const app = desktop(runtime)
    app.session.connectFails = true

    // The durable agent still answers a request it owns.
    expect(await app.send(EXAMPLE)).toBe('agent')
    expect(app.focus).toEqual(['approval_card'])
    expect(app.session.connects).toBe(0)

    // Ordinary chat reports the failure honestly and sends nothing.
    await expect(app.send('Hello')).rejects.toThrow('Lumi could not reach the voice service.')
    expect(app.conversation).toEqual([])
    expect(shell.openExternal).not.toHaveBeenCalled()

    // And the failure created no durable task of its own.
    const loaded = await app.agent.loadActiveTask()
    expect(loaded.ok && loaded.value?.task.kind).toBe('page_inspection')
  })

  it('without a voice client an unhandled request says so rather than failing silently', async () => {
    const connects: number[] = []
    const converse = realtimeConversation({
      ensureConnected: async () => { connects.push(1) },
      client: () => undefined,
      appendUserLine: () => { throw new Error('a line was added although nothing was sent') }
    })
    await expect(converse('Hello')).rejects.toThrow('Connect voice first, then ask Lumi a question.')
    expect(connects).toHaveLength(1)
  })
})

describe('the app uses the router for every typed request', () => {
  const app = readFileSync(join(process.cwd(), 'src/renderer/src/LifeLensApp.tsx'), 'utf8')
  const start = app.indexOf('const askQuestion = async')
  const body = app.slice(start, app.indexOf('const chooseDocumentRoot', start))

  it('asks main first and reaches realtime only from the unhandled branch', () => {
    expect(body).toContain('submitComposerRequest(request, {')
    expect(body).toContain('route: (requestId, text) => window.lifeLens.agent.routeTypedRequest(requestId, text)')
    const converse = body.slice(body.indexOf('converse: async (text) => {'))
    expect(converse).toContain('conversationStep(text)')
    // The app sends to realtime only through the tested conversation step,
    // and that step is the only place the composer path connects voice.
    expect(app).not.toMatch(/sendUserRequest\(/)
    expect(body).not.toContain('ensureConnected')
    expect(app).toContain('const conversationStep = realtimeConversation({')
  })

  it('send needs text, not a voice session, and one request at a time', () => {
    const gate = app.slice(app.indexOf('const sendDisabledReason ='), app.indexOf('const canSend ='))
    expect(gate).not.toContain('clientRef')
    expect(gate).toContain('COPY.labels.sendDisabledEmpty')
    expect(gate).toContain('COPY.labels.sendDisabledBusy')
    // The synchronous guard, and the disabled control that follows it.
    expect(body).toContain('if (!request || routingRef.current) return')
    expect(body).toContain('routingRef.current = true')
    expect(body).toContain('setIsSendingRequest(true)')
    expect(app).toContain('disabled={!canSend}')
  })

  it('shows the agent surface with neutral names', () => {
    expect(app).toContain('aria-label="Lumi agent"')
    expect(app).not.toMatch(/Book appointment|Appointment booking/)
    const panel = readFileSync(join(process.cwd(), 'src/renderer/src/components/AgentTaskPanel.tsx'), 'utf8')
    expect(panel).toContain('aria-label="Close agent"')
    expect(panel).not.toMatch(/Appointment booking|Close appointment booking/)
  })
})
