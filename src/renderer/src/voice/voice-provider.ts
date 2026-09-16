/**
 * Lumi's realtime voice transport, independent of any vendor.
 *
 * A provider moves audio and events between the microphone/speaker and a
 * realtime model. It owns no task semantics: turn binding, tool routing,
 * de-duplication, narration and every durable decision stay in
 * `RealtimeClient` and Electron main. Providers translate their vendor's wire
 * protocol into the Lumi-owned events below and never invent an event the
 * vendor did not send.
 */

export type VoiceProviderId = 'openai' | 'gemini' | 'scripted'

export interface ProviderToolCall {
  callId: string
  name: string
  argumentsJson: string
  /** The model response this call belongs to, when the vendor says so. */
  responseId?: string
}

export type VoiceProviderEvent =
  /** The session configuration was accepted. */
  | { type: 'ready' }
  | { type: 'speech_started' }
  /** A spoken user turn exists; its final transcript follows. */
  | { type: 'turn_committed'; turnId: string }
  | { type: 'transcript_completed'; turnId?: string; text: string }
  | { type: 'transcript_failed'; turnId: string }
  | { type: 'response_started'; responseId?: string }
  | { type: 'response_text'; delta: string }
  | { type: 'response_done'; responseId?: string; text?: string; toolCalls: ProviderToolCall[] }
  | { type: 'tool_call'; call: ProviderToolCall }
  /** The user talked over the model; queued output was dropped. */
  | { type: 'interrupted' }
  | { type: 'error'; message: string }

export interface ProviderHandlers {
  onEvent: (event: VoiceProviderEvent) => void
  /** The transport failed or closed underneath the session. */
  onFailure: () => void
}

export interface ToolDefinition {
  type: string
  name: string
  description: string
  parameters: object
}

export interface VoiceSessionConfig {
  instructions: string
  tools: readonly ToolDefinition[]
  listening: boolean
  /** Ceiling for responses the server starts on its own (voice activity). */
  maxOutputTokens: number
}

export interface RealtimeVoiceProvider {
  readonly id: VoiceProviderId
  /** Resolves once events can flow. Rejects if the transport cannot open. */
  connect(handlers: ProviderHandlers): Promise<void>
  isOpen(): boolean
  /** Initial configuration. A provider answers with `ready`. */
  configure(config: VoiceSessionConfig): void
  updateInstructions(instructions: string): void
  setListening(enabled: boolean): void
  /** A typed user turn with a client-chosen id. */
  sendUserText(itemId: string, text: string): void
  /** App-authored context or a user-approved image, not bound to a user turn. */
  sendContext(content: { text: string; imageDataUrl?: string }): void
  sendToolResult(callId: string, output: string): void
  requestResponse(options: { maxOutputTokens: number; instructions?: string }): void
  cancelResponse(): void
  close(): void
}
