import { afterEach, describe, expect, it, vi } from 'vitest'
import { createScreenReasoningSummary, getReasoningEffort, parseScreenReasoningSummary } from './screen-reasoning'

const originalApiKey = process.env.OPENAI_API_KEY
const originalModel = process.env.LUMI_REASONING_MODEL
const originalEffort = process.env.LUMI_REASONING_EFFORT

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetModules()
  if (originalApiKey === undefined) delete process.env.OPENAI_API_KEY
  else process.env.OPENAI_API_KEY = originalApiKey
  if (originalModel === undefined) delete process.env.LUMI_REASONING_MODEL
  else process.env.LUMI_REASONING_MODEL = originalModel
  if (originalEffort === undefined) delete process.env.LUMI_REASONING_EFFORT
  else process.env.LUMI_REASONING_EFFORT = originalEffort
})

const validBrief = {
  summary: 'Interview invitation with a deadline tomorrow.',
  dates: ['Tomorrow'],
  links: ['https://example.com/interview'],
  risks: ['The preparation deadline is tomorrow.'],
  next_actions: ['Review the interview details.']
}

describe('screen reasoning schema validation', () => {
  it('maps the closed OpenAI schema into Lumi’s public contract', () => {
    expect(parseScreenReasoningSummary(validBrief, 'capture-1')).toEqual({
      sourceCaptureId: 'capture-1',
      summary: validBrief.summary,
      dates: validBrief.dates,
      links: validBrief.links,
      risks: validBrief.risks,
      nextActions: validBrief.next_actions
    })
  })

  it('rejects unexpected fields and unsafe links', () => {
    expect(() => parseScreenReasoningSummary({ ...validBrief, secret: 'no' }, 'capture-1')).toThrow('closed schema')
    expect(() => parseScreenReasoningSummary({ ...validBrief, links: ['file:///C:/secret.txt'] }, 'capture-1')).toThrow('http or https')
  })

  it('uses low reasoning effort by default and validates overrides', () => {
    expect(getReasoningEffort(undefined)).toBe('low')
    expect(getReasoningEffort('high')).toBe('high')
    expect(() => getReasoningEffort('max')).toThrow('LUMI_REASONING_EFFORT')
  })
})

describe('screen reasoning request', () => {
  it('sends one retained capture to the verified GPT-5.6 Responses API flow', async () => {
    vi.stubEnv('OPENAI_API_KEY', 'test-key')
    vi.stubEnv('LUMI_REASONING_MODEL', '')
    vi.stubEnv('LUMI_REASONING_EFFORT', 'low')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify(validBrief) }] }]
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const { createScreenReasoningSummary: createSummary } = await import('./screen-reasoning')

    const summary = await createSummary({ id: 'capture-1', dataUrl: 'data:image/jpeg;base64,AA==' }, 'test-user')

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(String(request.body)) as {
      model: string
      reasoning: { effort: string }
      store: boolean
      text: { format: { strict: boolean } }
      input: Array<{ content: Array<{ type: string; image_url?: string }> }>
    }
    expect(body.model).toBe('gpt-5.6-terra')
    expect(body.reasoning).toEqual({ effort: 'low' })
    expect(body.store).toBe(false)
    expect(body.text.format.strict).toBe(true)
    expect(body.input[1]?.content[1]).toEqual({ type: 'input_image', image_url: 'data:image/jpeg;base64,AA==', detail: 'auto' })
    expect(summary.sourceCaptureId).toBe('capture-1')
  })

  it('does not make a request without a main-process API key', async () => {
    vi.stubEnv('OPENAI_API_KEY', '')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(createScreenReasoningSummary({ id: 'capture-1', dataUrl: 'data:image/jpeg;base64,AA==' }, 'test-user'))
      .rejects.toThrow('OPENAI_API_KEY')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
