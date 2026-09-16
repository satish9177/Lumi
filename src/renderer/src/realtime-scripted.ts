import type { ScriptedRealtimeChannel } from './realtime'
import type { VoiceSlotFact } from '../../shared/voice-task-contracts'
import type { GeminiLiveProvider } from './voice/gemini-live-provider'
import { narrateScripted, scriptedToolCall } from '../../shared/scripted-voice'

/**
 * A deterministic stand-in for the OpenAI Realtime server, for acceptance tests.
 *
 * It speaks the same data-channel event protocol the real service does
 * (session updates, committed audio turns, interim and completed
 * transcriptions, function calls, function outputs, spoken transcripts), so a
 * test drives the real `RealtimeClient`, preload, main, runtime and worker.
 *
 * Its "understanding" is a small English rule set standing in for the model's
 * multilingual NLU. It is test scaffolding: main only issues a `scripted`
 * credential in an unpackaged build started with LUMI_REALTIME_SCRIPTED=1.
 * What it says is built from the function output's typed facts, the same
 * facts a real model is told to speak.
 */

type Json = Record<string, unknown>

interface ToolCall {
  name: string
  arguments: Json
}

export interface SayOptions {
  /** Unstable partial transcripts emitted before the final one. */
  interim?: string[]
  /** Emit the tool call before the final transcript, as the live service may. */
  transcriptAfterToolCall?: boolean
  /** Never complete the transcript (transcription failed). */
  transcriptionFails?: boolean
}

export interface RealtimeHarness {
  say: (text: string, options?: SayOptions) => void
  bargeIn: () => void
  replayLastToolCall: () => void
  replayLastTranscript: () => void
  reconnect: () => Promise<void>
  spoken: () => string[]
  toolCalls: () => ToolCall[]
}

declare global {
  interface Window {
    __lumiRealtimeHarness?: RealtimeHarness
  }
}

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export class ScriptedRealtimeServer implements ScriptedRealtimeChannel {
  readyState: RTCDataChannelState = 'connecting'
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null

  private readonly spokenLines: string[] = []
  private readonly calls: ToolCall[] = []
  private sequence = 0
  /** Like the service's own ids, unique across sessions and restarts. */
  private readonly idPrefix = crypto.randomUUID().replaceAll('-', '').slice(0, 12)
  private lastInput: { kind: 'user'; text: string } | { kind: 'output'; output: Json } | undefined
  private lastResults: VoiceSlotFact[] = []
  private lastToolEvents: string[] = []
  private lastTranscriptEvent: string | undefined

  open(): void {
    window.setTimeout(() => {
      if (this.readyState !== 'connecting') return
      this.readyState = 'open'
      this.onopen?.()
    }, 0)
  }

  close(): void {
    if (this.readyState === 'closed') return
    this.readyState = 'closed'
    this.onclose?.()
  }

  send(data: string): void {
    if (this.readyState !== 'open') throw new Error('The scripted Realtime channel is closed.')
    const event: unknown = JSON.parse(data)
    window.setTimeout(() => this.receive(event), 0)
  }

  // ---- harness surface ------------------------------------------------------

  spoken(): string[] {
    return [...this.spokenLines]
  }

  toolCalls(): ToolCall[] {
    return this.calls.map((call) => ({ ...call }))
  }

  say(text: string, options: SayOptions = {}): void {
    const itemId = this.id('item')
    const responseId = this.id('resp')
    this.emit({ type: 'input_audio_buffer.speech_started', item_id: itemId })
    this.emit({ type: 'input_audio_buffer.speech_stopped', item_id: itemId })
    this.emit({ type: 'input_audio_buffer.committed', item_id: itemId })
    for (const partial of options.interim ?? []) {
      this.emit({ type: 'conversation.item.input_audio_transcription.delta', item_id: itemId, delta: partial })
    }
    // Server VAD creates the response as soon as the turn is committed.
    this.emit({ type: 'response.created', response: { id: responseId } })
    const transcript = JSON.stringify(options.transcriptionFails
      ? { type: 'conversation.item.input_audio_transcription.failed', item_id: itemId, error: { message: 'failed' } }
      : { type: 'conversation.item.input_audio_transcription.completed', item_id: itemId, transcript: text })
    if (!options.transcriptAfterToolCall) {
      this.lastTranscriptEvent = transcript
      this.emitRaw(transcript)
    }
    this.respondTo(text, responseId)
    if (options.transcriptAfterToolCall) {
      this.lastTranscriptEvent = transcript
      window.setTimeout(() => this.emitRaw(transcript), 50)
    }
  }

  bargeIn(): void {
    // Noise or the user talking over Lumi: speech starts, nothing is said.
    this.emit({ type: 'input_audio_buffer.speech_started', item_id: this.id('item') })
  }

  replayLastToolCall(): void {
    this.lastToolEvents.forEach((event) => this.emitRaw(event))
  }

  replayLastTranscript(): void {
    if (this.lastTranscriptEvent) this.emitRaw(this.lastTranscriptEvent)
  }

  // ---- protocol -------------------------------------------------------------

  private id(prefix: string): string {
    this.sequence += 1
    return `${prefix}_scripted${this.idPrefix}${String(this.sequence).padStart(4, '0')}`
  }

  private emit(event: Json): void {
    this.emitRaw(JSON.stringify(event))
  }

  private emitRaw(serialized: string): void {
    if (this.readyState !== 'open') return
    this.onmessage?.({ data: serialized })
  }

  private receive(event: unknown): void {
    if (!isRecord(event) || this.readyState !== 'open') return
    switch (event.type) {
      case 'session.update':
        this.emit({ type: 'session.updated' })
        return
      case 'conversation.item.create': {
        const item = isRecord(event.item) ? event.item : {}
        if (item.type === 'message' && item.role === 'user' && Array.isArray(item.content)) {
          const text = item.content
            .filter((part): part is Json => isRecord(part) && part.type === 'input_text' && typeof part.text === 'string')
            .map((part) => part.text as string)
            .join(' ')
          this.lastInput = { kind: 'user', text }
        } else if (item.type === 'function_call_output' && typeof item.output === 'string') {
          const output: unknown = JSON.parse(item.output)
          this.lastInput = { kind: 'output', output: isRecord(output) ? output : {} }
        }
        this.emit({ type: 'conversation.item.created', item: { id: typeof item.id === 'string' ? item.id : this.id('item') } })
        return
      }
      case 'response.create': {
        const input = this.lastInput
        this.lastInput = undefined
        const responseId = this.id('resp')
        if (input?.kind === 'output') {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.speak(responseId, this.narrate(input.output))
        } else if (input?.kind === 'user') {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.respondTo(input.text, responseId)
        } else {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.speak(responseId, 'Hi, I am Lumi. This is the scripted test voice.')
        }
        return
      }
      case 'response.cancel':
        return
      default:
    }
  }

  private respondTo(text: string, responseId: string): void {
    const call = this.interpret(text)
    if (!call) {
      this.speak(responseId, 'I can help you find a clinic appointment.')
      return
    }
    this.calls.push(call)
    const callId = this.id('call')
    const argumentsJson = JSON.stringify(call.arguments)
    const done = JSON.stringify({
      type: 'response.function_call_arguments.done',
      response_id: responseId,
      call_id: callId,
      name: call.name,
      arguments: argumentsJson
    })
    const responseDone = JSON.stringify({
      type: 'response.done',
      response: { id: responseId, output: [{ type: 'function_call', name: call.name, call_id: callId, arguments: argumentsJson }] }
    })
    this.lastToolEvents = [done, responseDone]
    this.emitRaw(done)
    this.emitRaw(responseDone)
  }

  private speak(responseId: string, text: string): void {
    this.spokenLines.push(text)
    this.emit({ type: 'response.output_audio_transcript.delta', response_id: responseId, delta: text })
    this.emit({ type: 'response.done', response: { id: responseId, output: [] } })
  }

  // ---- the scripted "model" (shared with the Gemini harness) ------------------

  private interpret(raw: string): ToolCall | undefined {
    return scriptedToolCall(raw, this.lastResults)
  }

  private narrate(output: Json): string {
    const narration = narrateScripted(output)
    if (narration.results) this.lastResults = narration.results
    return narration.text
  }
}

/** Exposes the harness to an acceptance test. Called only in scripted mode. */
export function installRealtimeHarness(server: ScriptedRealtimeServer, reconnect: () => Promise<void>): void {
  window.__lumiRealtimeHarness = {
    say: (text, options) => server.say(text, options),
    bargeIn: () => server.bargeIn(),
    replayLastToolCall: () => server.replayLastToolCall(),
    replayLastTranscript: () => server.replayLastTranscript(),
    reconnect,
    spoken: () => server.spoken(),
    toolCalls: () => server.toolCalls()
  }
}

/**
 * The same harness surface over the Gemini Live provider (scripted builds
 * only): utterances go through main's relay to the scripted Gemini socket.
 */
export function installGeminiHarness(provider: GeminiLiveProvider, reconnect: () => Promise<void>): void {
  window.__lumiRealtimeHarness = {
    say: (text, options) => provider.harnessSay(text, { transcriptAfterToolCall: options?.transcriptAfterToolCall }),
    bargeIn: () => undefined,
    replayLastToolCall: () => undefined,
    replayLastTranscript: () => undefined,
    reconnect,
    spoken: () => [...provider.spokenLines],
    toolCalls: () => provider.toolCallLog.map((call) => ({ ...call }))
  }
}
