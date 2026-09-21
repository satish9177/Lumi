/**
 * Shared, renderer-safe shapes for model routing, memory and diagnostics.
 *
 * Nothing here carries a credential, a provider endpoint, a prompt or a raw
 * model response. The renderer may display these; it can never use them to
 * reach a provider.
 */

/** The small, explicit set of reasons Lumi calls a non-realtime model. */
export const MODEL_TASK_CLASSES = [
  'intent_extraction',
  'constraint_extraction',
  'summarization',
  'conversation',
  'screen_understanding',
  'difficult_reasoning',
  /** Milestone 7a: answer the user's question from an untrusted page observation. */
  'page_answer',
  /** Milestone 7b: choose the one next bounded public-research step. */
  'research_planning',
  /** Milestone 7b: answer the research objective from collected observations. */
  'research_answer',
  /**
   * Milestone 8a S3: plan and answer from *account-private* observations. These two
   * classes never fail over: the caller must name the one approved recipient, and a
   * router asked to run them without that rule refuses before any provider is called.
   */
  'authenticated_planning',
  'authenticated_answer',
  /**
   * Milestone 8b S5: propose which saved detail belongs in which form field, from the form
   * structure and masked previews only. Private, one recipient, no failover, no image.
   */
  'form_planning',
  /**
   * Milestone 9 S2: answer one typed question from ONE redacted, user-approved desktop snapshot.
   * Private, one recipient, no failover, no image, read-only output.
   */
  'desktop_planning'
] as const
export type ModelTaskClass = typeof MODEL_TASK_CLASSES[number]

export const TEXT_PROVIDER_IDS = ['openai', 'gemini', 'deepseek', 'scripted'] as const
export type TextProviderId = typeof TEXT_PROVIDER_IDS[number]

export const VOICE_PROVIDER_IDS = ['openai', 'gemini'] as const
export type VoiceProviderId = typeof VOICE_PROVIDER_IDS[number]

/** Remembered user preferences. A closed set, each with a typed value. */
export const PREFERENCE_KEYS = ['preferred_part_of_day', 'max_price_inr', 'reply_language'] as const
export type PreferenceKey = typeof PREFERENCE_KEYS[number]

export const PREFERENCE_PARTS_OF_DAY = ['morning', 'afternoon', 'evening'] as const
export const PREFERENCE_LANGUAGES = ['English', 'Telugu', 'Hindi', 'Telugu-English'] as const

export type PreferenceValue =
  | { key: 'preferred_part_of_day'; value: typeof PREFERENCE_PARTS_OF_DAY[number] }
  | { key: 'max_price_inr'; value: number }
  | { key: 'reply_language'; value: typeof PREFERENCE_LANGUAGES[number] }

/** Where a remembered value came from. */
export interface MemoryProvenance {
  /** Only an explicit user statement can create a preference. */
  source: 'user_statement'
  /** The completed voice turn or typed request id it was stated in. */
  turnId: string
  recordedAt: string
}

export type AgentPreferenceView = PreferenceValue & { provenance: MemoryProvenance }

/** One redacted diagnostic line. No prompt, transcript, key or form value. */
export interface ModelDiagnosticView {
  at: string
  kind: 'model_call' | 'plan' | 'voice_session'
  taskId?: string
  taskRevision?: number
  provider?: string
  model?: string
  taskClass?: ModelTaskClass
  command?: string
  latencyMs?: number
  inputTokens?: number
  outputTokens?: number
  /** Approximate input tokens Lumi assembled, before the provider counted. */
  contextTokens?: number
  result: string
  attempt?: number
}
