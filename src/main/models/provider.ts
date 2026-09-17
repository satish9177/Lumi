import type { ModelTaskClass, TextProviderId } from '../../shared/model-contracts'

/**
 * The narrow interface Lumi needs from a non-realtime model.
 *
 * A provider turns bounded text (and optionally one image) into text or JSON.
 * It never receives a tool it could call, never sees a credential other than
 * its own, and nothing it returns executes: callers validate the output into a
 * closed type and hand that to the durable controller, which decides.
 */

export interface ModelRequest {
  taskClass: ModelTaskClass
  /** App-authored instructions, including the security rules. */
  system: string
  /** The budgeted context assembled by the context builder. */
  input: string
  /** One user-approved image, for multimodal task classes only. */
  image?: { mimeType: 'image/png' | 'image/jpeg'; base64: string }
  responseFormat: 'json' | 'text'
  /** A JSON schema the provider may use to constrain output. Always re-validated. */
  jsonSchema?: Record<string, unknown>
  maxOutputTokens: number
  signal?: AbortSignal
}

export interface ModelUsage {
  inputTokens?: number
  outputTokens?: number
}

export interface ModelResponse {
  text: string
  provider: TextProviderId
  model: string
  usage: ModelUsage
}

export type ModelFailureKind =
  | 'not_configured'
  | 'unavailable'
  | 'timeout'
  | 'rate_limited'
  | 'refused'
  | 'bad_response'
  | 'unsupported'

/** A model call failed. Safe to show: it never carries a provider body or key. */
export class ModelProviderError extends Error {
  constructor(readonly kind: ModelFailureKind, readonly status?: number) {
    super(`Model call failed (${kind}${status ? ` ${status}` : ''}).`)
    this.name = 'ModelProviderError'
  }

  /** Whether a *different* provider may reasonably be tried. */
  get failover(): boolean {
    return this.kind !== 'refused'
  }
}

export interface ModelCapabilities {
  json: boolean
  vision: boolean
  /** Rough context window, in tokens. */
  contextTokens: number
}

export interface ModelProvider {
  readonly id: TextProviderId
  readonly model: string
  readonly capabilities: ModelCapabilities
  /** Whether credentials/configuration exist. Never performs a network call. */
  configured(): boolean
  generate(request: ModelRequest): Promise<ModelResponse>
}

export function classifyHttpStatus(status: number): ModelFailureKind {
  if (status === 401 || status === 403) return 'not_configured'
  if (status === 408) return 'timeout'
  if (status === 429) return 'rate_limited'
  if (status === 400 || status === 404 || status === 422) return 'unsupported'
  return 'unavailable'
}

export function isAbort(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error &&
    (error.name === 'AbortError' || error.name === 'TimeoutError')
}

/** POST JSON and classify every failure into a ModelProviderError. */
export async function postJson(
  fetchImpl: typeof globalThis.fetch,
  url: string,
  headers: Record<string, string>,
  body: unknown,
  signal?: AbortSignal
): Promise<Record<string, unknown>> {
  let response: Response
  try {
    response = await fetchImpl(url, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      redirect: 'error',
      ...(signal ? { signal } : {})
    })
  } catch (error) {
    throw new ModelProviderError(isAbort(error) ? 'timeout' : 'unavailable')
  }
  if (!response.ok) {
    // The body is never read into an error: it may echo the request.
    throw new ModelProviderError(classifyHttpStatus(response.status), response.status)
  }
  let value: unknown
  try {
    value = await response.json()
  } catch (error) {
    throw new ModelProviderError(isAbort(error) ? 'timeout' : 'bad_response')
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ModelProviderError('bad_response')
  return value as Record<string, unknown>
}

export function tokenCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}
