import type { VoiceRelayApi, VoiceRelayServerEvent } from '../../../shared/voice-relay-contracts'
import type { AudioIO } from './pcm-audio'
import type {
  ProviderHandlers,
  RealtimeVoiceProvider,
  VoiceSessionConfig
} from './voice-provider'

/**
 * Gemini Live through Electron main's relay.
 *
 * Gemini's event model differs from OpenAI's, and this class translates it
 * honestly rather than pretending the two are the same:
 *
 * - There are no conversation item ids. A spoken user turn gets a local id
 *   (`gemini_turn_N`) when its first input transcription arrives.
 * - Input transcription arrives in pieces. The turn's transcript is final when
 *   Gemini marks it `finished`, or, if it never does, once the model has begun
 *   answering and no further piece arrived for a short quiet period. A tool
 *   call waits for that (RealtimeClient already refuses calls whose turn never
 *   completed).
 * - Responses have no ids either. Each model turn gets a local id when its
 *   first output (audio, text or tool call) arrives, after the user turn was
 *   committed, so tool calls bind to the right utterance.
 * - Gemini answers a tool response or a user text turn by itself; an explicit
 *   response request only matters when nothing is pending (the greeting).
 *   Per-response instructions are not supported and are dropped.
 * - System instructions cannot change mid-session. Later changes are sent as
 *   an app-context note that does not complete a turn.
 * - There is no response cancel; interrupting stops local playback.
 */

const TRANSCRIPT_QUIET_MS = 400
const CONTEXT_PREFIX = '[Lumi app context — not a user request] '

export interface GeminiProviderOptions {
  relay: VoiceRelayApi
  audio: AudioIO
  /** Scripted acceptance builds: let the harness inject an utterance. */
  scripted?: boolean
  timers?: { set: (fn: () => void, ms: number) => number; clear: (id: number) => void }
}

interface PendingTurn {
  id: string
  text: string
  finished: boolean
  /** A model turn already answered this user turn. */
  answered: boolean
  quietTimer?: number
}

export class GeminiLiveProvider implements RealtimeVoiceProvider {
  readonly id = 'gemini' as const
  private sessionId?: string
  private handlers?: ProviderHandlers
  private unsubscribe?: () => void
  private open = false
  private configured = false
  private listening = true
  private initialInstructions = ''
  private turnSequence = 0
  private responseSequence = 0
  private turn?: PendingTurn
  private responseId?: string
  private responseText = ''
  private pendingAutoResponse = false
  /** The next model turn answers something Lumi sent, not new speech. */
  private continuation = false
  private readonly toolNames = new Map<string, string>()
  private readonly idPrefix = crypto.randomUUID().replaceAll('-', '')
  private readonly timers: NonNullable<GeminiProviderOptions['timers']>
  /** Harness inspection (scripted builds only). */
  readonly spokenLines: string[] = []
  readonly toolCallLog: Array<{ name: string; arguments: Record<string, unknown> }> = []

  constructor(private readonly options: GeminiProviderOptions) {
    this.timers = options.timers ?? {
      set: (fn, ms) => window.setTimeout(fn, ms),
      clear: (id) => window.clearTimeout(id)
    }
  }

  async connect(handlers: ProviderHandlers): Promise<void> {
    this.handlers = handlers
    this.unsubscribe = this.options.relay.onEvent((sessionId, event) => {
      if (sessionId === this.sessionId) this.receive(event)
    })
    const opened = await this.options.relay.open()
    if (!opened.ok) {
      this.unsubscribe()
      throw new Error(opened.message)
    }
    this.sessionId = opened.sessionId
    this.open = true
  }

  isOpen(): boolean {
    return this.open
  }

  private send(message: Parameters<VoiceRelayApi['send']>[1]): void {
    if (!this.open || !this.sessionId) throw new Error('The Realtime event channel is not ready.')
    void this.options.relay.send(this.sessionId, message)
  }

  configure(config: VoiceSessionConfig): void {
    if (this.configured) return
    this.configured = true
    this.listening = config.listening
    this.initialInstructions = config.instructions
    this.send({
      kind: 'setup',
      instructions: config.instructions,
      tools: config.tools.map((tool) => ({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters as Record<string, unknown>
      }))
    })
  }

  updateInstructions(instructions: string): void {
    if (!this.configured || instructions === this.initialInstructions) return
    // Only the part that changed since setup is worth saying again.
    let common = 0
    const base = this.initialInstructions
    while (common < base.length && common < instructions.length && base[common] === instructions[common]) common += 1
    const changed = instructions.slice(common).trim()
    if (changed) this.send({ kind: 'text', text: `${CONTEXT_PREFIX}${changed.slice(0, 2_000)}`, turnComplete: false })
  }

  setListening(enabled: boolean): void {
    const wasListening = this.listening
    this.listening = enabled
    this.options.audio.setCapturing(enabled)
    if (wasListening && !enabled && this.open && this.configured) this.send({ kind: 'audio_end' })
  }

  sendUserText(_itemId: string, text: string): void {
    // RealtimeClient already recorded this typed turn as complete.
    this.pendingAutoResponse = true
    this.continuation = true
    this.send({ kind: 'text', text, turnComplete: true })
  }

  sendContext(content: { text: string; imageDataUrl?: string }): void {
    this.pendingAutoResponse = true
    this.continuation = true
    const image = content.imageDataUrl ? /^data:(image\/(?:png|jpeg));base64,([A-Za-z0-9+/=]+)$/.exec(content.imageDataUrl) : null
    if (image) {
      this.send({ kind: 'image', text: content.text, mimeType: image[1] as 'image/png' | 'image/jpeg', data: image[2] })
    } else {
      this.send({ kind: 'text', text: content.text, turnComplete: true })
    }
  }

  sendToolResult(callId: string, output: string): void {
    const name = this.toolNames.get(callId)
    if (!name) return
    this.toolNames.delete(callId)
    let parsed: unknown
    try {
      parsed = JSON.parse(output)
    } catch {
      parsed = { message: output }
    }
    this.pendingAutoResponse = true
    this.continuation = true
    this.send({
      kind: 'tool_response',
      id: callId,
      name,
      output: typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : { value: parsed }
    })
  }

  requestResponse(options: { maxOutputTokens: number; instructions?: string }): void {
    if (this.pendingAutoResponse) {
      this.pendingAutoResponse = false
      return
    }
    if (options.instructions) {
      this.continuation = true
      this.send({ kind: 'text', text: `${CONTEXT_PREFIX}${options.instructions}`, turnComplete: true })
    }
  }

  cancelResponse(): void {
    this.options.audio.stopPlayback()
  }

  /** Scripted builds only: simulate the user saying something. */
  harnessSay(text: string, options: { transcriptAfterToolCall?: boolean } = {}): void {
    if (!this.options.scripted) throw new Error('The voice harness is not available.')
    this.send({ kind: 'harness_say', text, ...(options.transcriptAfterToolCall ? { transcriptAfterToolCall: true } : {}) })
  }

  close(): void {
    const sessionId = this.sessionId
    this.open = false
    this.handlers = undefined
    this.unsubscribe?.()
    this.unsubscribe = undefined
    if (this.turn?.quietTimer !== undefined) this.timers.clear(this.turn.quietTimer)
    this.options.audio.close()
    if (sessionId) void this.options.relay.close(sessionId)
    this.sessionId = undefined
  }

  // ---- server events -------------------------------------------------------

  private emit(event: Parameters<ProviderHandlers['onEvent']>[0]): void {
    this.handlers?.onEvent(event)
  }

  private beginTurn(): PendingTurn {
    if (this.turn && !this.turn.finished && !this.turn.answered) return this.turn
    this.turnSequence += 1
    // Globally unique: the id becomes a durable de-duplication key.
    this.turn = { id: `gemini_turn_${this.idPrefix}_${this.turnSequence}`, text: '', finished: false, answered: false }
    this.emit({ type: 'speech_started' })
    this.emit({ type: 'turn_committed', turnId: this.turn.id })
    return this.turn
  }

  private finishTurn(turn: PendingTurn): void {
    if (turn.finished) return
    turn.finished = true
    if (turn.quietTimer !== undefined) this.timers.clear(turn.quietTimer)
    const text = turn.text.replace(/\s+/g, ' ').trim()
    if (text) this.emit({ type: 'transcript_completed', turnId: turn.id, text })
    else this.emit({ type: 'transcript_failed', turnId: turn.id })
  }

  private scheduleFinish(turn: PendingTurn): void {
    if (turn.finished) return
    if (turn.quietTimer !== undefined) this.timers.clear(turn.quietTimer)
    turn.quietTimer = this.timers.set(() => this.finishTurn(turn), TRANSCRIPT_QUIET_MS)
  }

  private beginResponse(): string {
    if (this.responseId) return this.responseId
    // A model turn nobody asked for answers new speech, even when no
    // transcript piece has arrived yet: it gets its own user turn, never the
    // previous one.
    if (!this.continuation) {
      const turn = this.turn && !this.turn.answered ? this.turn : this.beginTurn()
      turn.answered = true
    }
    this.continuation = false
    this.responseSequence += 1
    this.responseId = `gemini_response_${this.idPrefix}_${this.responseSequence}`
    this.responseText = ''
    this.pendingAutoResponse = false
    this.emit({ type: 'response_started', responseId: this.responseId })
    // The model is answering: the user turn is over even if Gemini never
    // marks its transcription finished.
    if (this.turn && !this.turn.finished) this.scheduleFinish(this.turn)
    return this.responseId
  }

  private endResponse(): void {
    const responseId = this.responseId
    if (!responseId) return
    this.responseId = undefined
    const text = this.responseText.trim()
    if (text && this.options.scripted) this.spokenLines.push(text)
    this.emit({ type: 'response_done', responseId, ...(text ? { text } : {}), toolCalls: [] })
  }

  private receive(event: VoiceRelayServerEvent): void {
    switch (event.kind) {
      case 'ready':
        if (this.listening) {
          void this.options.audio.start((chunk) => {
            if (this.open && this.configured && this.listening) this.send({ kind: 'audio', data: chunk })
          }).catch(() => {
            this.emit({ type: 'error', message: 'Lumi could not open the microphone.' })
          })
        }
        this.emit({ type: 'ready' })
        return
      case 'input_transcript': {
        // Pieces belong to the turn still being transcribed, even if the model
        // already started answering it.
        const turn = this.turn && !this.turn.finished ? this.turn : this.beginTurn()
        turn.text += event.text
        if (event.finished) this.finishTurn(turn)
        else if (this.responseId) this.scheduleFinish(turn)
        return
      }
      case 'output_transcript':
        this.beginResponse()
        this.responseText += event.text
        this.emit({ type: 'response_text', delta: event.text })
        return
      case 'text':
        this.beginResponse()
        return
      case 'audio':
        this.beginResponse()
        this.options.audio.play(event.data)
        return
      case 'tool_call': {
        const responseId = this.beginResponse()
        for (const call of event.calls) {
          this.toolNames.set(call.id, call.name)
          if (this.options.scripted) {
            try {
              this.toolCallLog.push({ name: call.name, arguments: JSON.parse(call.argumentsJson) as Record<string, unknown> })
            } catch {
              this.toolCallLog.push({ name: call.name, arguments: {} })
            }
          }
          this.emit({ type: 'tool_call', call: { callId: call.id, name: call.name, argumentsJson: call.argumentsJson, responseId } })
        }
        // The model waits for the tool response; this model turn is over.
        this.endResponse()
        return
      }
      case 'tool_cancel':
        for (const id of event.ids) this.toolNames.delete(id)
        return
      case 'interrupted':
        this.options.audio.stopPlayback()
        this.emit({ type: 'interrupted' })
        this.endResponse()
        return
      case 'turn_complete':
        if (this.turn && !this.turn.finished) this.finishTurn(this.turn)
        this.endResponse()
        return
      case 'go_away':
      case 'closed':
        if (!this.open) return
        this.open = false
        this.handlers?.onFailure()
    }
  }
}
