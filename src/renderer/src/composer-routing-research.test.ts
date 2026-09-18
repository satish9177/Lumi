import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AGENT_IPC_CHANNELS, type AgentResult, type AgentTaskSnapshot, type TypedRequestRoute } from '../../shared/agent-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import { ActiveTaskStore, AgentTaskController } from '../../main/services/agent-tasks'
import { registerAgentIpc } from '../../main/services/agent-ipc'
import { VoiceTaskController } from '../../main/services/voice-task-controller'
import { TaskRequestInterpreter } from '../../main/agent/task-request-interpreter'
import { PublicUrlPolicy, RESEARCH_POLICY_VERSION } from '../../main/agent/public-url-policy'
import { ResearchPlanner } from '../../main/agent/research-planner'
import { ResearchAnswerer } from '../../main/agent/research-answer'
import { PageAnswerer } from '../../main/agent/page-answer'
import { DEFAULT_ROUTES, ModelRouter, type RoutingTable } from '../../main/models/model-router'
import { ScriptedTextProvider } from '../../main/models/scripted-provider'
import { FakeResearchRuntime } from '../../main/testing/fake-research-runtime'
import { describeResearchForConversation, realtimeConversation, submitComposerRequest } from './composer-routing'
import { describeOutcome, describeResearch, researchSources } from './agent-task-view'

/**
 * A public-research request typed in the main composer, end to end through the
 * real seams: renderer -> the fixed agent IPC channel -> main's interpreter ->
 * the durable controller -> the (fake) runtime, with main's planner and
 * answerer driven by the deterministic scripted provider.
 *
 * The properties under test are ownership and consent:
 *
 *  - a research request is claimed by the durable agent and never reaches the
 *    realtime conversation, where a legacy tool could open an address itself;
 *  - an ordinary question still goes to the conversation;
 *  - nothing is searched or opened until the trusted Allow click;
 *  - none of it needs a voice session.
 */

const RESEARCH = 'Find the Lumi repository on GitHub and tell me what it does'
const CONVERSATION = 'Explain Kafka consumer groups.'

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-research-route-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function router(): ModelRouter {
  const provider = new ScriptedTextProvider('gemini', 'rules')
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  for (const taskClass of ['intent_extraction', 'research_planning', 'research_answer', 'page_answer'] as const) {
    table[taskClass] = { ...table[taskClass], providers: [{ provider: 'gemini' }] }
  }
  return new ModelRouter((id) => (id === 'gemini' ? provider : undefined), table)
}

/** Main as index.ts assembles it, behind the real IPC registration. */
function desktop(runtime: FakeResearchRuntime, options: { research?: boolean } = {}) {
  const model = router()
  const tasks = new AgentTaskController(
    runtime,
    new ActiveTaskStore(directory),
    { policy: new PublicUrlPolicy({ allowedHosts: ['example.com'] }), answerer: new PageAnswerer(model) },
    options.research === false
      ? { policy: new PublicUrlPolicy({ version: RESEARCH_POLICY_VERSION }) }
      : {
          policy: new PublicUrlPolicy({ allowAnyPublicHost: true, version: RESEARCH_POLICY_VERSION }),
          planner: new ResearchPlanner(model),
          answerer: new ResearchAnswerer(model)
        }
  )
  const voice = new VoiceTaskController(tasks, { calendarNow: () => Date.now(), timeZone: () => 'Asia/Kolkata' })
  const interpreter = new TaskRequestInterpreter({
    router: model,
    controller: voice,
    inspections: tasks,
    research: tasks,
    loadTask: async () => {
      const loaded = await tasks.loadActiveTask(0)
      return loaded.ok ? loaded.value : null
    }
  })
  const handlers = new Map<string, (event: never, ...args: unknown[]) => unknown>()
  registerAgentIpc({
    ipcMain: { handle: (channel, listener) => { handlers.set(channel, listener) } },
    assertTrustedSender: () => undefined,
    controller: tasks,
    voice,
    text: interpreter,
    runtimeStatus: () => ({ state: 'running' }),
    restartRuntime: async () => ({ state: 'running' })
  })
  const invoke = <T>(channel: string, ...args: unknown[]): Promise<T> =>
    Promise.resolve(handlers.get(channel)!({ trusted: true } as never, ...args) as T)

  const agent = {
    routeTypedRequest: (requestId: string, text: string) =>
      invoke<TypedRequestRoute>(AGENT_IPC_CHANNELS.routeTypedRequest, requestId, text),
    loadActiveTask: () => invoke<AgentResult<AgentTaskSnapshot | null>>(AGENT_IPC_CHANNELS.loadActiveTask, 0),
    grantResearchScope: (grantId: string, revision: number) =>
      invoke<AgentResult<AgentTaskSnapshot>>(AGENT_IPC_CHANNELS.grantResearchScope, grantId, revision),
    runResearch: () => invoke<AgentResult<AgentTaskSnapshot>>(AGENT_IPC_CHANNELS.runResearch)
  }

  // The legacy realtime conversation, as it behaved before routing existed.
  const legacy = { openUrl: vi.fn(), telegram: vi.fn(), capture: vi.fn() }
  const conversation: string[] = []
  const session = { connects: 0, client: undefined as { sendUserRequest: (text: string) => Promise<void> } | undefined }
  const ensureConnected = async (): Promise<void> => {
    session.connects += 1
    session.client = { sendUserRequest: async (text) => { await legacyConversation(text) } }
  }
  const legacyConversation = async (text: string): Promise<void> => {
    conversation.push(text)
    if (/https?:\/\/\S+/.test(text)) legacy.openUrl()
    if (/telegram/i.test(text)) legacy.telegram()
    if (/screen|page/i.test(text)) legacy.capture()
  }
  const converse = realtimeConversation({
    ensureConnected,
    client: () => session.client,
    appendUserLine: () => undefined
  })

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

  /** The trusted card's Allow research button, as AgentTaskPanel runs it. */
  const allowCard = async (): Promise<AgentResult<AgentTaskSnapshot>> => {
    const loaded = await agent.loadActiveTask()
    const grant = loaded.ok ? loaded.value?.research?.grant : undefined
    if (!grant) throw new Error('no research card on screen')
    const granted = await agent.grantResearchScope(grant.grantId, grant.revision)
    if (!granted.ok) return granted
    const done = await agent.runResearch()
    if (done.ok && done.value.research) {
      const line = describeResearchForConversation(done.value.research)
      if (line) transcript.push(line)
    }
    return done
  }

  return { agent, send, allowCard, conversation, legacy, transcript, focus, session }
}

function noLegacyEffects(app: ReturnType<typeof desktop>): void {
  expect(app.conversation).toEqual([])
  expect(app.legacy.openUrl).not.toHaveBeenCalled()
  expect(app.legacy.telegram).not.toHaveBeenCalled()
  expect(app.legacy.capture).not.toHaveBeenCalled()
  // An agent-owned request neither needs nor starts a voice session.
  expect(app.session.connects).toBe(0)
}

describe('a public-research request typed in the main composer', () => {
  it('is claimed by the durable agent and shows its permission card', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    expect(await app.send(RESEARCH)).toBe('agent')
    noLegacyEffects(app)
    expect(app.focus).toEqual(['approval_card'])

    const loaded = await app.agent.loadActiveTask()
    expect(loaded.ok).toBe(true)
    if (!loaded.ok || !loaded.value) throw new Error('unreachable')
    expect(loaded.value.task.kind).toBe('public_research')
    expect(loaded.value.research?.grant?.status).toBe('PENDING')
    // Nothing has been searched or opened while the card is on screen.
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
  })

  it('searches and answers only after the trusted Allow click', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    await app.send(RESEARCH)
    expect(runtime.searches).toBe(0)

    const done = await app.allowCard()
    expect(done.ok).toBe(true)
    if (!done.ok) throw new Error('unreachable')
    expect(runtime.searches).toBe(1)
    const research = done.value.research!
    // The offline stand-in reads by label, so it may answer fully or partly.
    // What must hold either way is that the answer is grounded: every quote it
    // cites is text a page Lumi opened actually showed.
    expect(['answered', 'partial']).toContain(research.answer?.status)
    expect(research.answer?.evidence.length).toBeGreaterThan(0)
    for (const item of research.answer!.evidence) {
      const observation = research.observations.find((candidate) => candidate.ref === item.observation)
      const block = observation?.blocks.find((candidate) => candidate.id === item.block)
      expect(block?.text).toContain(item.quote)
    }
    // The user is told which public pages the answer came from.
    const sources = researchSources(research)
    expect(sources.length).toBeGreaterThan(0)
    expect(app.transcript.at(-1)).toContain('public page')
    noLegacyEffects(app)
  })

  it('shows a card whose words are Lumi’s, listing what research may not do', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    await app.send(RESEARCH)
    const loaded = await app.agent.loadActiveTask()
    if (!loaded.ok || !loaded.value?.research) throw new Error('unreachable')
    const card = describeResearch(loaded.value.research, Date.now())
    expect(card.showScope).toBe(true)
    expect(card.controls).toEqual(['decline_research', 'allow_research'])
    expect(loaded.value.research.grant?.scope.forbidden).toContain('login')
    expect(loaded.value.research.grant?.scope.forbidden).toContain('uploads_and_downloads')
  })

  it('cannot be allowed by typing "yes, go ahead"', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    await app.send(RESEARCH)
    for (const text of ['yes, go ahead', 'approve it', 'allow research', 'do it']) {
      await app.send(text)
    }
    const loaded = await app.agent.loadActiveTask()
    if (!loaded.ok || !loaded.value?.research) throw new Error('unreachable')
    // Typed words are not the trusted click. The scope is still pending and
    // nothing has been searched or opened.
    expect(loaded.value.research.grant?.status).toBe('PENDING')
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
    expect(runtime.count('POST', /\/research\/grant$/)).toBe(0)
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(0)
  })

  it('is still claimed, never passed on, when research is not configured', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime, { research: false })
    expect(await app.send(RESEARCH)).toBe('agent')
    noLegacyEffects(app)
    expect(app.transcript.at(-1)).toContain('not set up')
    expect(runtime.tasks.size).toBe(0)
  })
})

describe('ordinary conversation is not swallowed', () => {
  it('goes to the realtime conversation, and starts a voice session to do it', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    expect(await app.send(CONVERSATION)).toBe('conversation')
    expect(app.conversation).toEqual([CONVERSATION])
    expect(app.session.connects).toBe(1)
    expect(runtime.tasks.size).toBe(0)
    expect(runtime.searches).toBe(0)
  })

  it.each([
    'What is the weather like?',
    'Tell me a joke',
    'Summarise what we just talked about'
  ])('leaves %s to the conversation', async (text) => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    expect(await app.send(text)).toBe('conversation')
    expect(runtime.tasks.size).toBe(0)
  })
})

describe('the Milestone 7a path is unchanged', () => {
  it('a request naming one address is still a page inspection, not research', async () => {
    const runtime = new FakeResearchRuntime()
    const app = desktop(runtime)
    // The fake research runtime has no inspection routes, so the request is
    // claimed and refused rather than turning into a research task.
    expect(await app.send('Inspect https://example.com and tell me what this page is for.')).toBe('agent')
    noLegacyEffects(app)
    expect(runtime.count('POST', /\/research\/prepare$/)).toBe(0)
  })
})
