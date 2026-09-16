import type {
  ProviderHandlers,
  ProviderToolCall,
  RealtimeVoiceProvider,
  VoiceProviderEvent,
  VoiceSessionConfig
} from './voice-provider'

/**
 * OpenAI Realtime over WebRTC (audio) plus the `oai-events` data channel, or
 * the deterministic scripted channel that speaks the same event protocol.
 *
 * This is the only renderer module that knows OpenAI event names.
 */

const INPUT_TRANSCRIPTION_MODEL = 'gpt-4o-mini-transcribe'
// Tuned conservatively for a laptop microphone. These are operational knobs,
// not universal constants: retain genuine barge-in while rejecting more room
// noise than the Realtime defaults.
export const SERVER_TURN_DETECTION = {
  type: 'server_vad',
  threshold: 0.7,
  prefix_padding_ms: 300,
  silence_duration_ms: 650,
  create_response: true,
  interrupt_response: true
} as const
export const LAPTOP_MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true
  }
} as const

/** The data-channel surface both the real channel and the scripted server implement. */
export interface EventChannel {
  readonly readyState: RTCDataChannelState
  onopen: ((event?: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onerror: ((event?: unknown) => void) | null
  onclose: ((event?: unknown) => void) | null
  send: (data: string) => void
  close: () => void
  /** Scripted channels open on request; a WebRTC channel opens by itself. */
  open?: () => void
}

type Json = Record<string, unknown>

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function extractResponseText(response: Json): string {
  const output = Array.isArray(response.output) ? response.output : []
  const parts: string[] = []
  for (const item of output) {
    if (!isRecord(item)) continue
    if (typeof item.transcript === 'string') parts.push(item.transcript)
    if (!Array.isArray(item.content)) continue
    for (const content of item.content) {
      if (!isRecord(content)) continue
      if (typeof content.text === 'string') parts.push(content.text)
      if (typeof content.transcript === 'string') parts.push(content.transcript)
    }
  }
  return parts.join(' ').trim()
}

function toolCallFrom(value: Json, responseId?: string): ProviderToolCall | undefined {
  const name = typeof value.name === 'string' ? value.name : ''
  const callId = typeof value.call_id === 'string' ? value.call_id : typeof value.callId === 'string' ? value.callId : ''
  if (!name || !callId) return undefined
  return {
    callId,
    name,
    argumentsJson: typeof value.arguments === 'string' ? value.arguments : '',
    ...(responseId !== undefined ? { responseId } : {})
  }
}

/** One serialized OpenAI Realtime server event -> Lumi events. Unknown events map to nothing. */
export function decodeOpenAIEvent(serialized: unknown): VoiceProviderEvent[] {
  if (typeof serialized !== 'string') return []
  let event: Json
  try {
    const parsed: unknown = JSON.parse(serialized)
    if (!isRecord(parsed)) return []
    event = parsed
  } catch {
    return []
  }
  const type = typeof event.type === 'string' ? event.type : ''
  const itemId = typeof event.item_id === 'string' ? event.item_id : undefined
  switch (type) {
    case 'error':
      return [{ type: 'error', message: isRecord(event.error) && typeof event.error.message === 'string' ? event.error.message : 'Realtime returned an error.' }]
    case 'session.updated':
      return [{ type: 'ready' }]
    case 'input_audio_buffer.speech_started':
      return [{ type: 'speech_started' }]
    case 'input_audio_buffer.committed':
      return itemId ? [{ type: 'turn_committed', turnId: itemId }] : []
    case 'conversation.item.input_audio_transcription.failed':
      return itemId ? [{ type: 'transcript_failed', turnId: itemId }] : []
    case 'conversation.item.input_audio_transcription.completed':
      return [{ type: 'transcript_completed', ...(itemId ? { turnId: itemId } : {}), text: typeof event.transcript === 'string' ? event.transcript : '' }]
    case 'response.created': {
      const responseId = isRecord(event.response) && typeof event.response.id === 'string' ? event.response.id : undefined
      return [{ type: 'response_started', ...(responseId ? { responseId } : {}) }]
    }
    case 'response.output_text.delta':
    case 'response.text.delta':
    case 'response.output_audio_transcript.delta':
      return typeof event.delta === 'string' && event.delta ? [{ type: 'response_text', delta: event.delta }] : []
    case 'response.function_call_arguments.done': {
      const call = toolCallFrom(event, typeof event.response_id === 'string' ? event.response_id : undefined)
      return call ? [{ type: 'tool_call', call }] : []
    }
    case 'response.done': {
      if (!isRecord(event.response)) return [{ type: 'response_done', toolCalls: [] }]
      const response = event.response
      const responseId = typeof response.id === 'string' ? response.id : undefined
      const output = Array.isArray(response.output) ? response.output : []
      const toolCalls = output
        .filter((item): item is Json => isRecord(item) && item.type === 'function_call')
        .map((item) => toolCallFrom(item, responseId))
        .filter((call): call is ProviderToolCall => call !== undefined)
      const text = extractResponseText(response)
      return [{ type: 'response_done', ...(responseId ? { responseId } : {}), ...(text ? { text } : {}), toolCalls }]
    }
    default:
      return []
  }
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 10_000)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } finally {
    window.clearTimeout(timeout)
  }
}

function isTimeoutError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
}

export class OpenAIRealtimeProvider implements RealtimeVoiceProvider {
  private channel: EventChannel | undefined
  private peerConnection: RTCPeerConnection | undefined
  private localAudio: MediaStream | undefined
  private remoteAudio: HTMLAudioElement | undefined
  private handlers: ProviderHandlers | undefined
  private listening = true
  private closed = false

  private constructor(
    readonly id: 'openai' | 'scripted',
    private readonly token: string | undefined,
    private readonly scriptedChannel: EventChannel | undefined
  ) {}

  static live(token: string): OpenAIRealtimeProvider {
    return new OpenAIRealtimeProvider('openai', token, undefined)
  }

  static scripted(channel: EventChannel): OpenAIRealtimeProvider {
    return new OpenAIRealtimeProvider('scripted', undefined, channel)
  }

  /** An already-open channel (tests and the scripted harness). */
  static attached(channel: EventChannel, handlers?: ProviderHandlers): OpenAIRealtimeProvider {
    const provider = new OpenAIRealtimeProvider('scripted', undefined, undefined)
    provider.channel = channel
    provider.handlers = handlers
    return provider
  }

  /** Test seam: the microphone stream this provider controls. */
  attachLocalAudio(stream: MediaStream): void {
    this.localAudio = stream
  }

  async connect(handlers: ProviderHandlers): Promise<void> {
    this.handlers = handlers
    if (this.scriptedChannel) {
      this.channel = this.scriptedChannel
      const opened = this.waitForOpen(this.scriptedChannel)
      this.scriptedChannel.open?.()
      await opened
      return
    }
    if (!this.token) throw new Error('Lumi received an incomplete Realtime credential.')
    this.localAudio = await navigator.mediaDevices.getUserMedia(LAPTOP_MIC_CONSTRAINTS)
    const peerConnection = new RTCPeerConnection()
    this.peerConnection = peerConnection
    this.remoteAudio = document.createElement('audio')
    this.remoteAudio.autoplay = true
    this.remoteAudio.hidden = true
    document.body.append(this.remoteAudio)
    peerConnection.ontrack = (event) => {
      if (this.remoteAudio && event.streams[0]) this.remoteAudio.srcObject = event.streams[0]
    }
    peerConnection.onconnectionstatechange = () => {
      if (this.peerConnection === peerConnection && !this.closed && peerConnection.connectionState === 'failed') {
        this.handlers?.onFailure()
      }
    }
    const track = this.localAudio.getAudioTracks()[0]
    if (!track) throw new Error('Microphone access did not return an audio track.')
    track.enabled = this.listening
    peerConnection.addTrack(track, this.localAudio)

    const channel = peerConnection.createDataChannel('oai-events') as unknown as EventChannel
    this.channel = channel
    const opened = this.waitForOpen(channel)
    void opened.catch(() => undefined)

    const offer = await peerConnection.createOffer()
    if (!offer.sdp) throw new Error('Could not create a WebRTC session description.')
    await peerConnection.setLocalDescription(offer)
    let response: Response
    try {
      response = await fetchWithTimeout('https://api.openai.com/v1/realtime/calls', {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/sdp' },
        body: offer.sdp
      })
    } catch (error) {
      if (isTimeoutError(error)) throw new Error('Realtime connection timed out while negotiating audio.')
      throw error
    }
    if (!response.ok) throw new Error(`Realtime WebRTC connection failed (status ${response.status}).`)
    await peerConnection.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    await opened
  }

  private waitForOpen(channel: EventChannel): Promise<void> {
    channel.onmessage = (event) => {
      if (this.channel === channel && !this.closed) this.receive(event.data)
    }
    return new Promise<void>((resolve, reject) => {
      let isOpen = false
      const timeout = window.setTimeout(() => reject(new Error('Timed out while opening the Realtime event channel.')), 15_000)
      channel.onopen = () => {
        window.clearTimeout(timeout)
        if (this.channel !== channel || this.closed) {
          reject(new Error('The Realtime session was replaced before its event channel opened.'))
          return
        }
        isOpen = true
        resolve()
      }
      channel.onerror = () => {
        window.clearTimeout(timeout)
        reject(new Error('The Realtime event channel could not be opened.'))
      }
      channel.onclose = () => {
        window.clearTimeout(timeout)
        if (!isOpen) reject(new Error('The Realtime event channel closed before it opened.'))
      }
    })
  }

  /** Feed one serialized server event (the data channel, or a test). */
  receive(serialized: unknown): void {
    const handlers = this.handlers
    if (!handlers) return
    for (const event of decodeOpenAIEvent(serialized)) handlers.onEvent(event)
  }

  isOpen(): boolean {
    return !this.closed && this.channel?.readyState === 'open'
  }

  private send(event: unknown): void {
    if (!this.isOpen() || !this.channel) throw new Error('The Realtime event channel is not ready.')
    this.channel.send(JSON.stringify(event))
  }

  configure(config: VoiceSessionConfig): void {
    this.listening = config.listening
    this.send({
      type: 'session.update',
      session: {
        type: 'realtime',
        instructions: config.instructions,
        tools: config.tools,
        tool_choice: 'auto',
        // VAD-created spoken responses have no response.create override, so
        // this ceiling is intentionally high enough for legitimate long-form audio.
        max_output_tokens: config.maxOutputTokens,
        audio: {
          input: {
            noise_reduction: { type: 'far_field' },
            // Completed transcripts feed the trusted main-process intent
            // tracker, so spoken and typed requests get identical policy.
            transcription: { model: INPUT_TRANSCRIPTION_MODEL },
            turn_detection: config.listening ? SERVER_TURN_DETECTION : null
          }
        }
      }
    })
  }

  updateInstructions(instructions: string): void {
    this.send({ type: 'session.update', session: { type: 'realtime', instructions } })
  }

  setListening(enabled: boolean): void {
    this.listening = enabled
    this.localAudio?.getAudioTracks().forEach((track) => { track.enabled = enabled })
    if (this.isOpen()) {
      this.send({
        type: 'session.update',
        session: { type: 'realtime', audio: { input: { turn_detection: enabled ? SERVER_TURN_DETECTION : null } } }
      })
    }
  }

  sendUserText(itemId: string, text: string): void {
    this.send({
      type: 'conversation.item.create',
      item: { id: itemId, type: 'message', role: 'user', content: [{ type: 'input_text', text }] }
    })
  }

  sendContext(content: { text: string; imageDataUrl?: string }): void {
    this.send({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [
          { type: 'input_text', text: content.text },
          ...(content.imageDataUrl ? [{ type: 'input_image', image_url: content.imageDataUrl, detail: 'low' }] : [])
        ]
      }
    })
  }

  sendToolResult(callId: string, output: string): void {
    this.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output } })
  }

  requestResponse(options: { maxOutputTokens: number; instructions?: string }): void {
    this.send({
      type: 'response.create',
      response: {
        ...(options.instructions ? { instructions: options.instructions } : {}),
        output_modalities: ['audio'],
        max_output_tokens: options.maxOutputTokens
      }
    })
  }

  cancelResponse(): void {
    this.send({ type: 'response.cancel' })
  }

  close(): void {
    this.closed = true
    this.handlers = undefined
    this.channel?.close()
    this.peerConnection?.close()
    this.localAudio?.getTracks().forEach((track) => track.stop())
    this.remoteAudio?.remove()
    this.channel = undefined
    this.peerConnection = undefined
    this.localAudio = undefined
    this.remoteAudio = undefined
  }
}
