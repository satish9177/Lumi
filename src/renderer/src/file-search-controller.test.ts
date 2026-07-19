import { describe, expect, it, vi } from 'vitest'
import type {
  ApprovedDocumentRoot,
  FileSearchOutcome,
  FileSearchRequest,
  FileSearchResults
} from '../../shared/contracts'
import type { NormalizedSearchQuery } from '../../shared/search-query'
import { IntentTracker } from '../../main/services/intent-policy'
import { SearchOrchestrator } from '../../main/services/search-orchestrator'
import {
  FileSearchController,
  type FileSearchControllerCallbacks,
  type SearchConfirmationRequest,
  type TerminalCallResult
} from './file-search-controller'
import { RealtimeClient, type RealtimeServerCall } from './realtime'

const SERVER_CALL: RealtimeServerCall = { callId: 'call-1', generation: 1 }

function createResults(overrides: Partial<FileSearchResults> = {}): FileSearchResults {
  return {
    results: [
      {
        id: 'result-1',
        rootId: 'root-1',
        name: 'Resume_2026.pdf',
        relativePath: 'career/Resume_2026.pdf',
        modifiedAt: '2026-07-15T09:00:00.000Z',
        kind: 'document'
      }
    ],
    compactResults: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }],
    fallback: false,
    message: 'Found 1 matching file.',
    ...overrides
  }
}

interface ControllerHarness {
  controller: FileSearchController
  begins: FileSearchRequest[]
  completions: Array<{ callId: string | undefined; result: TerminalCallResult }>
  confirmations: Array<SearchConfirmationRequest | undefined>
  applied: FileSearchResults[]
  errors: Array<string | undefined>
  searchings: boolean[]
  chooseFolder: ReturnType<typeof vi.fn>
  listenings: number
}

function createControllerHarness(begin: (request: FileSearchRequest) => Promise<FileSearchOutcome>): ControllerHarness {
  const begins: FileSearchRequest[] = []
  const completions: Array<{ callId: string | undefined; result: TerminalCallResult }> = []
  const confirmations: Array<SearchConfirmationRequest | undefined> = []
  const applied: FileSearchResults[] = []
  const errors: Array<string | undefined> = []
  const searchings: boolean[] = []
  const chooseFolder = vi.fn(async () => undefined)
  let listenings = 0

  const callbacks: FileSearchControllerCallbacks = {
    begin: (request) => { begins.push(request); return begin(request) },
    chooseFolder,
    completeCall: (serverCall, result) => { completions.push({ callId: serverCall?.callId, result }) },
    applyResults: (results) => { applied.push(results) },
    presentConfirmation: (request) => { confirmations.push(request) },
    setSearching: (searching) => { searchings.push(searching) },
    setError: (message) => { errors.push(message) },
    setListening: () => { listenings += 1 }
  }

  return {
    controller: new FileSearchController(callbacks),
    begins,
    completions,
    confirmations,
    applied,
    errors,
    searchings,
    chooseFolder,
    get listenings() { return listenings }
  }
}

describe('FileSearchController routing', () => {
  it('completes a ready search and answers the call once', async () => {
    const harness = createControllerHarness(async () => ({ status: 'completed', ...createResults() }))

    await harness.controller.run({ queryTerms: 'resume' }, SERVER_CALL, 'model')

    expect(harness.applied).toHaveLength(1)
    expect(harness.completions).toEqual([
      { callId: 'call-1', result: expect.objectContaining({ ok: true, resultIds: ['result-1'] }) }
    ])
    expect(harness.chooseFolder).not.toHaveBeenCalled()
    expect(harness.confirmations).toEqual([])
    expect(harness.searchings).toEqual([true, false])
  })

  it('opens the folder chooser without answering the held call when a folder is missing', async () => {
    const harness = createControllerHarness(async () => ({ status: 'awaiting_folder', pendingId: 'pending-1' }))

    await harness.controller.run({ queryTerms: 'resume' }, SERVER_CALL, 'model')

    expect(harness.chooseFolder).toHaveBeenCalledTimes(1)
    // The original call stays open until the resume delivers its one result.
    expect(harness.completions).toEqual([])
    expect(harness.confirmations).toEqual([])
  })

  it('routes a fail-closed search to its own confirmation, never a pending action', async () => {
    const harness = createControllerHarness(async () => ({ status: 'needs_confirmation', input: { queryTerms: 'resume' } }))

    await harness.controller.run({ queryTerms: 'resume' }, SERVER_CALL, 'model')

    expect(harness.confirmations).toEqual([{ input: { queryTerms: 'resume' }, serverCall: SERVER_CALL }])
    expect(harness.chooseFolder).not.toHaveBeenCalled()
    // The call is held for the confirmation; nothing is answered yet.
    expect(harness.completions).toEqual([])
    expect(harness.errors).toEqual([undefined])
  })

  it('continues a confirmed search through the orchestrator as an explicit request', async () => {
    const harness = createControllerHarness(async (request) =>
      request.origin === 'user'
        ? { status: 'completed', ...createResults() }
        : { status: 'needs_confirmation', input: { queryTerms: 'resume' } })

    await harness.controller.confirm({ input: { queryTerms: 'resume' }, serverCall: SERVER_CALL })

    // Clears the card, then re-enters begin as an explicit user request.
    expect(harness.confirmations).toEqual([undefined])
    expect(harness.begins).toEqual([{ queryTerms: 'resume', callId: '1:call-1', origin: 'user' }])
    expect(harness.completions).toEqual([
      { callId: 'call-1', result: expect.objectContaining({ ok: true }) }
    ])
  })

  it('declines by answering the held call exactly once', async () => {
    const harness = createControllerHarness(async () => ({ status: 'completed', ...createResults() }))

    harness.controller.decline({ input: { queryTerms: 'resume' }, serverCall: SERVER_CALL })

    expect(harness.confirmations).toEqual([undefined])
    expect(harness.begins).toEqual([])
    expect(harness.completions).toEqual([
      { callId: 'call-1', result: expect.objectContaining({ ok: false }) }
    ])
  })

  it('answers and surfaces the message for a failed search', async () => {
    const harness = createControllerHarness(async () => ({ status: 'failed', message: 'That search request is not valid.' }))

    await harness.controller.run({ queryTerms: '   ' }, SERVER_CALL, 'model')

    expect(harness.completions).toEqual([
      { callId: 'call-1', result: { ok: false, message: 'That search request is not valid.' } }
    ])
    expect(harness.errors).toContain('That search request is not valid.')
  })

  it('answers the resumed call once the main process reports completion', async () => {
    const harness = createControllerHarness(async () => ({ status: 'awaiting_folder', pendingId: 'pending-1' }))

    await harness.controller.run({ queryTerms: 'resume' }, SERVER_CALL, 'model')
    harness.controller.resolve({ status: 'completed', callId: '1:call-1', ...createResults() })

    expect(harness.applied).toHaveLength(1)
    expect(harness.completions).toEqual([
      { callId: 'call-1', result: expect.objectContaining({ ok: true, resultIds: ['result-1'] }) }
    ])
  })

  it('keeps same-ID search resumes isolated by session generation', async () => {
    const harness = createControllerHarness(async () => ({ status: 'awaiting_folder', pendingId: 'pending-1' }))
    const nextGenerationCall = { callId: 'call-1', generation: 2 }

    await harness.controller.run({ queryTerms: 'old resume' }, SERVER_CALL, 'model')
    await harness.controller.run({ queryTerms: 'new resume' }, nextGenerationCall, 'model')
    harness.controller.expireGeneration(1)
    harness.controller.resolve({ status: 'completed', callId: '1:call-1', ...createResults() })
    harness.controller.resolve({ status: 'completed', callId: '2:call-1', ...createResults() })

    expect(harness.completions).toEqual([
      { callId: 'call-1', result: expect.objectContaining({ ok: true }) }
    ])
    expect(harness.begins.map((request) => request.callId)).toEqual(['1:call-1', '2:call-1'])
  })

  it('clears only the expired in-flight generation search loading state', async () => {
    let resolveBegin: ((outcome: FileSearchOutcome) => void) | undefined
    const harness = createControllerHarness(() => new Promise((resolve) => { resolveBegin = resolve }))

    const run = harness.controller.run({ queryTerms: 'old resume' }, SERVER_CALL, 'model')
    expect(harness.searchings).toEqual([true])

    expect(harness.controller.expireGeneration(1)).toBe(true)
    expect(harness.searchings).toEqual([true, false])

    resolveBegin?.({ status: 'completed', ...createResults() })
    await run

    expect(harness.searchings).toEqual([true, false])
    expect(harness.applied).toEqual([])
    expect(harness.completions).toEqual([])
  })

  it('does not let an older stale request clear a newer local search', async () => {
    const resolvers: Array<(outcome: FileSearchOutcome) => void> = []
    const harness = createControllerHarness(() => new Promise((resolve) => { resolvers.push(resolve) }))

    const oldRun = harness.controller.run({ queryTerms: 'old resume' }, SERVER_CALL, 'model')
    harness.controller.expireGeneration(1)
    const localRun = harness.controller.run({ queryTerms: 'local resume' }, undefined, 'user')
    expect(harness.searchings).toEqual([true, false, true])

    resolvers[0]?.({ status: 'completed', ...createResults() })
    await oldRun
    expect(harness.searchings).toEqual([true, false, true])
    expect(harness.applied).toEqual([])

    resolvers[1]?.({ status: 'completed', ...createResults() })
    await localRun
    expect(harness.searchings).toEqual([true, false, true, false])
    expect(harness.applied).toHaveLength(1)
  })
})

// --- Integration: the live regression reproduced end to end ------------------

const SEARCH_QUERY = 'resume'

function flushAsyncWork(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function injectDataChannel(client: RealtimeClient, events: Array<Record<string, unknown>>): void {
  ;(client as unknown as { dataChannel: Pick<RTCDataChannel, 'readyState' | 'send'> }).dataChannel = {
    readyState: 'open',
    send: (value: string) => { events.push(JSON.parse(value) as Record<string, unknown>) }
  } as unknown as Pick<RTCDataChannel, 'readyState' | 'send'>
  ;(client as unknown as { mode: 'live' | 'mock' }).mode = 'live'
  ;(client as unknown as { activeGeneration: number }).activeGeneration = 1
  ;(client as unknown as { dataChannelGeneration: number }).dataChannelGeneration = 1
}

function callHandleServerEvent(client: RealtimeClient, event: unknown): void {
  ;(client as unknown as { handleServerEvent: (serializedEvent: unknown, generation: number) => void }).handleServerEvent.call(client, event, 1)
}

function functionCallOutputs(events: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  return events
    .filter((event) => event.type === 'conversation.item.create')
    .map((event) => (event.item as { output?: string } | undefined)?.output)
    .filter((output): output is string => typeof output === 'string')
    .map((output) => JSON.parse(output) as Record<string, unknown>)
}

function searchCallEvent(callId: string): string {
  return JSON.stringify({
    type: 'response.function_call_arguments.done',
    name: 'search_documents',
    call_id: callId,
    arguments: JSON.stringify({ query_terms: SEARCH_QUERY, recency: 'latest', reason: 'Find the stored resume.' })
  })
}

function transcriptEvent(text: string): string {
  return JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: text })
}

interface IntegrationHarness {
  client: RealtimeClient
  events: Array<Record<string, unknown>>
  order: string[]
  roots: ApprovedDocumentRoot[]
  createPendingAction: ReturnType<typeof vi.fn>
  chooseFolderCount: () => number
  confirmation: () => SearchConfirmationRequest | undefined
  errors: Array<string | undefined>
  run: () => Promise<void>
  confirm: () => Promise<void>
  decline: () => void
}

/**
 * Wires the real RealtimeClient, IntentTracker, SearchOrchestrator, and
 * FileSearchController exactly as the app does, so the routing under test is the
 * production routing and not a re-implementation. `deliverTranscript` models
 * whether the late voice transcript lands inside the orchestrator's grace window.
 */
function createIntegrationHarness(options: { deliverTranscript: boolean }): IntegrationHarness {
  const events: Array<Record<string, unknown>> = []
  const order: string[] = []
  const roots: ApprovedDocumentRoot[] = []
  const errors: Array<string | undefined> = []
  const createPendingAction = vi.fn(async () => { throw new Error('create-pending-action must not run for a search') })
  let chooseFolderCount = 0
  let confirmation: SearchConfirmationRequest | undefined
  let runPromise: Promise<void> | undefined

  const tracker = new IntentTracker()
  const client = new RealtimeClient({
    onState: () => undefined,
    onTranscript: () => undefined,
    onExplanation: () => undefined,
    onCaptureContextRequest: () => undefined,
    onFileSearchRequest: (request, callId) => { runPromise = controller.run(request, callId) },
    onUserTranscript: (text) => { tracker.noteUserRequest(text); order.push('intent-update') },
    onToolProposal: () => undefined,
    onError: (message) => { errors.push(message) }
  })
  injectDataChannel(client, events)

  const orchestrator = new SearchOrchestrator({
    listRoots: async () => roots,
    runSearch: async () => { order.push('search-executed'); return createResults() },
    isTrustedIntent: (query) => tracker.supportsFileSearch(query),
    waitForTrust: async () => {
      // Stands in for a late voice transcript arriving during the grace window.
      if (options.deliverTranscript) {
        callHandleServerEvent(client, transcriptEvent('Find my latest resume'))
        await flushAsyncWork()
      }
    },
    emit: (resolution) => controller.resolve(resolution)
  })

  const controller = new FileSearchController({
    begin: (request) => orchestrator.begin(request),
    chooseFolder: async () => {
      order.push('folder-approval')
      chooseFolderCount += 1
      roots.push({ id: 'root-1', label: 'Documents' })
      await orchestrator.notifyFolderApproved()
    },
    completeCall: (callId, result) => client.completeFileSearch(callId, result),
    applyResults: () => undefined,
    presentConfirmation: (request) => { confirmation = request },
    setSearching: () => undefined,
    setError: (message) => { errors.push(message) },
    setListening: () => { order.push('resume') }
  })

  return {
    client,
    events,
    order,
    roots,
    createPendingAction,
    chooseFolderCount: () => chooseFolderCount,
    confirmation: () => confirmation,
    errors,
    run: async () => {
      order.push('voice-request')
      callHandleServerEvent(client, searchCallEvent('call-voice-1'))
      await flushAsyncWork()
      await runPromise
    },
    confirm: async () => {
      const held = confirmation
      if (!held) throw new Error('No confirmation was presented.')
      await controller.confirm(held)
    },
    decline: () => {
      const held = confirmation
      if (!held) throw new Error('No confirmation was presented.')
      controller.decline(held)
    }
  }
}

describe('folderless voice search regression', () => {
  it('resumes a voice "find my latest resume" through folder approval without a pending action or IPC error', async () => {
    const harness = createIntegrationHarness({ deliverTranscript: true })

    await harness.run()

    // Never routed to the root-assuming create-pending-action path.
    expect(harness.createPendingAction).not.toHaveBeenCalled()
    // The folder chooser opened exactly once and no confirmation card was needed.
    expect(harness.chooseFolderCount()).toBe(1)
    expect(harness.confirmation()).toBeUndefined()
    // Exactly one terminal function_call_output was returned to the model.
    const outputs = functionCallOutputs(harness.events)
    expect(outputs).toHaveLength(1)
    expect(outputs[0]).toMatchObject({ ok: true })
    // No red IPC error surfaced.
    expect(harness.errors.filter((message) => message !== undefined)).toEqual([])
    // Event ordering: request -> intent update -> policy/orchestrator -> approval -> resume.
    expect(harness.order).toEqual(['voice-request', 'intent-update', 'folder-approval', 'search-executed', 'resume'])
  })

  it('falls back to a search-specific confirmation when the transcript never arrives, then resumes on approval', async () => {
    const harness = createIntegrationHarness({ deliverTranscript: false })

    await harness.run()

    // Fail-closed: a confirmation is presented, the call is still held, and no
    // folder chooser or pending action ran.
    expect(harness.confirmation()).toEqual({
      input: { queryTerms: 'resume', kind: 'any', recency: 'latest' },
      serverCall: { callId: 'call-voice-1', generation: 1 }
    })
    expect(harness.chooseFolderCount()).toBe(0)
    expect(harness.createPendingAction).not.toHaveBeenCalled()
    expect(functionCallOutputs(harness.events)).toEqual([])

    await harness.confirm()

    // Confirming continues through the orchestrator: chooser opens once, the
    // original call is answered exactly once, still with no pending action.
    expect(harness.chooseFolderCount()).toBe(1)
    expect(harness.createPendingAction).not.toHaveBeenCalled()
    const outputs = functionCallOutputs(harness.events)
    expect(outputs).toHaveLength(1)
    expect(outputs[0]).toMatchObject({ ok: true })
    expect(harness.errors.filter((message) => message !== undefined)).toEqual([])
  })

  it('answers the held call once when the user declines the fail-closed confirmation', async () => {
    const harness = createIntegrationHarness({ deliverTranscript: false })

    await harness.run()
    expect(harness.confirmation()).toBeDefined()

    harness.decline()

    expect(harness.chooseFolderCount()).toBe(0)
    expect(harness.createPendingAction).not.toHaveBeenCalled()
    const outputs = functionCallOutputs(harness.events)
    expect(outputs).toHaveLength(1)
    expect(outputs[0]).toMatchObject({ ok: false })
  })
})
