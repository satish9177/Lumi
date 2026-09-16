import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRealtimeSessionCredential, getRealtimeReasoningEffort } from './realtime'

const originalApiKey = process.env.OPENAI_API_KEY
const originalReasoning = process.env.LIFELENS_REALTIME_REASONING
const originalModel = process.env.LIFELENS_REALTIME_MODEL

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  vi.resetModules()
  if (originalApiKey === undefined) {
    delete process.env.OPENAI_API_KEY
  } else {
    process.env.OPENAI_API_KEY = originalApiKey
  }
  if (originalReasoning === undefined) {
    delete process.env.LIFELENS_REALTIME_REASONING
  } else {
    process.env.LIFELENS_REALTIME_REASONING = originalReasoning
  }
  if (originalModel === undefined) {
    delete process.env.LIFELENS_REALTIME_MODEL
  } else {
    process.env.LIFELENS_REALTIME_MODEL = originalModel
  }
})

describe('Realtime model selection', () => {
  it('defaults to gpt-realtime-2.1-mini when the model environment variable is unset', async () => {
    vi.resetModules()
    vi.stubEnv('OPENAI_API_KEY', 'test-key')
    vi.stubEnv('LIFELENS_REALTIME_MODEL', '')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: 'temporary-token' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(console, 'info').mockImplementation(() => undefined)
    const { createRealtimeSessionCredential: createCredential } = await import('./realtime')

    await createCredential('test-user')

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(String(request.body)) as { session: { model?: string; reasoning?: { effort?: string } } }
    expect(body.session.model).toBe('gpt-realtime-2.1-mini')
    expect(body.session.reasoning).toEqual({ effort: 'low' })
  })

  it('honors the gpt-realtime-2.1 flagship override', async () => {
    vi.resetModules()
    vi.stubEnv('OPENAI_API_KEY', 'test-key')
    vi.stubEnv('LIFELENS_REALTIME_MODEL', 'gpt-realtime-2.1')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: 'temporary-token' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(console, 'info').mockImplementation(() => undefined)
    const { createRealtimeSessionCredential: createCredential } = await import('./realtime')

    await createCredential('test-user')

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(String(request.body)) as { session: { model?: string } }
    expect(body.session.model).toBe('gpt-realtime-2.1')
  })

  it('keeps the legacy gpt-realtime-mini override but omits unsupported reasoning', async () => {
    vi.resetModules()
    vi.stubEnv('OPENAI_API_KEY', 'test-key')
    vi.stubEnv('LIFELENS_REALTIME_MODEL', 'gpt-realtime-mini')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: 'temporary-token' }), { status: 200 }))
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', fetchMock)
    const { createRealtimeSessionCredential: createCredential } = await import('./realtime')

    await createCredential('test-user')

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(String(request.body)) as { session: { model?: string; reasoning?: unknown } }
    expect(body.session.model).toBe('gpt-realtime-mini')
    expect(body.session.reasoning).toBeUndefined()
    expect(warning).toHaveBeenCalledWith('Realtime model "gpt-realtime-mini" does not support reasoning.effort; omitting it.')
  })
})

describe('Realtime reasoning effort', () => {
  it('accepts low', () => {
    expect(getRealtimeReasoningEffort('low')).toBe('low')
  })

  it('defaults missing configuration to low', () => {
    expect(getRealtimeReasoningEffort(undefined)).toBe('low')
  })

  it('rejects unsupported configuration values', () => {
    expect(() => getRealtimeReasoningEffort('xhigh')).toThrow(
      'Invalid LIFELENS_REALTIME_REASONING. Expected one of: low, medium, high.'
    )
  })

  it('sends the validated effort only in the initial client-secret session configuration', async () => {
    process.env.OPENAI_API_KEY = 'test-key'
    process.env.LIFELENS_REALTIME_REASONING = 'low'
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: 'temporary-token' }), { status: 200 }))
    const info = vi.spyOn(console, 'info').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', fetchMock)

    await createRealtimeSessionCredential('test-user')

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(String(request.body)) as { session: { reasoning?: { effort?: string } } }
    expect(body.session.reasoning).toEqual({ effort: 'low' })
    expect(info).toHaveBeenCalledWith('Realtime reasoning effort: low')
  })
})

describe('scripted realtime gate', () => {
  it('issues the scripted test voice only for an unpackaged build with the explicit flag', async () => {
    const { scriptedRealtimeRequested, createRealtimeSessionCredential } = await import('./realtime')
    expect(scriptedRealtimeRequested(true, { LUMI_REALTIME_SCRIPTED: '1' })).toBe(true)
    expect(scriptedRealtimeRequested(false, { LUMI_REALTIME_SCRIPTED: '1' })).toBe(false)
    expect(scriptedRealtimeRequested(true, { LUMI_REALTIME_SCRIPTED: 'true' })).toBe(false)
    expect(scriptedRealtimeRequested(true, {})).toBe(false)
    // No key: the non-scripted path must stay offline in this test.
    vi.stubEnv('OPENAI_API_KEY', '')
    const previous = process.env.LUMI_REALTIME_SCRIPTED
    process.env.LUMI_REALTIME_SCRIPTED = '1'
    try {
      const packaged = await createRealtimeSessionCredential('seed', { allowScripted: false })
      expect(packaged.mode).not.toBe('scripted')
      const scripted = await createRealtimeSessionCredential('seed', { allowScripted: true })
      expect(scripted).toEqual({ mode: 'scripted', model: 'scripted-test-voice' })
    } finally {
      if (previous === undefined) delete process.env.LUMI_REALTIME_SCRIPTED
      else process.env.LUMI_REALTIME_SCRIPTED = previous
    }
  })
})
