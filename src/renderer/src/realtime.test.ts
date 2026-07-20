import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CaptureResult, Explanation, SearchDocumentsInput, ToolProposal } from '../../shared/contracts'
import { IntentTracker } from '../../main/services/intent-policy'
import { normalizeSearchQuery } from '../../shared/search-query'
import {
  COLLAPSE_DISCONNECT_MS,
  IDLE_DISCONNECT_MS,
  LAPTOP_MIC_CONSTRAINTS,
  MAX_PENDING_WORK_EXTENSION_MS,
  RealtimeClient,
  type RealtimeServerCall
} from './realtime'

const originalWindow = globalThis.window
const testClients: RealtimeClient[] = []

afterEach(() => {
  if (!globalThis.window) {
    installTimerWindow()
  }
  for (const client of testClients.splice(0)) {
    client.disconnect()
  }
  vi.useRealTimers()
  vi.restoreAllMocks()
  globalThis.window = originalWindow
})

function flushAsyncWork(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

type Callbacks = ConstructorParameters<typeof RealtimeClient>[0]

/** Every callback a test does not care about is a no-op. */
function createClient(overrides: Partial<Callbacks> = {}): RealtimeClient {
  const client = new RealtimeClient({
    onState: () => undefined,
    onTranscript: () => undefined,
    onExplanation: () => undefined,
    onCaptureContextRequest: () => undefined,
    onFileSearchRequest: () => undefined,
    onToolProposal: () => undefined,
    onError: () => undefined,
    ...overrides
  })
  testClients.push(client)
  return client
}

function injectDataChannel(client: RealtimeClient, events: Array<Record<string, unknown>>): void {
  ;(client as unknown as { dataChannel: Pick<RTCDataChannel, 'readyState' | 'send'> }).dataChannel = {
    readyState: 'open',
    send: (value: string) => { events.push(JSON.parse(value) as Record<string, unknown>) },
    close: () => undefined
  } as unknown as Pick<RTCDataChannel, 'readyState' | 'send'>
}

function installTimerWindow(): void {
  globalThis.window = { setTimeout, clearTimeout, speechSynthesis: undefined } as unknown as Window & typeof globalThis
}

function setLiveMode(client: RealtimeClient): void {
  if (!globalThis.window || typeof globalThis.window.setTimeout !== 'function') {
    installTimerWindow()
  }
  const internals = client as unknown as {
    mode: 'live' | 'mock'
    activeGeneration?: number
    dataChannelGeneration?: number
  }
  internals.mode = 'live'
  internals.activeGeneration ??= 1
  internals.dataChannelGeneration ??= internals.activeGeneration
}

function callHandleServerEvent(client: RealtimeClient, event: unknown): void {
  if (!(client as unknown as { dataChannel?: RTCDataChannel }).dataChannel) {
    injectDataChannel(client, [])
  }
  setLiveMode(client)
  const generation = (client as unknown as { activeGeneration: number }).activeGeneration
  const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown, generation: number) => void }).handleServerEvent
  handleServerEvent.call(client, event, generation)
}

function registerServerCall(client: RealtimeClient, callId: string): RealtimeServerCall {
  setLiveMode(client)
  const generation = (client as unknown as { activeGeneration: number }).activeGeneration
  const serverCall = { callId, generation }
  ;(client as unknown as { pendingCallGenerations: Map<string, number> }).pendingCallGenerations.set(callId, generation)
  return serverCall
}

function activateGeneration(client: RealtimeClient, generation: number): void {
  const internals = client as unknown as {
    mode: 'live' | 'mock'
    connected: boolean
    activeGeneration: number
    dataChannelGeneration: number
  }
  internals.mode = 'live'
  internals.connected = true
  internals.activeGeneration = generation
  internals.dataChannelGeneration = generation
}

function functionCallOutputs(events: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  return events
    .filter((event) => event.type === 'conversation.item.create')
    .map((event) => (event.item as { output?: string } | undefined)?.output)
    .filter((output): output is string => typeof output === 'string')
    .map((output) => JSON.parse(output) as Record<string, unknown>)
}

function toolCallEvent(name: string, callId: string, args: unknown): string {
  return JSON.stringify({
    type: 'response.function_call_arguments.done',
    name,
    call_id: callId,
    arguments: typeof args === 'string' ? args : JSON.stringify(args)
  })
}

describe('RealtimeClient server events', () => {
  it('ignores duplicate completed tool-call events', () => {
    const proposals: string[] = []
    const client = createClient({ onToolProposal: (proposal) => { proposals.push(proposal.callId ?? '') } })
    const event = toolCallEvent('open_url', 'call-1', { url: 'https://example.com', reason: 'Open the displayed website.' })

    callHandleServerEvent(client, event)
    callHandleServerEvent(client, event)

    expect(proposals).toEqual(['call-1'])
  })

  it('routes one internal screen-context request without creating an external proposal', () => {
    const captureCalls: Array<string | undefined> = []
    const proposals: ToolProposal[] = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureCalls.push(serverCall?.callId) },
      onToolProposal: (proposal) => { proposals.push(proposal) }
    })
    const event = toolCallEvent('capture_screen_context', 'capture-call-1', '{}')

    callHandleServerEvent(client, event)
    callHandleServerEvent(client, event)

    expect(captureCalls).toEqual(['capture-call-1'])
    expect(proposals).toEqual([])
  })

  it('routes Telegram recipient lookup locally without exposing any recipient metadata', () => {
    const searches: Array<{ query: string; callId: string }> = []
    const client = createClient({
      onTelegramRecipientSearch: (query, serverCall) => { searches.push({ query, callId: serverCall.callId }) }
    })

    callHandleServerEvent(client, toolCallEvent('telegram_search_recipients', 'telegram-search-1', { query: 'Ravi' }))

    expect(searches).toEqual([{ query: 'Ravi', callId: 'telegram-search-1' }])
  })

  it('resolves selected and ordinal Telegram attachments locally with an exact caption', () => {
    const requests: Array<{ fileResultId: string; recipientQuery: string; caption?: string }> = []
    const events: Array<Record<string, unknown>> = []
    const client = createClient({
      onTelegramAttachmentRequest: (request) => { requests.push(request) }
    })
    injectDataChannel(client, events)
    client.setSearchResults([
      { id: 'private-result-a', kind: 'screenshot' },
      { id: 'private-result-b', kind: 'screenshot' }
    ])
    callHandleServerEvent(client, toolCallEvent('telegram_send_attachment', 'attachment-ordinal', {
      attachment: '2',
      recipient_query: 'Kesava',
      caption: '  exact caption  ',
      reason: 'Send the second screenshot.'
    }))

    ;(client as unknown as { selectedPhoto: { resultId: string; name: string }; lastUserRequest: string }).selectedPhoto = {
      resultId: 'private-selected-photo', name: 'life.jpg'
    }
    ;(client as unknown as { lastUserRequest: string }).lastUserRequest = 'Send this picture to Kesava.'
    callHandleServerEvent(client, toolCallEvent('telegram_send_attachment', 'attachment-selected', {
      attachment: 'selected', recipient_query: 'Kesava', reason: 'Send this picture.'
    }))

    expect(requests).toEqual([
      { fileResultId: 'private-result-b', recipientQuery: 'Kesava', caption: '  exact caption  ', reason: 'Send the second screenshot.' },
      { fileResultId: 'private-selected-photo', recipientQuery: 'Kesava', caption: undefined, reason: 'Send this picture.' }
    ])
    expect(JSON.stringify(events)).not.toContain('private-result')
    expect(JSON.stringify(events)).not.toContain('private-selected-photo')
  })

  it('asks one clarification for an ambiguous selected attachment and never emits local IDs', () => {
    const events: Array<Record<string, unknown>> = []
    const requests: unknown[] = []
    const client = createClient({ onTelegramAttachmentRequest: (request) => { requests.push(request) } })
    injectDataChannel(client, events)
    client.setSearchResults([{ id: 'secret-a', kind: 'document' }, { id: 'secret-b', kind: 'document' }])
    ;(client as unknown as { lastUserRequest: string }).lastUserRequest = 'Send this document to Kesava.'

    callHandleServerEvent(client, toolCallEvent('telegram_send_attachment', 'attachment-ambiguous', {
      attachment: 'selected', recipient_query: 'Kesava', reason: 'Send it.'
    }))

    expect(requests).toEqual([])
    expect(functionCallOutputs(events)).toEqual([expect.objectContaining({ ok: false, message: expect.stringMatching(/which one/i) })])
    expect(JSON.stringify(events)).not.toContain('secret-a')
    expect(JSON.stringify(events)).not.toContain('secret-b')
  })

  it('uses a flat bounded string schema for attachment references', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession

    configureLiveSession.call(client)

    const session = events[0]!.session as { tools: Array<Record<string, unknown>> }
    const tool = session.tools.find((candidate) => candidate.name === 'telegram_send_attachment')!
    const parameters = tool.parameters as { properties: { attachment: Record<string, unknown> } }
    expect(parameters.properties.attachment).toEqual({
      type: 'string',
      enum: ['selected', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10'],
      description: expect.any(String)
    })
    expect(JSON.stringify(tool)).not.toContain('oneOf')
  })

  it('rejects invalid attachment strings without echoing paths or trusted-looking IDs', () => {
    const events: Array<Record<string, unknown>> = []
    const requests: unknown[] = []
    const client = createClient({ onTelegramAttachmentRequest: (request) => { requests.push(request) } })
    injectDataChannel(client, events)
    client.setSearchResults([{ id: 'trusted-result-1', kind: 'document' }, { id: 'trusted-result-2', kind: 'document' }])
    const invalid: unknown[] = ['0', '11', 2, 'C:\\Users\\person\\secret.pdf', 'trusted-result-2', 'resume.pdf', 'arbitrary']

    invalid.forEach((attachment, index) => {
      callHandleServerEvent(client, toolCallEvent('telegram_send_attachment', `invalid-attachment-${index}`, {
        attachment, recipient_query: 'Kesava', reason: 'Send it.'
      }))
    })

    expect(requests).toEqual([])
    const outputs = functionCallOutputs(events)
    expect(outputs).toHaveLength(invalid.length)
    expect(outputs.every((output) => output.ok === false && /selected file or a result number/i.test(String(output.message)))).toBe(true)
    const serialized = JSON.stringify(events)
    expect(serialized).not.toContain('trusted-result-1')
    expect(serialized).not.toContain('trusted-result-2')
    expect(serialized).not.toContain('secret.pdf')
  })

  it('never auto-selects a fallback recent possibility', () => {
    const events: Array<Record<string, unknown>> = []
    const requests: unknown[] = []
    const client = createClient({ onTelegramAttachmentRequest: (request) => { requests.push(request) } })
    injectDataChannel(client, events)
    client.setSearchResults([{ id: 'fallback-id', kind: 'document' }], true)
    ;(client as unknown as { lastUserRequest: string }).lastUserRequest = 'Send my latest resume to Kesava.'

    callHandleServerEvent(client, toolCallEvent('telegram_send_attachment', 'attachment-fallback', {
      attachment: 'selected', recipient_query: 'Kesava', reason: 'Send resume.'
    }))

    expect(requests).toEqual([])
    expect(functionCallOutputs(events)[0]).toMatchObject({ ok: false })
    expect(JSON.stringify(events)).not.toContain('fallback-id')
  })

  it('sends a complete session payload and waits for session.updated before greeting', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession

    configureLiveSession.call(client)

    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      type: 'session.update',
      session: { type: 'realtime', tool_choice: 'auto', max_output_tokens: 1024 }
    })
    const session = events[0]?.session as Record<string, unknown>
    expect(session.model).toBeUndefined()
    expect(session.reasoning).toBeUndefined()
    // Completed input transcription is what feeds the trusted intent tracker.
    expect(session.audio).toMatchObject({
      input: {
        noise_reduction: { type: 'far_field' },
        transcription: { model: 'gpt-4o-mini-transcribe' },
        turn_detection: {
          type: 'server_vad',
          threshold: 0.7,
          prefix_padding_ms: 300,
          silence_duration_ms: 650,
          create_response: true,
          interrupt_response: true
        }
      }
    })

    callHandleServerEvent(client, JSON.stringify({ type: 'session.updated' }))

    expect(events[1]).toMatchObject({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: 192 }
    })
  })

  it('does not repeat the greeting when a reconnect opts out', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    ;(client as unknown as { greetAfterInitialSessionUpdate: boolean }).greetAfterInitialSessionUpdate = false
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession

    configureLiveSession.call(client)
    callHandleServerEvent(client, JSON.stringify({ type: 'session.updated' }))

    expect(events.filter((event) => event.type === 'response.create')).toEqual([])
  })

  it('skips the initial greeting when an input turn has already started', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession

    configureLiveSession.call(client)
    callHandleServerEvent(client, JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    callHandleServerEvent(client, JSON.stringify({ type: 'session.updated' }))

    expect(events.filter((event) => event.type === 'response.create')).toEqual([])
  })

  it('deduplicates unchanged instructions and sends only instructions after the initial update', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession

    configureLiveSession.call(client)
    const roots = [{ id: 'root-1', label: 'Documents' }]
    client.setApprovedRoots(roots)
    client.setApprovedRoots(roots)

    const updates = events.filter((event) => event.type === 'session.update')
    expect(updates).toHaveLength(2)
    const incremental = updates[1]?.session as Record<string, unknown>
    expect(incremental.instructions).toEqual(expect.any(String))
    expect(incremental.tools).toBeUndefined()
    expect(incremental.audio).toBeUndefined()
  })

  it('uses a long-form ceiling for detailed typed requests', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { connected: boolean }).connected = true

    await client.sendUserRequest('Explain this page in detail')

    expect(events.at(-1)).toMatchObject({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: 2048 }
    })
  })

  it('uses the normal ceiling for a simple typed request', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { connected: boolean }).connected = true

    await client.sendUserRequest('What time is it?')

    expect(events.at(-1)).toMatchObject({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: 512 }
    })
  })

  it('keeps selected capture bytes out of Realtime and sends only the validated review text', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { connected: boolean }).connected = true
    const capture: CaptureResult = {
      id: 'capture-budget',
      sourceId: 'screen:1:0',
      sourceKind: 'screen',
      label: 'Primary screen',
      dataUrl: 'data:image/jpeg;base64,AA==',
      mimeType: 'image/jpeg',
      width: 1_600,
      height: 900,
      capturedAt: '2026-07-19T09:00:00.000Z'
    }

    await client.provideScreenContext(capture)

    expect(JSON.stringify(events)).not.toContain(capture.dataUrl)
    expect(JSON.stringify(events)).not.toContain('input_image')

    client.provideScreenReview({
      sourceCaptureId: capture.id,
      summary: 'Interview invitation with a deadline tomorrow.',
      dates: ['Tomorrow'],
      links: ['https://example.com/interview'],
      risks: ['The deadline is tomorrow.'],
      nextActions: ['Review the interview details.']
    })

    const serializedEvents = JSON.stringify(events)
    expect(serializedEvents).toContain('Interview invitation with a deadline tomorrow.')
    expect(serializedEvents).toContain('https://example.com/interview')
    expect(serializedEvents).not.toContain(capture.dataUrl)
    expect(serializedEvents).not.toContain('input_image')
    expect(events.at(-1)).toMatchObject({
      type: 'response.create',
      response: { max_output_tokens: 2048 }
    })
  })

  it('uses the search-results ceiling after a file-search function-call output', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)

    client.completeFileSearch(registerServerCall(client, 'search-budget'), {
      ok: true,
      message: 'Found one file.',
      compactResults: [{ ordinal: 1, name: 'Resume.pdf', modifiedAgo: 'today' }],
      resultCount: 1
    })

    expect(events.at(-1)).toMatchObject({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: 512 }
    })
  })

  it('bounds search narration to three safe filenames while preserving the full UI result count', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    const veryLongName = 'Quarterly_Implementation_Status_and_Architecture_Review_for_Realtime_Cost_Reduction_Phase_A_Final_Draft.pdf'

    client.completeFileSearch(registerServerCall(client, 'search-narration'), {
      ok: true,
      message: 'Found 4 matching files.',
      compactResults: [
        { ordinal: 1, name: 'Resume.pdf', modifiedAgo: 'today' },
        { ordinal: 2, name: veryLongName, modifiedAgo: 'yesterday' },
        { ordinal: 3, name: 'Portfolio.pdf', modifiedAgo: '2 days ago' },
        { ordinal: 4, name: 'Certificate.pdf', modifiedAgo: '3 days ago' }
      ],
      resultCount: 4
    })

    const output = functionCallOutputs(events)[0]!
    const results = output.results as Array<{ ordinal: number; name: string }>
    expect(results).toHaveLength(3)
    expect(results.map((entry) => entry.ordinal)).toEqual([1, 2, 3])
    expect(results[1]?.name).toMatch(/…$/)
    expect(results[1]?.name).not.toContain('Final_Draft')
    expect(output.message).toContain('4 total results')
    expect(output.message).toContain('complete list is visible in the UI')
    expect(output.message).toContain('Would you like to hear more results?')
    expect(events.at(-1)).toMatchObject({
      response: {
        max_output_tokens: 512,
        instructions: expect.stringContaining('Speak exactly this short search summary')
      }
    })
  })

  it('keeps laptop microphone echo cancellation, noise suppression, and gain control enabled', () => {
    expect(LAPTOP_MIC_CONSTRAINTS).toEqual({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    })
  })

  it('keeps volatile capture timestamps out of the long-lived session instructions', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { currentCapture: CaptureResult }).currentCapture = {
      id: 'capture-1',
      sourceId: 'screen:1:0',
      sourceKind: 'screen',
      label: 'Primary screen',
      dataUrl: 'data:image/jpeg;base64,AA==',
      mimeType: 'image/jpeg',
      width: 560,
      height: 315,
      capturedAt: new Date().toISOString()
    }
    const sendSessionUpdate = (client as unknown as { sendSessionUpdate: () => void }).sendSessionUpdate

    sendSessionUpdate.call(client)

    const instructions = String((events[0]?.session as Record<string, unknown>).instructions)
    expect(instructions).toMatch(/screen context/i)
    expect(instructions).not.toMatch(/\d{4}-\d{2}-\d{2}T/)
  })

  it('keeps the deterministic no-key mock capture flow intact', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const transcripts: string[] = []
    const explanations: Explanation[] = []
    const proposals: ToolProposal[] = []
    const client = createClient({
      onTranscript: (text) => { transcripts.push(text) },
      onExplanation: (explanation) => { explanations.push(explanation) },
      onToolProposal: (proposal) => { proposals.push(proposal) }
    })
    const capture: CaptureResult = {
      id: 'capture-1',
      sourceId: 'screen:1:0',
      sourceKind: 'screen',
      label: 'Primary screen',
      dataUrl: 'data:image/jpeg;base64,AA==',
      mimeType: 'image/jpeg',
      width: 560,
      height: 315,
      capturedAt: '2026-07-18T09:00:00.000Z'
    }

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.provideScreenContext(capture)

    expect(transcripts).toContain('Hi, I am Lumi. I am ready to look at a screen with you.')
    expect(explanations[0]?.sourceCaptureId).toBe(capture.id)
    expect(proposals[0]).toMatchObject({ toolName: 'create_reminder', requiresConfirmation: true })
  })

  it('rejects capture_screen_context for local-file intent without opening the source picker', async () => {
    const captureRequests: Array<string | undefined> = []
    const events: Array<Record<string, unknown>> = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      evaluateToolPolicy: async () => ({
        allowed: false,
        code: 'use_search_documents',
        message: 'The user asked to find a stored file; call search_documents instead.'
      })
    })
    injectDataChannel(client, events)
    setLiveMode(client)

    callHandleServerEvent(client, toolCallEvent('capture_screen_context', 'capture-blocked-1', '{}'))
    await flushAsyncWork()

    expect(captureRequests).toEqual([])
    expect(functionCallOutputs(events)).toEqual([
      expect.objectContaining({ ok: false, code: 'use_search_documents' })
    ])
  })

  it('allows capture_screen_context when the trusted policy approves it', async () => {
    const captureRequests: Array<string | undefined> = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      evaluateToolPolicy: async () => ({ allowed: true })
    })

    callHandleServerEvent(client, toolCallEvent('capture_screen_context', 'capture-allowed-1', '{}'))
    await flushAsyncWork()

    expect(captureRequests).toEqual(['capture-allowed-1'])
  })
})

describe('RealtimeClient stored-file search', () => {
  it('accepts a search call that carries no folder identifier', async () => {
    const requests: Array<{ request: SearchDocumentsInput; callId?: string }> = []
    const proposals: ToolProposal[] = []
    const client = createClient({
      onFileSearchRequest: (request, serverCall) => { requests.push({ request, callId: serverCall?.callId }) },
      onToolProposal: (proposal) => { proposals.push(proposal) }
    })

    callHandleServerEvent(client, toolCallEvent('search_documents', 'search-1', {
      query_terms: 'resume',
      recency: 'latest',
      reason: 'Find the stored resume.'
    }))
    await flushAsyncWork()

    expect(requests).toEqual([
      { request: { queryTerms: 'resume', kind: undefined, recency: 'latest' }, callId: 'search-1' }
    ])
    // The search is not a confirmation-card proposal by default.
    expect(proposals).toEqual([])
  })

  it('does not answer the search call while the request is held for folder approval', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)

    callHandleServerEvent(client, toolCallEvent('search_documents', 'search-held-1', {
      query_terms: 'resume',
      reason: 'Find the stored resume.'
    }))
    await flushAsyncWork()

    expect(functionCallOutputs(events)).toEqual([])
  })

  it('returns one terminal result per call and never a second', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)

    const serverCall = registerServerCall(client, 'search-1')
    client.completeFileSearch(serverCall, {
      ok: true,
      message: 'Found 1 matching file.',
      compactResults: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }],
      resultIds: ['result-1']
    })
    client.completeFileSearch(serverCall, { ok: false, message: 'A duplicate result.' })

    expect(functionCallOutputs(events)).toEqual([
      expect.objectContaining({ ok: true, results: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }] })
    ])
  })

  it('sends no identifier, root, or path to the model with search results', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)

    client.completeFileSearch(registerServerCall(client, 'search-1'), {
      ok: true,
      message: 'Found 1 matching file.',
      compactResults: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }],
      resultIds: ['0f8c4a1e-1d2b-4a6f-9a1c-3f5b7c9d0e11']
    })

    const serialized = JSON.stringify(events)
    expect(serialized).not.toContain('0f8c4a1e-1d2b-4a6f-9a1c-3f5b7c9d0e11')
    expect(serialized).not.toContain('rootId')
    expect(serialized).not.toContain('relativePath')
    expect(serialized).not.toContain('C:\\')
    expect(serialized).toContain('Resume_2026.pdf')
  })

  it('resolves "open the second one" to a local result identifier', () => {
    const proposals: ToolProposal[] = []
    const client = createClient({ onToolProposal: (proposal) => { proposals.push(proposal) } })
    client.setSearchOrdinals(['result-a', 'result-b', 'result-c'])

    callHandleServerEvent(client, toolCallEvent('open_file', 'open-1', { ordinal: 2, reason: 'Open the second result.' }))

    expect(proposals).toEqual([
      expect.objectContaining({ toolName: 'open_file', requiresConfirmation: true, arguments: { resultId: 'result-b' } })
    ])
  })

  it('refuses an ordinal that no search produced', async () => {
    const events: Array<Record<string, unknown>> = []
    const errors: string[] = []
    const client = createClient({ onError: (message) => { errors.push(message) } })
    injectDataChannel(client, events)
    setLiveMode(client)

    callHandleServerEvent(client, toolCallEvent('open_file', 'open-2', { ordinal: 4, reason: 'Open it.' }))
    await flushAsyncWork()

    expect(errors[0]).toMatch(/does not exist/i)
    expect(functionCallOutputs(events)).toEqual([expect.objectContaining({ ok: false })])
  })

  it('starts a local-file search for "Open my resume" in mock mode instead of capturing', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const requests: SearchDocumentsInput[] = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      onFileSearchRequest: (request) => { requests.push(request) }
    })
    client.setApprovedRoots([{ id: 'root-1', label: 'Documents' }])

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('Open my resume')

    expect(captureRequests).toEqual([])
    expect(requests).toEqual([{ queryTerms: 'resume' }])
  })

  it('asks for a search rather than folder details when no folder is approved in mock mode', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const requests: SearchDocumentsInput[] = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      onFileSearchRequest: (request) => { requests.push(request) }
    })

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('Find my latest resume')

    expect(captureRequests).toEqual([])
    expect(requests).toEqual([{ queryTerms: 'resume' }])
  })

  it('searches for the newest screenshot without opening the screen-source picker', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const requests: SearchDocumentsInput[] = []
    const client = createClient({
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      onFileSearchRequest: (request) => { requests.push(request) }
    })

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('Find my newest screenshot')

    expect(captureRequests).toEqual([])
    expect(requests).toEqual([{ queryTerms: 'screenshot' }])
  })

  it('passes a photo search through with its kind and recency intact', async () => {
    const requests: Array<{ request: SearchDocumentsInput; callId?: string }> = []
    const client = createClient({
      onFileSearchRequest: (request, serverCall) => { requests.push({ request, callId: serverCall?.callId }) }
    })

    callHandleServerEvent(client, toolCallEvent('search_documents', 'search-photo-1', {
      query_terms: 'ravi beach',
      kind: 'photo',
      reason: 'Find the beach photo.'
    }))
    await flushAsyncWork()

    expect(requests).toEqual([
      { request: { queryTerms: 'ravi beach', kind: 'photo', recency: undefined }, callId: 'search-photo-1' }
    ])
  })

  it('asks the clarification question for the ambiguous "Check my resume" in mock mode', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const transcripts: string[] = []
    const client = createClient({
      onTranscript: (text) => { transcripts.push(text) },
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) }
    })

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('Check my resume')

    expect(captureRequests).toEqual([])
    expect(transcripts).toContain('Should I inspect the resume currently visible, or find it in your approved folder?')
  })

  it('uses mock screen context for follow-up questions instead of requesting another capture', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const client = createClient({ onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) } })
    const capture: CaptureResult = {
      id: 'capture-2',
      sourceId: 'screen:1:0',
      sourceKind: 'screen',
      label: 'Primary screen',
      dataUrl: 'data:image/jpeg;base64,AA==',
      mimeType: 'image/jpeg',
      width: 560,
      height: 315,
      capturedAt: new Date().toISOString()
    }

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('What is this email about?')
    await client.provideScreenContext(capture)
    await client.sendUserRequest('When is the interview?')

    expect(captureRequests).toEqual([undefined])
  })
})

describe('RealtimeClient selected-photo analysis', () => {
  const approvedImage = {
    resultId: 'result-photo-1',
    name: 'beach.jpg',
    dataUrl: 'data:image/jpeg;base64,QkVBQ0g=',
    mimeType: 'image/jpeg' as const,
    width: 800,
    height: 600
  }

  function connectedClient(events: Array<Record<string, unknown>>, overrides: Partial<Callbacks> = {}): RealtimeClient {
    const client = createClient(overrides)
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { connected: boolean }).connected = true
    return client
  }

  function imageContents(events: Array<Record<string, unknown>>): unknown[] {
    return events
      .filter((event) => event.type === 'conversation.item.create')
      .flatMap((event) => ((event.item as { content?: unknown[] } | undefined)?.content ?? []))
      .filter((content) => (content as { type?: string }).type === 'input_image')
  }

  it('sends exactly one image with the user question when a photo is approved', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)

    await client.analyzeSelectedPhoto(approvedImage, 'Who is in this photo?')

    const images = imageContents(events)
    expect(images).toEqual([{ type: 'input_image', image_url: approvedImage.dataUrl, detail: 'low' }])
    expect(JSON.stringify(events)).toContain('Who is in this photo?')
    expect(client.hasSelectedPhoto()).toBe(true)
    expect(events.at(-1)).toMatchObject({
      type: 'response.create',
      response: { max_output_tokens: 2048 }
    })
  })

  it('answers follow-up questions without uploading the image again', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)

    await client.analyzeSelectedPhoto(approvedImage, 'What is in this photo?')
    await client.sendUserRequest('And what colour is the umbrella?')

    expect(imageContents(events)).toHaveLength(1)
    expect(client.hasSelectedPhoto()).toBe(true)
  })

  it('replaces the selected photo when the user chooses another one', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)

    await client.analyzeSelectedPhoto(approvedImage, 'What is this?')
    await client.analyzeSelectedPhoto({ ...approvedImage, resultId: 'result-photo-2', name: 'hills.jpg' }, 'And this?')

    expect(imageContents(events)).toHaveLength(2)
  })

  it('clears the selected photo explicitly and on disconnect', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)
    await client.analyzeSelectedPhoto(approvedImage, 'What is this?')
    client.clearSelectedPhoto()
    expect(client.hasSelectedPhoto()).toBe(false)

    await client.analyzeSelectedPhoto(approvedImage, 'What is this?')
    client.disconnect()
    expect(client.hasSelectedPhoto()).toBe(false)
  })

  it('keeps the photo filename out of the cacheable session instructions', async () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)

    await client.analyzeSelectedPhoto(approvedImage, 'What is this?')

    const sessionUpdates = events.filter((event) => event.type === 'session.update')
    const instructions = sessionUpdates.map((event) => String((event.session as Record<string, unknown>).instructions ?? ''))
    expect(instructions.some((text) => text.includes('selected one photo'))).toBe(true)
    expect(instructions.every((text) => !text.includes('beach.jpg'))).toBe(true)
  })

  it('refuses a photo analysis the model tried to start itself', () => {
    const events: Array<Record<string, unknown>> = []
    const proposals: ToolProposal[] = []
    const client = connectedClient(events, { onToolProposal: (proposal) => { proposals.push(proposal) } })

    callHandleServerEvent(client, toolCallEvent('analyze_photo', 'photo-call-1', {
      result_id: 'result-photo-1',
      reason: 'Look at the photo.'
    }))

    expect(proposals).toEqual([])
    expect(imageContents(events)).toEqual([])
  })

  it('never sends a local thumbnail to the model with search results', () => {
    const events: Array<Record<string, unknown>> = []
    const client = connectedClient(events)

    client.completeFileSearch(registerServerCall(client, 'search-photos-1'), {
      ok: true,
      message: 'Found 2 matching files.',
      compactResults: [
        { ordinal: 1, name: 'Screenshot 2026-07-18.png', modifiedAgo: 'yesterday' },
        { ordinal: 2, name: 'beach.jpg', modifiedAgo: '3 days ago' }
      ],
      resultIds: ['result-photo-1', 'result-photo-2']
    })

    const serialized = JSON.stringify(events)
    expect(serialized).not.toContain('data:image')
    expect(serialized).not.toContain('thumbnail')
    expect(serialized).toContain('Screenshot 2026-07-18.png')
  })

  it('refuses to analyse a photo before the session is connected', async () => {
    const client = createClient()

    await expect(client.analyzeSelectedPhoto(approvedImage, 'What is this?')).rejects.toThrow(/connect voice/i)
  })
})

describe('RealtimeClient voice intent plumbing', () => {
  it('classifies a completed transcript before it runs a guarded tool call', async () => {
    const order: string[] = []
    const client = createClient({
      onUserTranscript: async (text) => { order.push(`transcript:${text}`) },
      onFileSearchRequest: () => { order.push('search') }
    })

    callHandleServerEvent(client, JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed',
      transcript: 'Find my latest resume'
    }))
    callHandleServerEvent(client, toolCallEvent('search_documents', 'search-voice-1', {
      query_terms: 'resume',
      reason: 'Find the stored resume.'
    }))
    await flushAsyncWork()

    expect(order).toEqual(['transcript:Find my latest resume', 'search'])
  })

  it('stops a spoken local-file request from opening the screen-source picker', async () => {
    const tracker = new IntentTracker()
    const captureRequests: Array<string | undefined> = []
    const events: Array<Record<string, unknown>> = []
    const client = createClient({
      onUserTranscript: async (text) => { tracker.noteUserRequest(text) },
      onCaptureContextRequest: (serverCall) => { captureRequests.push(serverCall?.callId) },
      // The renderer delegates to the same trusted main-process policy.
      evaluateToolPolicy: async (toolName) => tracker.evaluateToolRequest(toolName, true)
    })
    injectDataChannel(client, events)
    setLiveMode(client)

    callHandleServerEvent(client, JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed',
      transcript: 'Find my latest resume'
    }))
    await flushAsyncWork()
    callHandleServerEvent(client, toolCallEvent('capture_screen_context', 'capture-voice-1', '{}'))
    await flushAsyncWork()

    expect(captureRequests).toEqual([])
    expect(functionCallOutputs(events)).toEqual([
      expect.objectContaining({ ok: false, code: 'use_search_documents' })
    ])
    expect(tracker.supportsFileSearch(normalizeSearchQuery({ queryTerms: 'resume' }))).toBe(true)
  })

  it('ignores an empty transcript', async () => {
    const transcripts: string[] = []
    const client = createClient({ onUserTranscript: (text) => { transcripts.push(text) } })

    callHandleServerEvent(client, JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed',
      transcript: '   '
    }))
    await flushAsyncWork()

    expect(transcripts).toEqual([])
  })
})

describe('RealtimeClient session generations', () => {
  it('rejects an old tool result after manual reconnect and accepts the current call with the same ID', () => {
    const firstEvents: Array<Record<string, unknown>> = []
    const secondEvents: Array<Record<string, unknown>> = []
    const proposals: Array<{ proposal: ToolProposal; serverCall: RealtimeServerCall }> = []
    const client = createClient({
      onToolProposal: (proposal, serverCall) => {
        if (serverCall) proposals.push({ proposal, serverCall })
      }
    })
    injectDataChannel(client, firstEvents)
    activateGeneration(client, 1)
    callHandleServerEvent(client, toolCallEvent('open_url', 'same-call', { url: 'https://old.example', reason: 'Open old.' }))
    const old = proposals[0]!

    client.disconnect()
    injectDataChannel(client, secondEvents)
    activateGeneration(client, 2)
    callHandleServerEvent(client, toolCallEvent('open_url', 'same-call', { url: 'https://new.example', reason: 'Open new.' }))
    const current = proposals[1]!

    client.sendToolResult(old.proposal, { ok: true, message: 'Old result.' }, old.serverCall)
    expect(secondEvents).toEqual([])
    client.sendToolResult(current.proposal, { ok: true, message: 'Current result.' }, current.serverCall)
    expect(functionCallOutputs(secondEvents)).toEqual([expect.objectContaining({ message: 'Current result.' })])
  })

  it('silently drops an async search completion from the previous generation', async () => {
    const firstEvents: Array<Record<string, unknown>> = []
    const secondEvents: Array<Record<string, unknown>> = []
    let oldCall: RealtimeServerCall | undefined
    const client = createClient({ onFileSearchRequest: (_request, serverCall) => { oldCall = serverCall } })
    injectDataChannel(client, firstEvents)
    activateGeneration(client, 1)
    callHandleServerEvent(client, toolCallEvent('search_documents', 'old-search', {
      query_terms: 'resume', reason: 'Find it.'
    }))
    await flushAsyncWork()
    expect(oldCall).toBeDefined()

    client.disconnect()
    injectDataChannel(client, secondEvents)
    activateGeneration(client, 2)
    client.completeFileSearch(oldCall, { ok: true, message: 'Late old result.' })

    expect(secondEvents).toEqual([])
  })

  it('silently drops a captured image selected for the previous generation', async () => {
    const firstEvents: Array<Record<string, unknown>> = []
    const secondEvents: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, firstEvents)
    activateGeneration(client, 1)
    const oldCall = registerServerCall(client, 'old-capture')
    client.disconnect()
    injectDataChannel(client, secondEvents)
    activateGeneration(client, 2)

    await client.provideScreenContext({
      id: 'capture-old',
      sourceId: 'screen:1:0',
      sourceKind: 'screen',
      label: 'Old screen',
      dataUrl: 'data:image/jpeg;base64,AA==',
      mimeType: 'image/jpeg',
      width: 100,
      height: 100,
      capturedAt: new Date().toISOString()
    }, oldCall)

    expect(secondEvents).toEqual([])
  })

  it('invalidates pending calls on connection error before a reconnect', () => {
    const ended: Array<{ reason: string; generation: number }> = []
    const firstEvents: Array<Record<string, unknown>> = []
    const secondEvents: Array<Record<string, unknown>> = []
    const client = createClient({
      onSessionEnded: (reason, generation) => { ended.push({ reason, generation }) }
    })
    injectDataChannel(client, firstEvents)
    activateGeneration(client, 1)
    const oldCall = registerServerCall(client, 'error-call')
    const failLiveConnection = (client as unknown as { failLiveConnection: (generation: number) => void }).failLiveConnection

    failLiveConnection.call(client, 1)
    injectDataChannel(client, secondEvents)
    activateGeneration(client, 2)
    client.completeFileSearch(oldCall, { ok: true, message: 'Late after error.' })

    expect(ended).toEqual([{ reason: 'error', generation: 1 }])
    expect(secondEvents).toEqual([])
  })
})

describe('RealtimeClient cost-saving lifecycle', () => {
  function timedLiveClient(
    overrides: Partial<Callbacks> = {}
  ): { client: RealtimeClient; events: Array<Record<string, unknown>>; close: ReturnType<typeof vi.fn> } {
    installTimerWindow()
    const events: Array<Record<string, unknown>> = []
    const close = vi.fn()
    const client = createClient(overrides)
    ;(client as unknown as { dataChannel: RTCDataChannel }).dataChannel = {
      readyState: 'open',
      send: (value: string) => { events.push(JSON.parse(value) as Record<string, unknown>) },
      close
    } as unknown as RTCDataChannel
    setLiveMode(client)
    ;(client as unknown as { connected: boolean }).connected = true
    const touchActivity = (client as unknown as { touchActivity: () => void }).touchActivity
    touchActivity.call(client)
    return { client, events, close }
  }

  it('ends an inactive live session after four minutes', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client, close } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS)

    expect(ended).toEqual(['idle'])
    expect(close).toHaveBeenCalledOnce()
    expect(client.isConnected()).toBe(false)
  })

  it('hard-stops an active response after the bounded idle extension', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })
    ;(client as unknown as { responseActive: boolean }).responseActive = true

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS)
    expect(ended).toEqual([])

    vi.advanceTimersByTime(MAX_PENDING_WORK_EXTENSION_MS - 1)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['idle'])
  })

  it('gives short pending work grace, then ends as soon as its response finishes', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const proposals: ToolProposal[] = []
    let serverCall: RealtimeServerCall | undefined
    const { client } = timedLiveClient({
      onSessionEnded: (reason) => { ended.push(reason) },
      onToolProposal: (proposal, call) => { proposals.push(proposal); serverCall = call }
    })
    callHandleServerEvent(client, toolCallEvent('open_url', 'pending-call', {
      url: 'https://example.com',
      reason: 'Open it.'
    }))

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS)
    expect(ended).toEqual([])

    client.sendToolResult(proposals[0]!, { ok: true, message: 'Opened.' }, serverCall)
    expect(ended).toEqual([])
    callHandleServerEvent(client, JSON.stringify({ type: 'response.done', response: { output: [] } }))
    expect(ended).toEqual(['idle'])
  })

  it('expires an abandoned confirmation at the idle hard deadline', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })
    callHandleServerEvent(client, toolCallEvent('open_url', 'abandoned-call', {
      url: 'https://example.com',
      reason: 'Open it.'
    }))

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS + MAX_PENDING_WORK_EXTENSION_MS - 1)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['idle'])
  })

  it('caps collapsed pending work at three minutes total', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    let pending: { proposal: ToolProposal; serverCall: RealtimeServerCall } | undefined
    const { client, events } = timedLiveClient({
      onSessionEnded: (reason) => { ended.push(reason) },
      onToolProposal: (proposal, serverCall) => {
        if (serverCall) pending = { proposal, serverCall }
      }
    })
    callHandleServerEvent(client, toolCallEvent('open_url', 'collapse-pending', {
      url: 'https://example.com', reason: 'Open it.'
    }))

    client.startCollapseDisconnect(Date.now())
    vi.advanceTimersByTime(COLLAPSE_DISCONNECT_MS + MAX_PENDING_WORK_EXTENSION_MS - 1)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['collapsed'])
    expect(vi.getTimerCount()).toBe(0)
    client.sendToolResult(pending!.proposal, { ok: true, message: 'Late result.' }, pending!.serverCall)
    expect(events).toEqual([])
  })

  it('re-arms exactly one idle timer after an immediate collapse and reopen', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })

    client.startCollapseDisconnect(Date.now())
    vi.advanceTimersByTime(COLLAPSE_DISCONNECT_MS / 2)
    client.cancelCollapseDisconnect()
    expect(vi.getTimerCount()).toBe(1)

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS - 1)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['idle'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('re-arms bounded idle teardown when reopening after collapsed deferral consumed the old idle timer', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })
    callHandleServerEvent(client, toolCallEvent('open_url', 'reopen-pending', {
      url: 'https://example.com', reason: 'Open it.'
    }))

    // Collapse two minutes after the original activity. Its normal timer fires
    // at t=3m and defers to t=5m; the original idle timer is consumed at t=4m.
    vi.advanceTimersByTime(2 * 60_000)
    client.startCollapseDisconnect(Date.now())
    vi.advanceTimersByTime(COLLAPSE_DISCONNECT_MS)
    vi.advanceTimersByTime(IDLE_DISCONNECT_MS - 2 * 60_000 - COLLAPSE_DISCONNECT_MS)
    expect(ended).toEqual([])
    expect(vi.getTimerCount()).toBe(1)

    client.cancelCollapseDisconnect()
    const timers = client as unknown as {
      idleTimer?: number
      collapseTimer?: number
      deferredDisconnectTimer?: number
    }
    expect(timers.collapseTimer).toBeUndefined()
    expect(timers.deferredDisconnectTimer).toBeUndefined()
    expect(timers.idleTimer).toBeDefined()
    expect(vi.getTimerCount()).toBe(1)

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(MAX_PENDING_WORK_EXTENSION_MS - 1)
    expect(ended).toEqual([])
    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['idle'])
    expect(client.isConnected()).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    vi.advanceTimersByTime(IDLE_DISCONNECT_MS + MAX_PENDING_WORK_EXTENSION_MS)
    expect(ended).toEqual(['idle'])
  })

  it('does not arm an idle timer when reopening after collapse already disconnected', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })

    client.startCollapseDisconnect(Date.now())
    vi.advanceTimersByTime(COLLAPSE_DISCONNECT_MS)
    expect(ended).toEqual(['collapsed'])
    expect(vi.getTimerCount()).toBe(0)

    client.cancelCollapseDisconnect()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(IDLE_DISCONNECT_MS + MAX_PENDING_WORK_EXTENSION_MS)
    expect(ended).toEqual(['collapsed'])
  })

  it('defers idle teardown when a completed transcript touches activity', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })
    vi.advanceTimersByTime(IDLE_DISCONNECT_MS - 1_000)

    callHandleServerEvent(client, JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed',
      transcript: 'Tell me what this means'
    }))
    vi.advanceTimersByTime(IDLE_DISCONNECT_MS - 1)
    expect(ended).toEqual([])

    vi.advanceTimersByTime(1)
    expect(ended).toEqual(['idle'])
  })

  it('never arms cost-saving teardown in mock mode', async () => {
    vi.useFakeTimers()
    globalThis.window = { setTimeout, clearTimeout } as unknown as Window & typeof globalThis
    const ended: string[] = []
    const client = createClient({ onSessionEnded: (reason) => { ended.push(reason) } })

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1-mini' })
    client.startCollapseDisconnect(Date.now())
    client.cancelCollapseDisconnect()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(IDLE_DISCONNECT_MS * 2)

    expect(ended).toEqual([])
    expect(client.isConnected()).toBe(true)
  })

  it('silently ignores tool outputs that arrive after disconnect', () => {
    vi.useFakeTimers()
    const errors: string[] = []
    const { client, events } = timedLiveClient({ onError: (message) => { errors.push(message) } })
    const lateSearch = registerServerCall(client, 'late-search')
    const lateTool = registerServerCall(client, 'late-call')
    client.disconnect()

    client.completeFileSearch(lateSearch, { ok: true, message: 'Found a late result.' })
    client.sendToolResult({
      id: 'late-proposal',
      callId: 'late-call',
      toolName: 'open_url',
      reason: 'Open it.',
      requiresConfirmation: true,
      arguments: { url: 'https://example.com' }
    }, { ok: true, message: 'Opened.' }, lateTool)

    expect(events).toEqual([])
    expect(errors).toEqual([])
  })

  it('ends a collapsed session at the normal deadline when no work is pending', () => {
    vi.useFakeTimers()
    const ended: string[] = []
    const { client } = timedLiveClient({ onSessionEnded: (reason) => { ended.push(reason) } })
    client.startCollapseDisconnect(Date.now())
    vi.advanceTimersByTime(COLLAPSE_DISCONNECT_MS)
    expect(ended).toEqual(['collapsed'])
  })
})

describe('RealtimeClient microphone privacy', () => {
  it('mutes the microphone and disables turn detection when the panel closes', () => {
    const events: Array<Record<string, unknown>> = []
    const track = { enabled: true, stop: () => undefined }
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { localAudio: { getAudioTracks: () => Array<{ enabled: boolean }>; getTracks: () => Array<{ stop: () => void }> } }).localAudio = {
      getAudioTracks: () => [track],
      getTracks: () => [track]
    }

    client.setListening(false)

    expect(track.enabled).toBe(false)
    expect(client.isListening()).toBe(false)
    expect(events).toEqual([
      expect.objectContaining({
        type: 'session.update',
        session: { type: 'realtime', audio: { input: { turn_detection: null } } }
      })
    ])
  })

  it('restores streaming when the panel is reopened', () => {
    const events: Array<Record<string, unknown>> = []
    const track = { enabled: true, stop: () => undefined }
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { localAudio: { getAudioTracks: () => Array<{ enabled: boolean }>; getTracks: () => Array<{ stop: () => void }> } }).localAudio = {
      getAudioTracks: () => [track],
      getTracks: () => [track]
    }

    client.setListening(false)
    client.setListening(true)

    expect(track.enabled).toBe(true)
    expect(events[1]).toMatchObject({
      session: { audio: { input: { turn_detection: { type: 'server_vad' } } } }
    })
  })
})
