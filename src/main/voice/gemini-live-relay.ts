import { randomUUID } from 'node:crypto'
import {
  MAX_RELAY_AUDIO_BASE64,
  MAX_RELAY_IMAGE_BASE64,
  MAX_RELAY_INSTRUCTIONS,
  MAX_RELAY_TEXT,
  MAX_RELAY_TOOLS,
  MAX_RELAY_TOOL_OUTPUT,
  toGeminiSchema,
  type VoiceRelayClientMessage,
  type VoiceRelayServerEvent
} from '../../shared/voice-relay-contracts'
import { NO_DIAGNOSTICS, type DiagnosticsSink } from '../agent/diagnostics'
import type { GoogleTokenSource } from '../models/google-auth'

/**
 * Gemini Live (Vertex AI) relay, owned by Electron main.
 *
 * Main opens the WebSocket with the Google access token in a header, builds
 * the setup message (model, voice, transcription) from its own configuration,
 * and forwards a closed, validated subset of messages each way. The renderer
 * never sees the token, the project, the endpoint or a raw server frame.
 *
 * One session at a time: opening a new one closes the previous one, and
 * events from a replaced session are dropped by session id.
 */

export interface RelaySocket {
  readonly readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code?: number; reason?: string }) => void) | null
  onerror: ((event: unknown) => void) | null
}

export type SocketFactory = (url: string, headers: Record<string, string>) => RelaySocket

export interface GeminiRelayOptions {
  tokens?: GoogleTokenSource
  location: string
  model: string
  voice?: string
  emit: (sessionId: string, event: VoiceRelayServerEvent) => void
  socketFactory?: SocketFactory
  diagnostics?: DiagnosticsSink
  openTimeoutMs?: number
}

const TOOL_NAME = /^[a-z][a-z0-9_]{0,63}$/
const CALL_ID = /^[A-Za-z0-9_-]{1,128}$/
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/
const OPEN = 1

type Json = Record<string, unknown>

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function boundedText(value: unknown, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) throw new Error('invalid text')
  return value
}

function base64(value: unknown, maximum: number): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum || value.length % 4 !== 0 || !BASE64.test(value)) {
    throw new Error('invalid audio')
  }
  return value
}

/** Validate a renderer message. Anything else is dropped before it reaches the socket. */
export function parseRelayMessage(value: unknown, allowHarness: boolean): VoiceRelayClientMessage {
  if (!isRecord(value)) throw new Error('invalid message')
  const only = (keys: string[]): void => {
    if (Object.keys(value).some((key) => !keys.includes(key))) throw new Error('invalid message')
  }
  switch (value.kind) {
    case 'setup': {
      only(['kind', 'instructions', 'tools'])
      if (!Array.isArray(value.tools) || value.tools.length > MAX_RELAY_TOOLS) throw new Error('invalid tools')
      const tools = value.tools.map((tool) => {
        if (!isRecord(tool) || typeof tool.name !== 'string' || !TOOL_NAME.test(tool.name) || !isRecord(tool.parameters)) {
          throw new Error('invalid tool')
        }
        if (JSON.stringify(tool.parameters).length > 16_000) throw new Error('invalid tool')
        return { name: tool.name, description: boundedText(tool.description, 2_000), parameters: tool.parameters }
      })
      return { kind: 'setup', instructions: boundedText(value.instructions, MAX_RELAY_INSTRUCTIONS), tools }
    }
    case 'audio':
      only(['kind', 'data'])
      return { kind: 'audio', data: base64(value.data, MAX_RELAY_AUDIO_BASE64) }
    case 'audio_end':
      only(['kind'])
      return { kind: 'audio_end' }
    case 'text':
      only(['kind', 'text', 'turnComplete'])
      if (typeof value.turnComplete !== 'boolean') throw new Error('invalid text')
      return { kind: 'text', text: boundedText(value.text, MAX_RELAY_TEXT), turnComplete: value.turnComplete }
    case 'image':
      only(['kind', 'text', 'mimeType', 'data'])
      if (value.mimeType !== 'image/png' && value.mimeType !== 'image/jpeg') throw new Error('invalid image')
      return { kind: 'image', text: boundedText(value.text, MAX_RELAY_TEXT), mimeType: value.mimeType, data: base64(value.data, MAX_RELAY_IMAGE_BASE64) }
    case 'tool_response': {
      only(['kind', 'id', 'name', 'output'])
      if (typeof value.id !== 'string' || !CALL_ID.test(value.id)) throw new Error('invalid tool response')
      if (typeof value.name !== 'string' || !TOOL_NAME.test(value.name)) throw new Error('invalid tool response')
      if (!isRecord(value.output) || JSON.stringify(value.output).length > MAX_RELAY_TOOL_OUTPUT) throw new Error('invalid tool response')
      return { kind: 'tool_response', id: value.id, name: value.name, output: value.output }
    }
    case 'harness_say':
      if (!allowHarness) throw new Error('invalid message')
      only(['kind', 'text', 'transcriptAfterToolCall'])
      return {
        kind: 'harness_say',
        text: boundedText(value.text, 1_000),
        ...(value.transcriptAfterToolCall === true ? { transcriptAfterToolCall: true } : {})
      }
    default:
      throw new Error('invalid message')
  }
}

/** Build the Vertex setup frame. Model and voice come from main, not the renderer. */
export function setupFrame(options: { project: string; location: string; model: string; voice?: string }, message: Extract<VoiceRelayClientMessage, { kind: 'setup' }>): Json {
  const declarations = message.tools.map((tool) => {
    const properties = isRecord(tool.parameters.properties) ? tool.parameters.properties : {}
    return Object.keys(properties).length === 0
      ? { name: tool.name, description: tool.description }
      : { name: tool.name, description: tool.description, parameters: toGeminiSchema(tool.parameters) }
  })
  return {
    setup: {
      model: `projects/${options.project}/locations/${options.location}/publishers/google/models/${options.model}`,
      generationConfig: {
        responseModalities: ['AUDIO'],
        ...(options.voice ? { speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: options.voice } } } } : {})
      },
      systemInstruction: { parts: [{ text: message.instructions }] },
      ...(declarations.length > 0 ? { tools: [{ functionDeclarations: declarations }] } : {}),
      inputAudioTranscription: {},
      outputAudioTranscription: {}
    }
  }
}

/** Project one server frame into the closed renderer event set. */
export function projectServerFrame(frame: unknown): VoiceRelayServerEvent[] {
  if (!isRecord(frame)) return []
  const events: VoiceRelayServerEvent[] = []
  if (frame.setupComplete !== undefined) events.push({ kind: 'ready' })
  const content = isRecord(frame.serverContent) ? frame.serverContent : undefined
  if (content) {
    const input = isRecord(content.inputTranscription) ? content.inputTranscription : undefined
    if (input && typeof input.text === 'string') {
      events.push({ kind: 'input_transcript', text: input.text.slice(0, 2_000), finished: input.finished === true })
    } else if (input?.finished === true) {
      events.push({ kind: 'input_transcript', text: '', finished: true })
    }
    const output = isRecord(content.outputTranscription) ? content.outputTranscription : undefined
    if (output && typeof output.text === 'string') events.push({ kind: 'output_transcript', text: output.text.slice(0, 4_000) })
    const turn = isRecord(content.modelTurn) ? content.modelTurn : undefined
    for (const part of Array.isArray(turn?.parts) ? turn.parts : []) {
      if (!isRecord(part) || part.thought === true) continue
      const inline = isRecord(part.inlineData) ? part.inlineData : undefined
      if (inline && typeof inline.data === 'string' && typeof inline.mimeType === 'string' && inline.mimeType.startsWith('audio/pcm') &&
          inline.data.length <= 2_000_000 && BASE64.test(inline.data)) {
        events.push({ kind: 'audio', data: inline.data })
      } else if (typeof part.text === 'string') {
        events.push({ kind: 'text', text: part.text.slice(0, 4_000) })
      }
    }
    if (content.interrupted === true) events.push({ kind: 'interrupted' })
    if (content.turnComplete === true) events.push({ kind: 'turn_complete' })
  }
  const toolCall = isRecord(frame.toolCall) ? frame.toolCall : undefined
  if (toolCall && Array.isArray(toolCall.functionCalls)) {
    const calls = toolCall.functionCalls.flatMap((call) => {
      if (!isRecord(call) || typeof call.name !== 'string' || !TOOL_NAME.test(call.name)) return []
      const id = typeof call.id === 'string' && CALL_ID.test(call.id) ? call.id : `call_${randomUUID()}`
      return [{ id, name: call.name, argumentsJson: JSON.stringify(isRecord(call.args) ? call.args : {}).slice(0, 8_000) }]
    })
    if (calls.length > 0) events.push({ kind: 'tool_call', calls })
  }
  const cancellation = isRecord(frame.toolCallCancellation) ? frame.toolCallCancellation : undefined
  if (cancellation && Array.isArray(cancellation.ids)) {
    events.push({ kind: 'tool_cancel', ids: cancellation.ids.filter((id): id is string => typeof id === 'string' && CALL_ID.test(id)) })
  }
  if (frame.goAway !== undefined) events.push({ kind: 'go_away' })
  return events
}

function defaultSocketFactory(url: string, headers: Record<string, string>): RelaySocket {
  // Electron 38's Node (undici) WebSocket accepts request headers; a browser
  // WebSocket could not, which is why this lives in main.
  const Socket = globalThis.WebSocket as unknown as new (url: string, init: { headers: Record<string, string> }) => RelaySocket
  return new Socket(url, { headers })
}

interface Session {
  id: string
  socket: RelaySocket
  setupSent: boolean
  opened: number
}

export class GeminiLiveRelay {
  private session?: Session
  private readonly diagnostics: DiagnosticsSink

  constructor(private readonly options: GeminiRelayOptions) {
    this.diagnostics = options.diagnostics ?? NO_DIAGNOSTICS
  }

  configured(): boolean {
    return this.options.tokens !== undefined
  }

  async open(): Promise<string> {
    const tokens = this.options.tokens
    if (!tokens) throw new Error('Gemini Live is not configured.')
    this.closeCurrent('replaced')
    const [token, project] = await Promise.all([tokens.accessToken(), tokens.projectId()])
    const host = this.options.location === 'global' ? 'aiplatform.googleapis.com' : `${this.options.location}-aiplatform.googleapis.com`
    const url = `wss://${host}/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent`
    const socket = (this.options.socketFactory ?? defaultSocketFactory)(url, { Authorization: `Bearer ${token}` })
    const id = randomUUID()
    const session: Session = { id, socket, setupSent: false, opened: Date.now() }
    this.session = session
    this.project = project
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Gemini Live did not connect in time.')), this.options.openTimeoutMs ?? 15_000)
      socket.onopen = () => { clearTimeout(timer); resolve() }
      socket.onerror = () => { clearTimeout(timer); reject(new Error('Gemini Live could not connect.')) }
      socket.onclose = () => { clearTimeout(timer); reject(new Error('Gemini Live closed before it opened.')) }
    }).catch((error: unknown) => {
      if (this.session === session) this.session = undefined
      throw error
    })
    socket.onmessage = (event) => { void this.receive(session, event.data) }
    socket.onerror = () => undefined
    socket.onclose = (event) => {
      if (this.session !== session) return
      this.session = undefined
      this.diagnostics.record({ kind: 'voice_session', provider: 'gemini', model: this.options.model, result: `closed_${event.code ?? 0}` })
      this.options.emit(id, { kind: 'closed', reason: event.code === 1000 ? 'remote' : 'error' })
    }
    this.diagnostics.record({ kind: 'voice_session', provider: 'gemini', model: this.options.model, result: 'opened' })
    return id
  }

  private project = ''

  send(sessionId: string, value: unknown, allowHarness = false): boolean {
    const session = this.session
    if (!session || session.id !== sessionId || session.socket.readyState !== OPEN) return false
    let message: VoiceRelayClientMessage
    try {
      message = parseRelayMessage(value, allowHarness)
    } catch {
      return false
    }
    if (message.kind === 'setup') {
      if (session.setupSent) return false
      session.setupSent = true
      session.socket.send(JSON.stringify(setupFrame({
        project: this.project, location: this.options.location, model: this.options.model, voice: this.options.voice
      }, message)))
      return true
    }
    if (!session.setupSent) return false
    session.socket.send(JSON.stringify(clientFrame(message)))
    return true
  }

  close(sessionId: string): void {
    if (this.session?.id === sessionId) this.closeCurrent('local')
  }

  private closeCurrent(reason: 'replaced' | 'local'): void {
    const session = this.session
    if (!session) return
    this.session = undefined
    try {
      session.socket.close(1000, 'client closed')
    } catch {
      // Already closed.
    }
    this.options.emit(session.id, { kind: 'closed', reason })
  }

  private async receive(session: Session, data: unknown): Promise<void> {
    if (this.session !== session) return
    let text: string
    if (typeof data === 'string') text = data
    else if (data instanceof ArrayBuffer) text = Buffer.from(data).toString('utf8')
    else if (typeof Blob !== 'undefined' && data instanceof Blob) text = await data.text()
    else if (Buffer.isBuffer(data)) text = data.toString('utf8')
    else return
    if (text.length > 4_000_000) return
    let frame: unknown
    try {
      frame = JSON.parse(text)
    } catch {
      return
    }
    if (isRecord(frame) && isRecord(frame.usageMetadata)) {
      const usage = frame.usageMetadata
      this.diagnostics.record({
        kind: 'voice_session', provider: 'gemini', model: this.options.model, result: 'usage',
        ...(typeof usage.promptTokenCount === 'number' ? { inputTokens: usage.promptTokenCount } : {}),
        ...(typeof usage.responseTokenCount === 'number' ? { outputTokens: usage.responseTokenCount } : {})
      })
    }
    if (this.session !== session) return
    for (const event of projectServerFrame(frame)) this.options.emit(session.id, event)
  }
}

export function clientFrame(message: Exclude<VoiceRelayClientMessage, { kind: 'setup' }>): Json {
  switch (message.kind) {
    case 'audio':
      return { realtimeInput: { audio: { mimeType: 'audio/pcm;rate=16000', data: message.data } } }
    case 'audio_end':
      return { realtimeInput: { audioStreamEnd: true } }
    case 'text':
      return { clientContent: { turns: [{ role: 'user', parts: [{ text: message.text }] }], turnComplete: message.turnComplete } }
    case 'image':
      return {
        clientContent: {
          turns: [{ role: 'user', parts: [{ text: message.text }, { inlineData: { mimeType: message.mimeType, data: message.data } }] }],
          turnComplete: true
        }
      }
    case 'tool_response':
      return { toolResponse: { functionResponses: [{ id: message.id, name: message.name, response: message.output }] } }
    case 'harness_say':
      return { harnessSay: { text: message.text, transcriptAfterToolCall: message.transcriptAfterToolCall === true } }
  }
}
