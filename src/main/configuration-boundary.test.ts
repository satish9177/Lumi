import { readFile } from 'node:fs/promises'
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('main-process OpenAI configuration boundary', () => {
  it('selects mock mode with an app-authored status only when the API key is unavailable', async () => {
    vi.resetModules()
    vi.stubEnv('OPENAI_API_KEY', '   ')
    const { createRealtimeSessionCredential: createCredential } = await import('./services/realtime')

    await expect(createCredential('test-user')).resolves.toEqual({
      mode: 'mock',
      model: 'gpt-realtime-2.1-mini',
      configurationStatus: 'openai_api_key_missing'
    })
  })

  it('keeps a configured API key in main and uses it only to mint a temporary credential', async () => {
    vi.resetModules()
    vi.stubEnv('OPENAI_API_KEY', 'main-process-only-key')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: 'temporary-token' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(console, 'info').mockImplementation(() => undefined)
    const { createRealtimeSessionCredential: createCredential } = await import('./services/realtime')

    await expect(createCredential('test-user')).resolves.toEqual({
      mode: 'live',
      model: 'gpt-realtime-2.1-mini',
      token: 'temporary-token',
      expiresAt: undefined
    })
  })

  it('keeps the permanent key out of renderer and preload contracts', async () => {
    const [preload, contracts, renderer] = await Promise.all([
      readFile(new URL('../preload/index.ts', import.meta.url), 'utf8'),
      readFile(new URL('../shared/contracts.ts', import.meta.url), 'utf8'),
      readFile(new URL('../renderer/src/LifeLensApp.tsx', import.meta.url), 'utf8')
    ])

    for (const source of [preload, contracts, renderer]) {
      expect(source).not.toContain('OPENAI_API_KEY')
      expect(source).not.toContain('VITE_OPENAI_API_KEY')
      expect(source).not.toContain('import.meta.env')
    }
  })
})
