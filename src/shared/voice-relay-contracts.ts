/**
 * The closed message set between the renderer's Gemini Live provider and the
 * relay Electron main runs.
 *
 * Why a relay: Vertex AI's Live API authenticates the WebSocket with a Google
 * OAuth access token in a request header. A browser WebSocket cannot set
 * headers, and handing the renderer a Google token would put a credential in
 * the least trusted process. So main owns the socket and the token; the
 * renderer sends and receives only these shapes, and main validates every one.
 *
 * Nothing here can name a model, an endpoint, a project or a credential.
 */

export const VOICE_RELAY_CHANNELS = {
  open: 'lifelens:voice-relay:open',
  send: 'lifelens:voice-relay:send',
  close: 'lifelens:voice-relay:close',
  event: 'lifelens:voice-relay:event'
} as const

export const MAX_RELAY_AUDIO_BASE64 = 96_000
export const MAX_RELAY_IMAGE_BASE64 = 6_000_000
export const MAX_RELAY_TEXT = 8_000
export const MAX_RELAY_INSTRUCTIONS = 40_000
export const MAX_RELAY_TOOLS = 24
export const MAX_RELAY_TOOL_OUTPUT = 64_000

/** A function the model may request. Parameters are a JSON schema subset. */
export interface RelayToolDeclaration {
  name: string
  description: string
  parameters: Record<string, unknown>
}

export type VoiceRelayClientMessage =
  | { kind: 'setup'; instructions: string; tools: RelayToolDeclaration[] }
  /** base64 PCM16 little-endian mono at 16 kHz. */
  | { kind: 'audio'; data: string }
  | { kind: 'audio_end' }
  /** A user or app-authored text turn. */
  | { kind: 'text'; text: string; turnComplete: boolean }
  | { kind: 'image'; text: string; mimeType: 'image/png' | 'image/jpeg'; data: string }
  | { kind: 'tool_response'; id: string; name: string; output: Record<string, unknown> }
  /** Scripted acceptance builds only: a spoken utterance to simulate. */
  | { kind: 'harness_say'; text: string; transcriptAfterToolCall?: boolean }

export type VoiceRelayServerEvent =
  | { kind: 'ready' }
  | { kind: 'input_transcript'; text: string; finished: boolean }
  | { kind: 'output_transcript'; text: string }
  | { kind: 'text'; text: string }
  /** base64 PCM16 little-endian mono at 24 kHz. */
  | { kind: 'audio'; data: string }
  | { kind: 'tool_call'; calls: Array<{ id: string; name: string; argumentsJson: string }> }
  | { kind: 'tool_cancel'; ids: string[] }
  | { kind: 'interrupted' }
  | { kind: 'turn_complete' }
  | { kind: 'go_away' }
  | { kind: 'closed'; reason: 'remote' | 'error' | 'replaced' | 'local' }

export interface VoiceRelayApi {
  open: () => Promise<{ ok: true; sessionId: string } | { ok: false; message: string }>
  send: (sessionId: string, message: VoiceRelayClientMessage) => Promise<boolean>
  close: (sessionId: string) => Promise<void>
  onEvent: (listener: (sessionId: string, event: VoiceRelayServerEvent) => void) => () => void
}

const JSON_TYPES: Record<string, string> = {
  string: 'STRING', integer: 'INTEGER', number: 'NUMBER', boolean: 'BOOLEAN', object: 'OBJECT', array: 'ARRAY'
}

/**
 * JSON schema (the subset Lumi's tools use) -> the Vertex `Schema` message.
 * Unsupported keywords are dropped; enum, pattern, bounds and required stay.
 */
export function toGeminiSchema(schema: unknown, depth = 0): Record<string, unknown> {
  if (depth > 8 || typeof schema !== 'object' || schema === null || Array.isArray(schema)) {
    throw new Error('Unsupported tool schema.')
  }
  const source = schema as Record<string, unknown>
  const type = typeof source.type === 'string' ? JSON_TYPES[source.type] : undefined
  if (!type) throw new Error('Unsupported tool schema type.')
  const result: Record<string, unknown> = { type }
  if (typeof source.description === 'string') result.description = source.description.slice(0, 1_000)
  if (Array.isArray(source.enum)) result.enum = source.enum.map(String)
  if (typeof source.pattern === 'string') result.pattern = source.pattern
  for (const key of ['minimum', 'maximum'] as const) {
    if (typeof source[key] === 'number') result[key] = source[key]
  }
  for (const [from, to] of [['maxLength', 'maxLength'], ['minItems', 'minItems'], ['maxItems', 'maxItems']] as const) {
    if (typeof source[from] === 'number') result[to] = String(source[from])
  }
  if (type === 'OBJECT') {
    const properties = (source.properties ?? {}) as Record<string, unknown>
    result.properties = Object.fromEntries(Object.entries(properties).map(([key, value]) => [key, toGeminiSchema(value, depth + 1)]))
    if (Array.isArray(source.required) && source.required.length > 0) result.required = source.required.map(String)
  }
  if (type === 'ARRAY') result.items = toGeminiSchema(source.items, depth + 1)
  return result
}
