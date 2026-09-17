import {
  ModelProviderError,
  postJson,
  tokenCount,
  type ModelCapabilities,
  type ModelProvider,
  type ModelRequest,
  type ModelResponse
} from './provider'
import type { GoogleTokenSource } from './google-auth'

/**
 * The three hosted text providers. Each one is a translation between Lumi's
 * `ModelRequest` and one vendor's request/response shape, nothing more.
 * Keys and tokens are read in main and sent only to their own vendor.
 */

type Json = Record<string, unknown>

function records(value: unknown): Json[] {
  return Array.isArray(value) ? value.filter((item): item is Json => typeof item === 'object' && item !== null) : []
}

// ---- OpenAI (Responses API) ------------------------------------------------------

export class OpenAITextProvider implements ModelProvider {
  readonly id = 'openai' as const
  readonly capabilities: ModelCapabilities = { json: true, vision: true, contextTokens: 128_000 }

  constructor(
    readonly model: string,
    private readonly apiKey: () => string | undefined,
    private readonly fetchImpl: typeof globalThis.fetch = globalThis.fetch
  ) {}

  configured(): boolean {
    return Boolean(this.apiKey())
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    const key = this.apiKey()
    if (!key) throw new ModelProviderError('not_configured')
    const content: Json[] = [{ type: 'input_text', text: request.input }]
    if (request.image) {
      content.push({ type: 'input_image', image_url: `data:${request.image.mimeType};base64,${request.image.base64}`, detail: 'low' })
    }
    const body: Json = {
      model: this.model,
      max_output_tokens: request.maxOutputTokens,
      store: false,
      input: [
        { role: 'developer', content: [{ type: 'input_text', text: request.system }] },
        { role: 'user', content }
      ]
    }
    if (request.responseFormat === 'json') {
      body.text = { format: { type: 'json_object' } }
    }
    const value = await postJson(this.fetchImpl, 'https://api.openai.com/v1/responses', { Authorization: `Bearer ${key}` }, body, request.signal)
    const text = records(value.output)
      .filter((item) => item.type === 'message')
      .flatMap((item) => records(item.content))
      .filter((part) => part.type === 'output_text' && typeof part.text === 'string')
      .map((part) => part.text as string)
      .join('')
    if (!text) throw new ModelProviderError(value.status === 'incomplete' ? 'bad_response' : 'refused')
    const usage = (value.usage ?? {}) as Json
    return {
      text, provider: this.id, model: this.model,
      usage: { inputTokens: tokenCount(usage.input_tokens), outputTokens: tokenCount(usage.output_tokens) }
    }
  }
}

// ---- Gemini on Vertex AI -----------------------------------------------------------

export class GeminiVertexProvider implements ModelProvider {
  readonly id = 'gemini' as const
  readonly capabilities: ModelCapabilities = { json: true, vision: true, contextTokens: 1_000_000 }

  constructor(
    readonly model: string,
    private readonly tokens: GoogleTokenSource | undefined,
    private readonly location: string,
    private readonly fetchImpl: typeof globalThis.fetch = globalThis.fetch
  ) {}

  configured(): boolean {
    return this.tokens !== undefined && /^[a-z]+(?:-[a-z]+\d*)*$/.test(this.location)
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    if (!this.tokens || !this.configured()) throw new ModelProviderError('not_configured')
    let token: string
    let project: string
    try {
      [token, project] = await Promise.all([this.tokens.accessToken(), this.tokens.projectId()])
    } catch {
      throw new ModelProviderError('not_configured')
    }
    const host = this.location === 'global' ? 'aiplatform.googleapis.com' : `${this.location}-aiplatform.googleapis.com`
    const url = `https://${host}/v1/projects/${project}/locations/${this.location}/publishers/google/models/${encodeURIComponent(this.model)}:generateContent`
    const parts: Json[] = [{ text: request.input }]
    if (request.image) parts.push({ inlineData: { mimeType: request.image.mimeType, data: request.image.base64 } })
    const generationConfig: Json = { maxOutputTokens: request.maxOutputTokens, temperature: 0.1 }
    // JSON mode only. Measured on 2026-09-16 (docs/PROVIDERS.md): passing the
    // interpretation schema as responseSchema/responseJsonSchema made
    // gemini-2.5-flash(-lite) drop most optional fields, while the prompt plus
    // Lumi's strict validator produced complete plans. The schema is not sent.
    if (request.responseFormat === 'json') generationConfig.responseMimeType = 'application/json'
    // 2.5 models think by default; extraction does not need it and it eats the output budget.
    if (/2\.5-flash/.test(this.model)) generationConfig.thinkingConfig = { thinkingBudget: 0 }
    const value = await postJson(this.fetchImpl, url, { Authorization: `Bearer ${token}` }, {
      systemInstruction: { parts: [{ text: request.system }] },
      contents: [{ role: 'user', parts }],
      generationConfig
    }, request.signal)
    const candidate = records(value.candidates)[0]
    if (!candidate) throw new ModelProviderError('refused')
    const text = records((candidate.content as Json | undefined)?.parts)
      .filter((part) => typeof part.text === 'string' && part.thought !== true)
      .map((part) => part.text as string)
      .join('')
    if (!text) throw new ModelProviderError(candidate.finishReason === 'MAX_TOKENS' ? 'bad_response' : 'refused')
    const usage = (value.usageMetadata ?? {}) as Json
    return {
      text, provider: this.id, model: this.model,
      usage: { inputTokens: tokenCount(usage.promptTokenCount), outputTokens: tokenCount(usage.candidatesTokenCount) }
    }
  }
}

// ---- DeepSeek (OpenAI-compatible chat completions) --------------------------------------

export class DeepSeekProvider implements ModelProvider {
  readonly id = 'deepseek' as const
  readonly capabilities: ModelCapabilities = { json: true, vision: false, contextTokens: 64_000 }

  constructor(
    readonly model: string,
    private readonly apiKey: () => string | undefined,
    private readonly fetchImpl: typeof globalThis.fetch = globalThis.fetch
  ) {}

  configured(): boolean {
    return Boolean(this.apiKey())
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    const key = this.apiKey()
    if (!key) throw new ModelProviderError('not_configured')
    if (request.image) throw new ModelProviderError('unsupported')
    const body: Json = {
      model: this.model,
      max_tokens: request.maxOutputTokens,
      temperature: 0.1,
      messages: [
        { role: 'system', content: request.system },
        { role: 'user', content: request.input }
      ]
    }
    if (request.responseFormat === 'json') body.response_format = { type: 'json_object' }
    const value = await postJson(this.fetchImpl, 'https://api.deepseek.com/chat/completions', { Authorization: `Bearer ${key}` }, body, request.signal)
    const choice = records(value.choices)[0]
    const message = choice?.message as Json | undefined
    const text = typeof message?.content === 'string' ? message.content : ''
    if (!text) throw new ModelProviderError('refused')
    const usage = (value.usage ?? {}) as Json
    return {
      text, provider: this.id, model: this.model,
      usage: { inputTokens: tokenCount(usage.prompt_tokens), outputTokens: tokenCount(usage.completion_tokens) }
    }
  }
}
