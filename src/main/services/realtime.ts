import { createHash } from 'node:crypto'
import type { RealtimeSessionCredential } from '../../shared/contracts'

export const REALTIME_MODEL = process.env.LIFELENS_REALTIME_MODEL?.trim() || 'gpt-realtime-2.1'

export async function createRealtimeSessionCredential(safetySeed: string): Promise<RealtimeSessionCredential> {
  const apiKey = process.env.OPENAI_API_KEY?.trim()
  if (!apiKey) {
    return { mode: 'mock', model: REALTIME_MODEL }
  }

  const safetyIdentifier = createHash('sha256').update(safetySeed).digest('hex')
  const response = await fetch('https://api.openai.com/v1/realtime/client_secrets', {
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
        audio: { output: { voice: 'marin' } }
      }
    })
  })

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
