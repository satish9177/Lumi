import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRealtimeSessionCredential, getRealtimeReasoningEffort } from './realtime'

const originalApiKey = process.env.OPENAI_API_KEY
const originalReasoning = process.env.LIFELENS_REALTIME_REASONING

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
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
