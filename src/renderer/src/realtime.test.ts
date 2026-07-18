import { afterEach, describe, expect, it } from 'vitest'
import type { CaptureResult, Explanation, SearchDocumentsInput, ToolProposal } from '../../shared/contracts'
import { IntentTracker } from '../../main/services/intent-policy'
import { normalizeSearchQuery } from '../../shared/search-query'
import { RealtimeClient } from './realtime'

const originalWindow = globalThis.window

afterEach(() => {
  globalThis.window = originalWindow
})

function flushAsyncWork(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

type Callbacks = ConstructorParameters<typeof RealtimeClient>[0]

/** Every callback a test does not care about is a no-op. */
function createClient(overrides: Partial<Callbacks> = {}): RealtimeClient {
  return new RealtimeClient({
    onState: () => undefined,
    onTranscript: () => undefined,
    onExplanation: () => undefined,
    onCaptureContextRequest: () => undefined,
    onFileSearchRequest: () => undefined,
    onToolProposal: () => undefined,
    onError: () => undefined,
    ...overrides
  })
}

function injectDataChannel(client: RealtimeClient, events: Array<Record<string, unknown>>): void {
  ;(client as unknown as { dataChannel: Pick<RTCDataChannel, 'readyState' | 'send'> }).dataChannel = {
    readyState: 'open',
    send: (value: string) => { events.push(JSON.parse(value) as Record<string, unknown>) },
    close: () => undefined
  } as unknown as Pick<RTCDataChannel, 'readyState' | 'send'>
}

function setLiveMode(client: RealtimeClient): void {
  ;(client as unknown as { mode: 'live' | 'mock' }).mode = 'live'
}

function callHandleServerEvent(client: RealtimeClient, event: unknown): void {
  const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown) => void }).handleServerEvent
  handleServerEvent.call(client, event)
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
      onCaptureContextRequest: (callId) => { captureCalls.push(callId) },
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
      onTelegramRecipientSearch: (query, callId) => { searches.push({ query, callId }) }
    })

    callHandleServerEvent(client, toolCallEvent('telegram_search_recipients', 'telegram-search-1', { query: 'Ravi' }))

    expect(searches).toEqual([{ query: 'Ravi', callId: 'telegram-search-1' }])
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
      session: { type: 'realtime', tool_choice: 'auto' }
    })
    const session = events[0]?.session as Record<string, unknown>
    expect(session.model).toBeUndefined()
    expect(session.reasoning).toBeUndefined()
    // Completed input transcription is what feeds the trusted intent tracker.
    expect(session.audio).toMatchObject({ input: { transcription: { model: expect.any(String) } } })

    callHandleServerEvent(client, JSON.stringify({ type: 'session.updated' }))

    expect(events[1]).toMatchObject({ type: 'response.create', response: { output_modalities: ['audio'] } })
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
    await client.sendCapture(capture, 'What is this email about?')

    expect(transcripts).toContain('Hi, I am LifeLens. I am ready to look at a screen with you.')
    expect(explanations[0]?.sourceCaptureId).toBe(capture.id)
    expect(proposals[0]).toMatchObject({ toolName: 'create_reminder', requiresConfirmation: true })
  })

  it('rejects capture_screen_context for local-file intent without opening the source picker', async () => {
    const captureRequests: Array<string | undefined> = []
    const events: Array<Record<string, unknown>> = []
    const client = createClient({
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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
      onFileSearchRequest: (request, callId) => { requests.push({ request, callId }) },
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

    client.completeFileSearch('search-1', {
      ok: true,
      message: 'Found 1 matching file.',
      compactResults: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }],
      resultIds: ['result-1']
    })
    client.completeFileSearch('search-1', { ok: false, message: 'A duplicate result.' })

    expect(functionCallOutputs(events)).toEqual([
      expect.objectContaining({ ok: true, results: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }] })
    ])
  })

  it('sends no identifier, root, or path to the model with search results', () => {
    const events: Array<Record<string, unknown>> = []
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)

    client.completeFileSearch('search-1', {
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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
      onFileSearchRequest: (request, callId) => { requests.push({ request, callId }) }
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) }
    })

    await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
    await client.sendUserRequest('Check my resume')

    expect(captureRequests).toEqual([])
    expect(transcripts).toContain('Should I inspect the resume currently visible, or find it in your approved folder?')
  })

  it('uses mock screen context for follow-up questions instead of requesting another capture', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const client = createClient({ onCaptureContextRequest: (callId) => { captureRequests.push(callId) } })
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
    expect(images).toEqual([{ type: 'input_image', image_url: approvedImage.dataUrl }])
    expect(JSON.stringify(events)).toContain('Who is in this photo?')
    expect(client.hasSelectedPhoto()).toBe(true)
    expect(events.at(-1)).toMatchObject({ type: 'response.create' })
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
    globalThis.window = { speechSynthesis: undefined } as unknown as Window & typeof globalThis

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

    client.completeFileSearch('search-photos-1', {
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
      onCaptureContextRequest: (callId) => { captureRequests.push(callId) },
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

describe('RealtimeClient microphone privacy', () => {
  it('mutes the microphone and disables turn detection when the panel closes', () => {
    const events: Array<Record<string, unknown>> = []
    const track = { enabled: true, stop: () => undefined }
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { localAudio: { getAudioTracks: () => Array<{ enabled: boolean }> } }).localAudio = {
      getAudioTracks: () => [track]
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
    const track = { enabled: true }
    const client = createClient()
    injectDataChannel(client, events)
    setLiveMode(client)
    ;(client as unknown as { localAudio: { getAudioTracks: () => Array<{ enabled: boolean }> } }).localAudio = {
      getAudioTracks: () => [track]
    }

    client.setListening(false)
    client.setListening(true)

    expect(track.enabled).toBe(true)
    expect(events[1]).toMatchObject({
      session: { audio: { input: { turn_detection: { type: 'server_vad' } } } }
    })
  })
})
