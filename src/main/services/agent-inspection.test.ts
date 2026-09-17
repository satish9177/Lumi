import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { PageAnswerer } from '../agent/page-answer'
import { PublicUrlPolicy } from '../agent/public-url-policy'
import { TaskRequestInterpreter, extractInspectionRequest } from '../agent/task-request-interpreter'
import { DEFAULT_ROUTES, ModelRouter, type RoutingTable } from '../models/model-router'
import { ScriptedTextProvider, type ScriptedBehaviour } from '../models/scripted-provider'
import { FakeInspectionRuntime, ORIGIN } from '../testing/fake-inspection-runtime'
import { ActiveTaskStore, AgentTaskController, type PageInspectionSupport } from './agent-tasks'
import { VoiceTaskController } from './voice-task-controller'

type Json = Record<string, unknown>

function routerWith(behaviours: ScriptedBehaviour[]): ModelRouter {
  const providers = behaviours.map((behaviour, index) => new ScriptedTextProvider((['gemini', 'openai', 'deepseek'] as const)[index], behaviour))
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  table.page_answer = { ...table.page_answer, providers: providers.map((provider) => ({ provider: provider.id })) }
  return new ModelRouter((id) => providers.find((provider) => provider.id === id), table)
}

let directory: string
let runtime: FakeInspectionRuntime
let support: PageInspectionSupport

function controller(behaviours: ScriptedBehaviour[] = ['rules']): AgentTaskController {
  support = { policy: new PublicUrlPolicy({ allowedHosts: ['github.com'], testOrigins: [ORIGIN] }), answerer: new PageAnswerer(routerWith(behaviours)) }
  return new AgentTaskController(runtime, new ActiveTaskStore(directory), support)
}

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-inspection-'))
  runtime = new FakeInspectionRuntime()
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function unwrap<T>(result: { ok: true; value: T } | { ok: false; error: { code: string; message: string } }): T {
  if (!result.ok) throw new Error(`${result.error.code}: ${result.error.message}`)
  return result.value
}

describe('page inspection through Electron main', () => {
  it('prepares a card from a canonical URL, opens nothing, and binds the disclosure', async () => {
    const tasks = controller()
    const snapshot = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated#top`, '  What is my   contest rating? '))
    expect(snapshot.task.kind).toBe('page_inspection')
    expect(snapshot.task.inspection).toEqual({ url: `${ORIGIN}/profiles/rated`, host: '127.0.0.1', question: 'What is my contest rating?' })
    expect(snapshot.inspection).toMatchObject({
      status: 'WAITING_APPROVAL',
      proposal: { url: `${ORIGIN}/profiles/rated`, host: '127.0.0.1', question: 'What is my contest rating?', recipients: ['scripted'], maxTextChars: 12_000 },
      approval: { status: 'PENDING' }
    })
    expect(runtime.pageReads).toBe(0)
    expect(runtime.calls.find((call) => call.path.endsWith('/inspection/prepare'))?.body).toEqual({
      disclosure: { recipients: ['scripted'], max_text_chars: 12_000 }
    })
  })

  it.each([
    ['file:///C:/Windows/win.ini', 'Only https web pages can be inspected.'],
    ['https://localhost/admin', 'Local and private network addresses cannot be inspected.'],
    ['https://169.254.169.254/latest/meta-data/', 'Addresses that are raw IP numbers cannot be inspected.'],
    ['https://leetcode.com/u/someone/', 'That website is not on Lumi’s list of sites it may inspect.'],
    ['javascript:alert(1)', 'Only https web pages can be inspected.']
  ])('refuses %s before contacting the runtime', async (url, message) => {
    const result = await controller().createPageInspection(url, 'What is my rating?')
    expect(result).toEqual({ ok: false, error: { code: 'destination_not_allowed', message } })
    expect(runtime.calls).toEqual([])
  })

  it('offers nothing when unconfigured or when no model could answer', async () => {
    const unconfigured = new AgentTaskController(runtime, new ActiveTaskStore(directory), { policy: new PublicUrlPolicy() })
    expect((await unconfigured.createPageInspection('https://github.com/', 'What?')).ok).toBe(false)
    const noModel = new AgentTaskController(runtime, new ActiveTaskStore(directory), { policy: new PublicUrlPolicy({ allowedHosts: ['github.com'] }) })
    const result = await noModel.createPageInspection('https://github.com/', 'What?')
    expect(result.ok ? undefined : result.error.code).toBe('inspection_unavailable')
    expect(runtime.calls).toEqual([])
  })

  it('reads once only after the trusted approval, and answers from the stored observation', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    // Executing the card as shown (before approval) is refused by the ledger.
    expect((await tasks.executeInspection(card.actionId, card.revision)).ok).toBe(false)
    expect(runtime.pageReads).toBe(0)

    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    expect(approved.status).toBe('APPROVED')
    const done = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(runtime.pageReads).toBe(1)
    expect(done.status).toBe('SUCCEEDED')
    expect(done.answer).toMatchObject({
      status: 'answered', answer: 'Contest rating: 1,842.', provider: 'scripted',
      evidence: [{ block: 'b2', quote: 'Contest rating' }, { block: 'b3', quote: '1,842' }]
    })
    expect(done.observation).toMatchObject({ finalUrl: `${ORIGIN}/profiles/rated`, blockCount: 7 })
    // The renderer view carries metadata and quoted evidence, never the page's blocks.
    expect(JSON.stringify(done)).not.toContain('Global rank')

    const answerBody = runtime.calls.find((call) => call.path.endsWith('/inspection/answer'))!.body as Json
    expect(answerBody.content_hash).toBe((runtime.actions.get(card.actionId)!.observation as Json).content_hash)

    // The consumed approval cannot read the page again.
    expect((await tasks.executeInspection(card.actionId, done.revision)).ok).toBe(false)
    expect(runtime.pageReads).toBe(1)
  })

  it('a stale revision is refused in main before any runtime mutation', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const result = await tasks.approveInspection(card.actionId, card.revision - 1)
    expect(result).toMatchObject({ ok: false, error: { code: 'stale_revision', currentRevision: card.revision } })
    expect(runtime.count('POST', /\/approve$/)).toBe(0)
  })

  it('a duplicate typed request, even after a main restart, shows the same card instead of a second task', async () => {
    const origin = { source: 'text' as const, turnId: 'req_duplicate01', utterance: 'x' }
    const first = unwrap(await controller().createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?', origin))
    // A new controller over the same profile directory: main restarted.
    const again = unwrap(await controller().createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?', origin))
    expect(again.task.taskId).toBe(first.task.taskId)
    expect(again.inspection?.actionId).toBe(first.inspection?.actionId)
    expect(runtime.count('POST', /^\/tasks$/)).toBe(1)
  })

  it('a lost execution response is not retried; after restart the stored observation is answered without reopening the page', async () => {
    const tasks = controller(['timeout'])
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    runtime.loseNextExecutionResponse = true
    const lost = await tasks.executeInspection(card.actionId, approved.revision)
    expect(lost).toMatchObject({ ok: false, error: { code: 'runtime_restarted' } })
    expect(runtime.count('POST', /\/browser-execution$/)).toBe(1)

    // Main restarts with a working model. Durable state shows the read happened.
    const restarted = controller(['rules'])
    const snapshot = unwrap(await restarted.loadActiveTask(0))!
    expect(snapshot.inspection).toMatchObject({ status: 'SUCCEEDED' })
    expect(snapshot.inspection?.answer).toBeUndefined()
    const answered = unwrap(await restarted.answerInspection(card.actionId))
    expect(answered.answer?.status).toBe('answered')
    expect(runtime.pageReads).toBe(1)
    expect(runtime.count('POST', /\/browser-execution$/)).toBe(1)
  })

  it('when no model answers after the read, the page is not reopened and the answer can be produced later', async () => {
    const tasks = controller(['unavailable'])
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    const read = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(read.status).toBe('SUCCEEDED')
    expect(read.answer).toBeUndefined()
    const retry = await tasks.answerInspection(card.actionId)
    expect(retry).toMatchObject({ ok: false, error: { code: 'answer_unavailable' } })
    expect(runtime.pageReads).toBe(1)
  })

  it('a missing value is recorded as not found, never guessed', async () => {
    runtime.pageBlocks = ['lumi_fixture_coder', 'Global rank', '12,345', 'Problems solved', '367', 'No contest history yet.']
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/unrated`, 'What is my contest rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    const done = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(done.answer).toMatchObject({ status: 'not_found', answer: 'Could not verify this from the inspected page.', evidence: [] })
  })

  it('an unknown outcome stays unknown; a repeat is a new card needing a new approval', async () => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const approved = unwrap(await tasks.approveInspection(card.actionId, card.revision))
    runtime.executionOutcome = 'OUTCOME_UNKNOWN'
    const unknown = unwrap(await tasks.executeInspection(card.actionId, approved.revision))
    expect(unknown.status).toBe('OUTCOME_UNKNOWN')
    expect(runtime.count('POST', /\/inspection\/answer$/)).toBe(0)
    const again = unwrap(await tasks.inspectPageAgain()).inspection!
    expect(again.actionId).not.toBe(card.actionId)
    expect(again).toMatchObject({ status: 'WAITING_APPROVAL', approval: { status: 'PENDING' } })
    expect(runtime.actions.get(card.actionId)!.status).toBe('OUTCOME_UNKNOWN')
  })

  it('cannot act on an inspection of another task', async () => {
    const tasks = controller()
    const first = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    await tasks.closeActiveTask()
    unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/unrated`, 'rating?'))
    const result = await tasks.approveInspection(first.actionId, first.revision)
    expect(result).toMatchObject({ ok: false, error: { code: 'not_found' } })
    expect(runtime.count('POST', /\/approve$/)).toBe(0)
  })
})

describe('speech and text cannot approve an inspection', () => {
  it.each(['yes', 'approve', 'go ahead', 'approve it and open the page', 'yes do it'])('"%s" only points at the card', async (utterance) => {
    const tasks = controller()
    const card = unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?')).inspection!
    const voice = new VoiceTaskController(tasks)
    const interpreter = new TaskRequestInterpreter({
      controller: voice,
      inspections: tasks,
      loadTask: async () => unwrap(await tasks.loadActiveTask(0))
    })
    const outcome = unwrap(await interpreter.submit('req_approve0001', utterance))
    expect(outcome.focus).toBe('approval_card')
    expect(outcome.narration).toEqual({ kind: 'inspection', host: '127.0.0.1', state: 'approval_required' })
    expect(runtime.count('POST', /\/(approve|browser-execution)$/)).toBe(0)
    expect(runtime.actions.get(card.actionId)!.status).toBe('WAITING_APPROVAL')
  })

  it('a voice command that tries to approve is refused outright', async () => {
    const tasks = controller()
    unwrap(await tasks.createPageInspection(`${ORIGIN}/profiles/rated`, 'rating?'))
    const voice = new VoiceTaskController(tasks)
    for (const command of [
      { kind: 'approve_inspection', turn: { turnId: 'item_1', utterance: 'yes' } },
      { kind: 'execute_action', turn: { turnId: 'item_2', utterance: 'yes' } },
      { kind: 'inspect_page', turn: { turnId: 'item_3', utterance: 'x' }, url: 'https://github.com/' }
    ]) {
      const result = await voice.handle(command, 'voice')
      expect(result.ok).toBe(false)
    }
    expect(runtime.count('POST', /\/(approve|browser-execution)$/)).toBe(0)
  })

  it('a typed request with one URL prepares a card and never opens the page', async () => {
    const tasks = controller()
    const interpreter = new TaskRequestInterpreter({
      controller: new VoiceTaskController(tasks),
      inspections: tasks,
      loadTask: async () => unwrap(await tasks.loadActiveTask(0))
    })
    const outcome = unwrap(await interpreter.submit('req_inspect0001', `Open ${ORIGIN}/profiles/rated and tell me my contest rating`))
    expect(outcome).toMatchObject({ focus: 'approval_card', narration: { kind: 'inspection', state: 'awaiting_approval' }, taskKind: 'page_inspection' })
    expect(runtime.pageReads).toBe(0)
    expect(extractInspectionRequest('compare https://github.com/a and https://github.com/b')).toBeUndefined()
    expect(extractInspectionRequest('what is on https://github.com/satish9177/Lumi?')).toEqual({ url: 'https://github.com/satish9177/Lumi', question: 'what is on' })
  })
})
