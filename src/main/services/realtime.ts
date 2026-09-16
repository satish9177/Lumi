import { createHash } from 'node:crypto'
import type { RealtimeSessionCredential } from '../../shared/contracts'

export const REALTIME_MODEL = process.env.LIFELENS_REALTIME_MODEL?.trim() || 'gpt-realtime-2.1-mini'
const REQUEST_TIMEOUT_MS = 10_000
const REALTIME_REASONING_EFFORTS = ['low', 'medium', 'high'] as const

/**
 * Model-specific client-secret capabilities. Unknown overrides stay usable,
 * but deliberately receive no optional field until that capability is known.
 */
export const REALTIME_MODEL_CAPABILITIES = {
  'gpt-realtime-2.1-mini': { supportsReasoning: true },
  'gpt-realtime-2.1': { supportsReasoning: true },
  'gpt-realtime-2': { supportsReasoning: true },
  'gpt-realtime-mini': { supportsReasoning: false }
} as const

export function supportsRealtimeReasoning(model: string): boolean {
  return REALTIME_MODEL_CAPABILITIES[model as keyof typeof REALTIME_MODEL_CAPABILITIES]?.supportsReasoning === true
}

function isKnownLegacyNonReasoningModel(model: string): boolean {
  return REALTIME_MODEL_CAPABILITIES[model as keyof typeof REALTIME_MODEL_CAPABILITIES]?.supportsReasoning === false
}

export type RealtimeReasoningEffort = (typeof REALTIME_REASONING_EFFORTS)[number]

export function getRealtimeReasoningEffort(value = process.env.LIFELENS_REALTIME_REASONING): RealtimeReasoningEffort {
  const normalized = value?.trim().toLowerCase()
  if (!normalized) {
    return 'low'
  }

  if (REALTIME_REASONING_EFFORTS.includes(normalized as RealtimeReasoningEffort)) {
    return normalized as RealtimeReasoningEffort
  }

  throw new Error(
    `Invalid LIFELENS_REALTIME_REASONING. Expected one of: ${REALTIME_REASONING_EFFORTS.join(', ')}.`
  )
}

export const SCRIPTED_REALTIME_MODEL = 'scripted-test-voice'

/**
 * The deterministic scripted voice is for acceptance tests only: it needs an
 * explicit environment flag *and* an unpackaged build. It carries no token.
 */
export function scriptedRealtimeRequested(allowScripted: boolean, environment: NodeJS.ProcessEnv = process.env): boolean {
  return allowScripted && (environment.LUMI_REALTIME_SCRIPTED === '1' || environment.LUMI_REALTIME_SCRIPTED === 'gemini')
}

export type VoiceProviderChoice = 'openai' | 'gemini'

/**
 * Which realtime transport main offers. `LUMI_VOICE_PROVIDER=gemini` selects
 * Gemini Live on Vertex AI (relayed through main); anything else is OpenAI.
 * The scripted harness follows `LUMI_REALTIME_SCRIPTED` (`1` or `gemini`).
 */
export function voiceProviderChoice(allowScripted: boolean, environment: NodeJS.ProcessEnv = process.env): VoiceProviderChoice {
  if (scriptedRealtimeRequested(allowScripted, environment)) {
    return environment.LUMI_REALTIME_SCRIPTED === 'gemini' ? 'gemini' : 'openai'
  }
  return environment.LUMI_VOICE_PROVIDER?.trim().toLowerCase() === 'gemini' ? 'gemini' : 'openai'
}

export const GEMINI_LIVE_MODEL = process.env.LUMI_GEMINI_LIVE_MODEL?.trim() || 'gemini-live-2.5-flash-native-audio'

export async function createRealtimeSessionCredential(
  safetySeed: string,
  options: { allowScripted?: boolean; geminiConfigured?: boolean } = {}
): Promise<RealtimeSessionCredential> {
  const provider = voiceProviderChoice(options.allowScripted === true)
  if (scriptedRealtimeRequested(options.allowScripted === true)) {
    return { mode: 'scripted', model: SCRIPTED_REALTIME_MODEL, provider }
  }
  if (provider === 'gemini') {
    // No token crosses to the renderer: main relays the Live session.
    return options.geminiConfigured
      ? { mode: 'live', model: GEMINI_LIVE_MODEL, provider: 'gemini' }
      : { mode: 'mock', model: GEMINI_LIVE_MODEL, provider: 'gemini', configurationStatus: 'vertex_not_configured' }
  }
  const apiKey = process.env.OPENAI_API_KEY?.trim()
  if (!apiKey) {
    return { mode: 'mock', model: REALTIME_MODEL, configurationStatus: 'openai_api_key_missing' }
  }

  const includesReasoning = supportsRealtimeReasoning(REALTIME_MODEL)
  const reasoningEffort = includesReasoning ? getRealtimeReasoningEffort() : undefined
  if (includesReasoning) {
    console.info(`Realtime reasoning effort: ${reasoningEffort}`)
  } else if (isKnownLegacyNonReasoningModel(REALTIME_MODEL)) {
    console.warn(`Realtime model "${REALTIME_MODEL}" does not support reasoning.effort; omitting it.`)
  }
  const safetyIdentifier = createHash('sha256').update(safetySeed).digest('hex')
  const session: Record<string, unknown> = {
    type: 'realtime',
    model: REALTIME_MODEL,
    audio: { output: { voice: 'marin' } }
  }
  if (includesReasoning) {
    session.reasoning = { effort: reasoningEffort }
  }
  let response: Response
  try {
    response = await fetchWithTimeout('https://api.openai.com/v1/realtime/client_secrets', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'OpenAI-Safety-Identifier': safetyIdentifier
      },
      body: JSON.stringify({
        session
      })
    })
  } catch (error) {
    if (isTimeoutError(error)) {
      throw new Error('Realtime connection timed out while creating a temporary credential.')
    }
    throw error
  }

  if (!response.ok) {
    throw new Error(`Unable to create the Realtime session credential (status ${response.status}).`)
  }

  const data: unknown = await response.json()
  if (!isCredentialResponse(data)) {
    throw new Error('The Realtime session credential response was malformed.')
  }

  return {
    mode: 'live',
    model: REALTIME_MODEL,
    token: data.value,
    expiresAt: typeof data.expires_at === 'number' ? new Date(data.expires_at * 1_000).toISOString() : undefined
  }
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timeout)
  }
}

function isTimeoutError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
}

function isCredentialResponse(value: unknown): value is { value: string; expires_at?: number } {
  return (
    typeof value === 'object' &&
    value !== null &&
    'value' in value &&
    typeof value.value === 'string' &&
    value.value.length > 0 &&
    (!('expires_at' in value) || value.expires_at === undefined || typeof value.expires_at === 'number')
  )
}
