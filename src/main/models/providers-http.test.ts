import { generateKeyPairSync, createVerify } from 'node:crypto'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentMemoryStore, sanitizeSummary } from '../agent/agent-memory'
import { trustedCalendarClock } from '../agent/trusted-clock'
import { ApplicationDefaultCredentials, GoogleCredentialError, serviceAccountAssertion } from './google-auth'
import { createModelRouter } from './model-config'
import { ModelProviderError, type ModelRequest } from './provider'
import { DeepSeekProvider, GeminiVertexProvider, OpenAITextProvider } from './text-providers'

const REQUEST: ModelRequest = {
  taskClass: 'intent_extraction', system: 'SYSTEM', input: 'INPUT', responseFormat: 'json', maxOutputTokens: 123
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('hosted text providers', () => {
  it('OpenAI: Responses API shape, key only in the Authorization header, usage parsed', async () => {
    const fetch = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => jsonResponse({
      output: [{ type: 'message', content: [{ type: 'output_text', text: '{"intent":"status"}' }] }],
      usage: { input_tokens: 40, output_tokens: 7 }
    }))
    const provider = new OpenAITextProvider('gpt-test', () => 'sk-test-key', fetch)
    const response = await provider.generate(REQUEST)
    expect(response).toEqual({ text: '{"intent":"status"}', provider: 'openai', model: 'gpt-test', usage: { inputTokens: 40, outputTokens: 7 } })
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('https://api.openai.com/v1/responses')
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer sk-test-key')
    const body = JSON.parse(String(init?.body))
    expect(body).toMatchObject({ model: 'gpt-test', max_output_tokens: 123, store: false, text: { format: { type: 'json_object' } } })
    expect(String(init?.body)).not.toContain('sk-test-key')
    expect(init?.redirect).toBe('error')
  })

  it('Gemini: Vertex generateContent with a bearer token, thinking disabled for flash, JSON mime type', async () => {
    const fetch = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => jsonResponse({
      candidates: [{ content: { parts: [{ text: 'thinking...', thought: true }, { text: '{"intent":"conversation"}' }] }, finishReason: 'STOP' }],
      usageMetadata: { promptTokenCount: 55, candidatesTokenCount: 9 }
    }))
    const tokens = { accessToken: async () => 'ya29.test-token', projectId: async () => 'demo-project-1' }
    const provider = new GeminiVertexProvider('gemini-2.5-flash', tokens, 'us-central1', fetch)
    const response = await provider.generate(REQUEST)
    expect(response.text).toBe('{"intent":"conversation"}')
    expect(response.usage).toEqual({ inputTokens: 55, outputTokens: 9 })
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('https://us-central1-aiplatform.googleapis.com/v1/projects/demo-project-1/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent')
    const body = JSON.parse(String(init?.body))
    expect(body.generationConfig).toEqual({ maxOutputTokens: 123, temperature: 0.1, responseMimeType: 'application/json', thinkingConfig: { thinkingBudget: 0 } })
    expect(body.systemInstruction).toEqual({ parts: [{ text: 'SYSTEM' }] })
  })

  it('DeepSeek: chat completions, refuses images', async () => {
    const fetch = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => jsonResponse({ choices: [{ message: { content: '{"intent":"status"}' } }], usage: { prompt_tokens: 3, completion_tokens: 2 } }))
    const provider = new DeepSeekProvider('deepseek-chat', () => 'ds-key', fetch)
    expect((await provider.generate(REQUEST)).usage).toEqual({ inputTokens: 3, outputTokens: 2 })
    expect(fetch.mock.calls[0][0]).toBe('https://api.deepseek.com/chat/completions')
    await expect(provider.generate({ ...REQUEST, image: { mimeType: 'image/png', base64: 'AA' } })).rejects.toMatchObject({ kind: 'unsupported' })
  })

  it('classifies failures without ever carrying the provider body', async () => {
    const cases: Array<[Response | Error, string]> = [
      [jsonResponse({ error: { message: 'your key sk-live-SECRET is invalid' } }, 401), 'not_configured'],
      [jsonResponse({}, 429), 'rate_limited'],
      [jsonResponse({}, 503), 'unavailable'],
      [Object.assign(new Error('aborted'), { name: 'TimeoutError' }), 'timeout'],
      [new TypeError('fetch failed'), 'unavailable'],
      [new Response('not json', { status: 200 }), 'bad_response'],
      [jsonResponse({ choices: [] }), 'refused']
    ]
    for (const [outcome, kind] of cases) {
      const fetch = vi.fn(async () => { if (outcome instanceof Error) throw outcome; return outcome })
      const error = await new DeepSeekProvider('m', () => 'k', fetch).generate(REQUEST).catch((caught: unknown) => caught)
      expect(error).toBeInstanceOf(ModelProviderError)
      expect((error as ModelProviderError).kind).toBe(kind)
      expect((error as Error).message).not.toContain('SECRET')
    }
    await expect(new OpenAITextProvider('m', () => undefined).generate(REQUEST)).rejects.toMatchObject({ kind: 'not_configured' })
    await expect(new GeminiVertexProvider('m', undefined, 'us-central1').generate(REQUEST)).rejects.toMatchObject({ kind: 'not_configured' })
  })

  it('builds the router from main configuration only; scripted models need an unpackaged build', () => {
    const packaged = createModelRouter({ allowScripted: false, environment: { LUMI_SCRIPTED_MODELS: 'gemini:rules' } })
    expect(packaged.scripted).toBe(false)
    const development = createModelRouter({ allowScripted: true, environment: { LUMI_SCRIPTED_MODELS: 'gemini:rules' } })
    expect(development.scripted).toBe(true)
    expect(() => createModelRouter({ allowScripted: false, environment: { LUMI_MODEL_ROUTES: '{"x":1}' } })).toThrow()
    const plain = createModelRouter({ allowScripted: false, environment: {} })
    expect(plain.router.route('intent_extraction').providers[0].provider).toBe('deepseek')
  })
})

describe('Google application default credentials', () => {
  it('exchanges an authorized-user refresh token once and caches the access token', async () => {
    let now = 1_000_000
    const fetch = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => jsonResponse({ access_token: 'ya29.fresh-token-value', expires_in: 3600 }))
    const source = new ApplicationDefaultCredentials({
      environment: { GOOGLE_APPLICATION_CREDENTIALS: 'adc.json' },
      fetch,
      now: () => now,
      readFile: async () => JSON.stringify({ type: 'authorized_user', client_id: 'id', client_secret: 'secret', refresh_token: 'refresh', quota_project_id: 'my-project-42' })
    })
    const [first, second] = await Promise.all([source.accessToken(), source.accessToken()])
    expect(first).toBe('ya29.fresh-token-value')
    expect(second).toBe(first)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('https://oauth2.googleapis.com/token')
    expect(String(fetch.mock.calls[0][1]?.body)).toContain('grant_type=refresh_token')
    expect(await source.projectId()).toBe('my-project-42')
    now += 56 * 60_000
    await source.accessToken()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('signs a service-account assertion that Google can verify', () => {
    const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
    const assertion = serviceAccountAssertion({
      type: 'service_account', client_email: 'lumi@demo.iam.gserviceaccount.com',
      private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()
    }, 1_700_000_000)
    const [header, claims, signature] = assertion.split('.')
    const verifier = createVerify('RSA-SHA256')
    verifier.update(`${header}.${claims}`)
    expect(verifier.verify(publicKey, Buffer.from(signature, 'base64url'))).toBe(true)
    expect(JSON.parse(Buffer.from(claims, 'base64url').toString())).toMatchObject({
      aud: 'https://oauth2.googleapis.com/token', scope: 'https://www.googleapis.com/auth/cloud-platform', exp: 1_700_003_600
    })
  })

  it('reports missing or unusable credentials without detail', async () => {
    const missing = new ApplicationDefaultCredentials({ environment: {}, readFile: async () => { throw new Error('ENOENT C:/secret/path') } })
    await expect(missing.accessToken()).rejects.toEqual(new GoogleCredentialError('missing'))
    const refused = new ApplicationDefaultCredentials({
      environment: {},
      readFile: async () => JSON.stringify({ type: 'authorized_user', client_id: 'a', client_secret: 'b', refresh_token: 'c' }),
      fetch: async () => jsonResponse({ error: 'invalid_grant' }, 400)
    })
    await expect(refused.accessToken()).rejects.toMatchObject({ reason: 'exchange_failed' })
    await expect(refused.projectId()).rejects.toMatchObject({ reason: 'no_project' })
    const external = new ApplicationDefaultCredentials({ environment: {}, readFile: async () => JSON.stringify({ type: 'external_account' }) })
    await expect(external.accessToken()).rejects.toMatchObject({ reason: 'unsupported' })
  })
})

describe('agent memory store', () => {
  let directory: string
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'lumi-memory-')) })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('keeps one value per preference with provenance, and forgets on request', async () => {
    const store = new AgentMemoryStore(directory, () => Date.parse('2026-09-16T10:00:00Z'))
    await store.remember({ key: 'max_price_inr', value: 800 }, 'item_a')
    await store.remember({ key: 'max_price_inr', value: 900 }, 'req_b')
    await store.remember({ key: 'preferred_part_of_day', value: 'evening' }, 'item_c')
    expect(await store.preferences()).toEqual([
      { key: 'preferred_part_of_day', value: 'evening', provenance: { source: 'user_statement', turnId: 'item_c', recordedAt: '2026-09-16T10:00:00.000Z' } },
      { key: 'max_price_inr', value: 900, provenance: { source: 'user_statement', turnId: 'req_b', recordedAt: '2026-09-16T10:00:00.000Z' } }
    ])
    expect((await store.forget('max_price_inr')).map((item) => item.key)).toEqual(['preferred_part_of_day'])
    await expect(store.remember({ key: 'home_address', value: 'x' } as never, 'item_d')).rejects.toThrow()
  })

  it('ignores tampered entries and never stores unsafe summary text', async () => {
    const store = new AgentMemoryStore(directory)
    await store.recordEpisode({ taskId: '00000000-0000-4000-8000-000000000001', kind: 'booking_search', summary: 'Searched <script>alert(1)</script> Dr A 800', sequence: 3 })
    const raw = JSON.parse(await readFile(join(directory, 'agent-memory.json'), 'utf8'))
    expect(raw.episodes[0].summary).not.toMatch(/[<>]/)
    raw.preferences.push({ key: 'max_price_inr', value: 'free', provenance: { source: 'model', turnId: 'x', recordedAt: 'now' } })
    raw.episodes.push({ taskId: 'bad', kind: 'booking_search', summary: 'x', recordedAt: 'now', provenance: {} })
    await (await import('node:fs/promises')).writeFile(join(directory, 'agent-memory.json'), JSON.stringify(raw))
    expect(await store.preferences()).toEqual([])
    expect(await store.episodes()).toHaveLength(1)
    expect(sanitizeSummary('a'.repeat(500))).toHaveLength(240)
  })
})

describe('trusted calendar clock', () => {
  it('honours a fixed calendar date only when allowed, and a valid time zone only', () => {
    const fixed = trustedCalendarClock({ allowFixedNow: true, environment: { LUMI_FIXED_NOW: '2026-09-16T04:30:00Z', LUMI_TIMEZONE: 'Asia/Kolkata' } })
    expect(Math.abs(fixed.now() - Date.parse('2026-09-16T04:30:00Z'))).toBeLessThan(5_000)
    expect(fixed.timeZone()).toBe('Asia/Kolkata')
    const packaged = trustedCalendarClock({ allowFixedNow: false, environment: { LUMI_FIXED_NOW: '2000-01-01T00:00:00Z', LUMI_TIMEZONE: 'Not/AZone' } })
    expect(Math.abs(packaged.now() - Date.now())).toBeLessThan(5_000)
    expect(packaged.timeZone()).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
  })
})
