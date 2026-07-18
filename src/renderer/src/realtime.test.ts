import { afterEach, describe, expect, it } from 'vitest'
import type { CaptureResult, Explanation, ToolProposal } from '../../shared/contracts'
import { RealtimeClient } from './realtime'

const originalWindow = globalThis.window

afterEach(() => {
  globalThis.window = originalWindow
})

describe('RealtimeClient server events', () => {
  it('ignores duplicate completed tool-call events', () => {
    const proposals: string[] = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: () => undefined,
      onExplanation: () => undefined,
      onCaptureContextRequest: () => undefined,
      onToolProposal: (proposal) => proposals.push(proposal.callId ?? ''),
      onError: () => undefined
    })
    const event = JSON.stringify({
      type: 'response.function_call_arguments.done',
      name: 'open_url',
      call_id: 'call-1',
      arguments: JSON.stringify({ url: 'https://example.com', reason: 'Open the displayed website.' })
    })
    const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown) => void }).handleServerEvent

    handleServerEvent.call(client, event)
    handleServerEvent.call(client, event)

    expect(proposals).toEqual(['call-1'])
  })

  it('routes one internal screen-context request without creating an external proposal', () => {
    const captureCalls: Array<string | undefined> = []
    const proposals: ToolProposal[] = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: () => undefined,
      onExplanation: () => undefined,
      onCaptureContextRequest: (callId) => captureCalls.push(callId),
      onToolProposal: (proposal) => proposals.push(proposal),
      onError: () => undefined
    })
    const event = JSON.stringify({
      type: 'response.function_call_arguments.done',
      name: 'capture_screen_context',
      call_id: 'capture-call-1',
      arguments: '{}'
    })
    const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown) => void }).handleServerEvent

    handleServerEvent.call(client, event)
    handleServerEvent.call(client, event)

    expect(captureCalls).toEqual(['capture-call-1'])
    expect(proposals).toEqual([])
  })

  it('routes Telegram recipient lookup locally without exposing any recipient metadata', () => {
    const searches: Array<{ query: string; callId: string }> = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: () => undefined,
      onExplanation: () => undefined,
      onCaptureContextRequest: () => undefined,
      onTelegramRecipientSearch: (query, callId) => searches.push({ query, callId }),
      onToolProposal: () => undefined,
      onError: () => undefined
    })
    const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown) => void }).handleServerEvent
    handleServerEvent.call(client, JSON.stringify({
      type: 'response.function_call_arguments.done',
      name: 'telegram_search_recipients',
      call_id: 'telegram-search-1',
      arguments: JSON.stringify({ query: 'Ravi' })
    }))

    expect(searches).toEqual([{ query: 'Ravi', callId: 'telegram-search-1' }])
  })

  it('sends a complete session payload and waits for session.updated before greeting', () => {
    const events: Array<Record<string, unknown>> = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: () => undefined,
      onExplanation: () => undefined,
      onCaptureContextRequest: () => undefined,
      onToolProposal: () => undefined,
      onError: () => undefined
    })
    ;(client as unknown as { dataChannel: Pick<RTCDataChannel, 'readyState' | 'send'> }).dataChannel = {
      readyState: 'open',
      send: (value: string) => { events.push(JSON.parse(value) as Record<string, unknown>) }
    } as unknown as Pick<RTCDataChannel, 'readyState' | 'send'>
    const configureLiveSession = (client as unknown as { configureLiveSession: () => void }).configureLiveSession
    const handleServerEvent = (client as unknown as { handleServerEvent: (serializedEvent: unknown) => void }).handleServerEvent

    configureLiveSession.call(client)

    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      type: 'session.update',
      session: { type: 'realtime', tool_choice: 'auto' }
    })
    expect((events[0]?.session as Record<string, unknown>).model).toBeUndefined()
    expect((events[0]?.session as Record<string, unknown>).audio).toBeUndefined()
    expect((events[0]?.session as Record<string, unknown>).reasoning).toBeUndefined()

    handleServerEvent.call(client, JSON.stringify({ type: 'session.updated' }))

    expect(events[1]).toMatchObject({ type: 'response.create', response: { output_modalities: ['audio'] } })
  })

  it('keeps the deterministic no-key mock capture flow intact', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const transcripts: string[] = []
    const explanations: Explanation[] = []
    const proposals: ToolProposal[] = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: (text) => transcripts.push(text),
      onExplanation: (explanation) => explanations.push(explanation),
      onCaptureContextRequest: () => undefined,
      onToolProposal: (proposal) => proposals.push(proposal),
      onError: () => undefined
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
    if (proposals[0]?.toolName === 'create_reminder') {
      expect(proposals[0].arguments.sourceContext.captureId).toBe(capture.id)
    }
  })

  it('uses mock screen context for follow-up questions instead of requesting another capture', async () => {
    globalThis.window = { setTimeout } as unknown as Window & typeof globalThis
    const captureRequests: Array<string | undefined> = []
    const client = new RealtimeClient({
      onState: () => undefined,
      onTranscript: () => undefined,
      onExplanation: () => undefined,
      onCaptureContextRequest: (callId) => captureRequests.push(callId),
      onToolProposal: () => undefined,
      onError: () => undefined
    })
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
