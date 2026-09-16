import { describe, expect, it, vi } from 'vitest'
import type { VoiceRelayApi, VoiceRelayServerEvent } from '../../shared/voice-relay-contracts'
import { toGeminiSchema } from '../../shared/voice-relay-contracts'
import { VOICE_TASK_TOOL_DEFINITIONS } from '../../renderer/src/voice-task-tools'
import { GeminiLiveProvider } from '../../renderer/src/voice/gemini-live-provider'
import { SilentAudio } from '../../renderer/src/voice/pcm-audio'
import type { VoiceProviderEvent } from '../../renderer/src/voice/voice-provider'
import { DiagnosticsLog } from '../agent/diagnostics'
import {
  GeminiLiveRelay,
  clientFrame,
  parseRelayMessage,
  projectServerFrame,
  setupFrame,
  type RelaySocket
} from './gemini-live-relay'
import { ScriptedGeminiSocket } from './scripted-gemini-socket'

const TOKENS = { accessToken: async () => 'ya29.secret-token', projectId: async () => 'demo-project-1' }

class FakeSocket implements RelaySocket {
  readyState = 0
  sent: string[] = []
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code?: number }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  constructor() { setTimeout(() => { this.readyState = 1; this.onopen?.({}) }, 0) }
  send(data: string): void { this.sent.push(data) }
  close(): void { this.readyState = 3 }
  serve(frame: unknown): void { this.onmessage?.({ data: JSON.stringify(frame) }) }
}

describe('Gemini relay validation', () => {
  it('accepts only closed renderer messages and refuses harness input outside scripted builds', () => {
    expect(parseRelayMessage({ kind: 'audio', data: 'AAAA' }, false)).toEqual({ kind: 'audio', data: 'AAAA' })
    for (const bad of [
      null, { kind: 'raw', frame: { setup: {} } }, { kind: 'audio', data: 'not base64!' }, { kind: 'audio', data: 'A'.repeat(200_000) },
      { kind: 'text', text: 'x' }, { kind: 'text', text: 'x', turnComplete: true, model: 'other' },
      { kind: 'setup', instructions: 'x', tools: [{ name: 'Shell Exec', description: '', parameters: {} }] },
      { kind: 'tool_response', id: 'call 1', name: 'appointment_status', output: {} },
      { kind: 'image', text: 'x', mimeType: 'image/svg+xml', data: 'AAAA' },
      { kind: 'harness_say', text: 'book it' }
    ]) {
      expect(() => parseRelayMessage(bad, false), JSON.stringify(bad)).toThrow()
    }
    expect(parseRelayMessage({ kind: 'harness_say', text: 'book it' }, true)).toEqual({ kind: 'harness_say', text: 'book it' })
  })

  it('builds the setup frame from main configuration and converts every Lumi tool schema', () => {
    const tools = VOICE_TASK_TOOL_DEFINITIONS.map((tool) => ({ name: tool.name, description: tool.description, parameters: tool.parameters as Record<string, unknown> }))
    const frame = setupFrame({ project: 'demo-project-1', location: 'us-central1', model: 'gemini-live-2.5-flash-native-audio', voice: 'Aoede' },
      parseRelayMessage({ kind: 'setup', instructions: 'Be Lumi.', tools }, false) as never) as { setup: Record<string, any> }
    expect(frame.setup.model).toBe('projects/demo-project-1/locations/us-central1/publishers/google/models/gemini-live-2.5-flash-native-audio')
    expect(frame.setup.generationConfig).toEqual({ responseModalities: ['AUDIO'], speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: 'Aoede' } } } })
    expect(frame.setup.inputAudioTranscription).toEqual({})
    const declarations = frame.setup.tools[0].functionDeclarations as Array<Record<string, any>>
    expect(declarations.map((declaration) => declaration.name)).toEqual(tools.map((tool) => tool.name))
    // No-argument tools carry no parameters; others use Vertex's schema types.
    expect(declarations.find((declaration) => declaration.name === 'appointment_status')!.parameters).toBeUndefined()
    const plan = declarations.find((declaration) => declaration.name === 'appointment_plan')!.parameters
    expect(plan.type).toBe('OBJECT')
    expect(plan.properties.search.properties.when.properties.kind.enum).toContain('next_weekday')
    expect(JSON.stringify(frame)).not.toContain('additionalProperties')
    const fieldNames = JSON.stringify(declarations, (key, value) => key === 'description' ? undefined : value)
    expect(fieldNames).not.toMatch(/approve|execute|url|selector|script/i)
    expect(() => toGeminiSchema({ type: 'null' })).toThrow()
  })

  it('projects only known server content and drops thoughts, oversized or malformed parts', () => {
    expect(projectServerFrame({
      serverContent: {
        inputTranscription: { text: 'find me', finished: true },
        outputTranscription: { text: 'Sure' },
        modelTurn: { parts: [{ text: 'secret plan', thought: true }, { inlineData: { mimeType: 'audio/pcm;rate=24000', data: 'AAAA' } }, { inlineData: { mimeType: 'text/html', data: 'PGI+' } }] },
        interrupted: true,
        turnComplete: true
      },
      toolCall: { functionCalls: [{ id: 'c1', name: 'appointment_status', args: {} }, { id: 'c2', name: 'Bad Name', args: {} }] },
      toolCallCancellation: { ids: ['c1', 'bad id'] },
      goAway: { timeLeft: '10s' },
      somethingElse: { secret: true }
    })).toEqual([
      { kind: 'input_transcript', text: 'find me', finished: true },
      { kind: 'output_transcript', text: 'Sure' },
      { kind: 'audio', data: 'AAAA' },
      { kind: 'interrupted' },
      { kind: 'turn_complete' },
      { kind: 'tool_call', calls: [{ id: 'c1', name: 'appointment_status', argumentsJson: '{}' }] },
      { kind: 'tool_cancel', ids: ['c1'] },
      { kind: 'go_away' }
    ])
    expect(projectServerFrame('nope')).toEqual([])
    expect(clientFrame({ kind: 'tool_response', id: 'c1', name: 'appointment_status', output: { ok: true } }))
      .toEqual({ toolResponse: { functionResponses: [{ id: 'c1', name: 'appointment_status', response: { ok: true } }] } })
  })

  it('keeps the token in main, sends setup first and only once, and drops a replaced session', async () => {
    const sockets: FakeSocket[] = []
    const headers: Array<Record<string, string>> = []
    const events: Array<[string, VoiceRelayServerEvent]> = []
    const diagnostics = new DiagnosticsLog()
    const relay = new GeminiLiveRelay({
      tokens: TOKENS, location: 'us-central1', model: 'gemini-live-2.5-flash-native-audio', diagnostics,
      emit: (id, event) => events.push([id, event]),
      socketFactory: (url, header) => {
        expect(url).toBe('wss://us-central1-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent')
        headers.push(header)
        const socket = new FakeSocket()
        sockets.push(socket)
        return socket
      }
    })
    const first = await relay.open()
    expect(headers[0]).toEqual({ Authorization: 'Bearer ya29.secret-token' })
    expect(relay.send(first, { kind: 'audio', data: 'AAAA' })).toBe(false)
    expect(relay.send(first, { kind: 'setup', instructions: 'x', tools: [] })).toBe(true)
    expect(relay.send(first, { kind: 'setup', instructions: 'y', tools: [] })).toBe(false)
    expect(relay.send(first, { kind: 'audio', data: 'AAAA' })).toBe(true)
    expect(sockets[0].sent).toHaveLength(2)
    sockets[0].serve({ setupComplete: {}, usageMetadata: { promptTokenCount: 5 } })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const second = await relay.open()
    sockets[0].serve({ serverContent: { turnComplete: true } })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(events).toEqual([[first, { kind: 'ready' }], [first, { kind: 'closed', reason: 'replaced' }]])
    expect(relay.send(first, { kind: 'audio_end' })).toBe(false)
    expect(second).not.toBe(first)
    expect(JSON.stringify(events) + JSON.stringify(diagnostics.list())).not.toContain('ya29')
  })

  it('reports an unconfigured relay', async () => {
    const relay = new GeminiLiveRelay({ location: 'us-central1', model: 'm', emit: () => undefined })
    expect(relay.configured()).toBe(false)
    await expect(relay.open()).rejects.toThrow('not configured')
  })
})

/** Relay + renderer provider + scripted Gemini socket, joined without IPC. */
function wired() {
  const listeners = new Set<(id: string, event: VoiceRelayServerEvent) => void>()
  const relay = new GeminiLiveRelay({
    tokens: TOKENS, location: 'us-central1', model: 'scripted',
    emit: (id, event) => listeners.forEach((listener) => listener(id, event)),
    socketFactory: () => new ScriptedGeminiSocket()
  })
  const api: VoiceRelayApi = {
    open: async () => ({ ok: true, sessionId: await relay.open() }),
    send: async (id, message) => relay.send(id, message, true),
    close: async (id) => relay.close(id),
    onEvent: (listener) => { listeners.add(listener); return () => listeners.delete(listener) }
  }
  const audio = new SilentAudio()
  const provider = new GeminiLiveProvider({ relay: api, audio, scripted: true, timers: { set: (fn, ms) => setTimeout(fn, ms) as unknown as number, clear: (id) => clearTimeout(id) } })
  const events: VoiceProviderEvent[] = []
  return { provider, audio, events, handlers: { onEvent: (event: VoiceProviderEvent) => events.push(event), onFailure: vi.fn() } }
}

const settle = (ms = 20): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

describe('Gemini Live provider over the scripted relay', () => {
  it('normalizes a spoken compound request into Lumi turn, response and tool events', async () => {
    const { provider, events, handlers, audio } = wired()
    await provider.connect(handlers)
    provider.configure({ instructions: 'Be Lumi.', tools: VOICE_TASK_TOOL_DEFINITIONS, listening: true, maxOutputTokens: 1024 })
    await settle()
    expect(events).toEqual([{ type: 'ready' }])
    provider.requestResponse({ maxOutputTokens: 100, instructions: 'Greet the user.' })
    await settle()
    expect(provider.spokenLines).toEqual(['Hi, I am Lumi. This is the scripted Gemini test voice.'])
    expect(audio.played).toBe(1)
    events.length = 0

    provider.harnessSay('Find me a dermatologist Saturday evening under 1000 and prepare the cheapest one')
    await settle()
    const types = events.map((event) => event.type)
    expect(types.slice(0, 5)).toEqual(['speech_started', 'turn_committed', 'transcript_completed', 'response_started', 'tool_call'])
    const committed = events[1] as Extract<VoiceProviderEvent, { type: 'turn_committed' }>
    const completed = events[2] as Extract<VoiceProviderEvent, { type: 'transcript_completed' }>
    expect(completed).toEqual({ type: 'transcript_completed', turnId: committed.turnId, text: 'Find me a dermatologist Saturday evening under 1000 and prepare the cheapest one' })
    const call = (events[4] as Extract<VoiceProviderEvent, { type: 'tool_call' }>).call
    expect(call.name).toBe('appointment_plan')
    expect(JSON.parse(call.argumentsJson)).toMatchObject({ choose: { strategy: 'cheapest' }, prepare: true })

    events.length = 0
    provider.sendToolResult(call.callId, JSON.stringify({ ok: true, message: 'm', facts: { kind: 'approval_ready', booking: { doctor: 'Dr A', day: 'Saturday', time: '18:30', price: 800, currency: 'INR' } } }))
    provider.requestResponse({ maxOutputTokens: 100, instructions: 'ignored for Gemini' })
    await settle()
    expect(provider.spokenLines.at(-1)).toContain('Dr A on Saturday at 18:30')
    expect(events.map((event) => event.type)).toEqual(['response_started', 'response_text', 'response_done'])
    provider.close()
  })

  it('binds a tool call to the new turn even when its transcript arrives late', async () => {
    const { provider, events, handlers } = wired()
    await provider.connect(handlers)
    provider.configure({ instructions: 'x', tools: VOICE_TASK_TOOL_DEFINITIONS, listening: true, maxOutputTokens: 1024 })
    await settle()
    provider.harnessSay('Book it.', { transcriptAfterToolCall: true })
    await settle(600)
    const committed = events.find((event) => event.type === 'turn_committed') as Extract<VoiceProviderEvent, { type: 'turn_committed' }>
    const started = events.findIndex((event) => event.type === 'response_started')
    expect(events.indexOf(committed)).toBeLessThan(started)
    expect(events).toContainEqual({ type: 'transcript_completed', turnId: committed.turnId, text: 'Book it.' })
    provider.close()
  })

  it('gives every session globally unique turn ids, because they become durable de-duplication keys', async () => {
    const ids: string[] = []
    for (let index = 0; index < 2; index += 1) {
      const { provider, events, handlers } = wired()
      await provider.connect(handlers)
      provider.configure({ instructions: 'x', tools: VOICE_TASK_TOOL_DEFINITIONS, listening: true, maxOutputTokens: 1024 })
      await settle()
      provider.harnessSay('Book it.')
      await settle()
      ids.push(...events.flatMap((event) => event.type === 'turn_committed' ? [event.turnId] : []))
      provider.close()
    }
    expect(ids).toHaveLength(2)
    expect(new Set(ids).size).toBe(2)
    expect(ids.every((id) => /^[A-Za-z0-9_-]{1,64}$/.test(id))).toBe(true)
  })

  it('reports a dropped session as a transport failure', async () => {
    const { provider, handlers } = wired()
    await provider.connect(handlers)
    provider.close()
    expect(provider.isOpen()).toBe(false)
    const other = wired()
    await other.provider.connect(other.handlers)
    ;(other.provider as unknown as { receive: (event: VoiceRelayServerEvent) => void }).receive({ kind: 'go_away' })
    expect(other.handlers.onFailure).toHaveBeenCalledTimes(1)
  })
})
