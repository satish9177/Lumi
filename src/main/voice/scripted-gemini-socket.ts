import { narrateScripted, scriptedToolCall } from '../../shared/scripted-voice'
import type { VoiceSlotFact } from '../../shared/voice-task-contracts'
import type { RelaySocket } from './gemini-live-relay'

/**
 * A deterministic stand-in for the Vertex Gemini Live WebSocket, for the
 * unpackaged acceptance build only (`LUMI_REALTIME_SCRIPTED=gemini`).
 *
 * It speaks Gemini's frame shapes (setupComplete, serverContent with input
 * and output transcriptions, toolCall, turnComplete) so the real relay
 * projection, preload bridge and renderer provider are exercised end to end.
 * Its understanding is the shared English rule set.
 */

type Json = Record<string, unknown>

// 20 ms of silence at 24 kHz, PCM16: proves audio frames flow without a speaker.
const SILENCE = Buffer.alloc(960).toString('base64')

export class ScriptedGeminiSocket implements RelaySocket {
  readyState = 0
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code?: number; reason?: string }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  private sequence = 0
  private lastResults: VoiceSlotFact[] = []
  readonly received: Json[] = []

  constructor() {
    setTimeout(() => {
      if (this.readyState !== 0) return
      this.readyState = 1
      this.onopen?.({})
    }, 0)
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error('closed')
    const frame = JSON.parse(data) as Json
    this.received.push(frame)
    setTimeout(() => this.receive(frame), 0)
  }

  close(code = 1000): void {
    if (this.readyState === 3) return
    this.readyState = 3
    this.onclose?.({ code })
  }

  private emit(frame: Json): void {
    if (this.readyState === 1) this.onmessage?.({ data: JSON.stringify(frame) })
  }

  private receive(frame: Json): void {
    if (frame.setup) {
      this.emit({ setupComplete: {} })
      return
    }
    const say = frame.harnessSay as { text?: string; transcriptAfterToolCall?: boolean } | undefined
    if (say?.text) {
      this.userTurn(say.text, say.transcriptAfterToolCall === true)
      return
    }
    const response = (frame.toolResponse as Json | undefined)?.functionResponses
    if (Array.isArray(response) && response[0]) {
      const output = (response[0] as Json).response as Json
      const narration = narrateScripted(output)
      if (narration.results) this.lastResults = narration.results
      this.speak(narration.text)
      return
    }
    const content = frame.clientContent as { turns?: Array<{ parts?: Array<{ text?: string }> }>; turnComplete?: boolean } | undefined
    if (content?.turnComplete) {
      const text = content.turns?.[0]?.parts?.[0]?.text ?? ''
      if (text.startsWith('[Lumi app context')) {
        this.speak('Hi, I am Lumi. This is the scripted Gemini test voice.')
      } else {
        this.answer(text)
      }
    }
  }

  private userTurn(text: string, transcriptAfterToolCall: boolean): void {
    const words = text.split(' ')
    const half = Math.ceil(words.length / 2)
    const pieces = [words.slice(0, half).join(' '), ` ${words.slice(half).join(' ')}`]
    if (!transcriptAfterToolCall) {
      this.emit({ serverContent: { inputTranscription: { text: pieces[0] } } })
      this.emit({ serverContent: { inputTranscription: { text: pieces[1], finished: true } } })
      this.answer(text)
      return
    }
    this.answer(text)
    setTimeout(() => {
      this.emit({ serverContent: { inputTranscription: { text: pieces[0] } } })
      this.emit({ serverContent: { inputTranscription: { text: pieces[1] } } })
    }, 50)
  }

  private answer(text: string): void {
    const call = scriptedToolCall(text, this.lastResults)
    if (!call) {
      this.speak('I can help you find a clinic appointment.')
      return
    }
    this.sequence += 1
    this.emit({ toolCall: { functionCalls: [{ id: `function-call-${this.sequence}`, name: call.name, args: call.arguments }] } })
  }

  private speak(text: string): void {
    this.emit({ serverContent: { modelTurn: { parts: [{ inlineData: { mimeType: 'audio/pcm;rate=24000', data: SILENCE } }] } } })
    this.emit({ serverContent: { outputTranscription: { text } } })
    this.emit({ serverContent: { generationComplete: true } })
    this.emit({ serverContent: { turnComplete: true }, usageMetadata: { promptTokenCount: 10, responseTokenCount: 5 } })
  }
}
