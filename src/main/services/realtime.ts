import { createHash } from 'node:crypto'
import type { RealtimeSessionCredential } from '../../shared/contracts'

export const REALTIME_MODEL = process.env.LIFELENS_REALTIME_MODEL?.trim() || 'gpt-realtime-2.1'
const REQUEST_TIMEOUT_MS = 10_000
const REALTIME_REASONING_EFFORTS = ['low', 'medium', 'high'] as const

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

export async function createRealtimeSessionCredential(safetySeed: string): Promise<RealtimeSessionCredential> {
  const reasoningEffort = getRealtimeReasoningEffort()
  const apiKey = process.env.OPENAI_API_KEY?.trim()
  if (!apiKey) {
    return { mode: 'mock', model: REALTIME_MODEL }
  }

  console.info(`Realtime reasoning effort: ${reasoningEffort}`)
  const safetyIdentifier = createHash('sha256').update(safetySeed).digest('hex')
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
        session: {
          type: 'realtime',
          model: REALTIME_MODEL,
          reasoning: { effort: reasoningEffort },
          audio: { output: { voice: 'marin' } }
        }
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
