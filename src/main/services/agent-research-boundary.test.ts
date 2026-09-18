import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS, type AgentApi } from '../../shared/agent-contracts'
import { isAllowedRuntimeRoute, validateRuntimeSettings } from './agent-runtime-supervisor'
import { RESEARCH_PLANNER_SCHEMA } from '../agent/research-planner'
import { RESEARCH_ANSWER_SCHEMA } from '../agent/research-answer'
import { parseRuntimeConfig } from '../agent/packaged-runtime'

/**
 * The Milestone 7b boundary, stated as tests.
 *
 * Three things have to stay true however the feature grows: main can reach
 * only the research routes it needs; the renderer has channels for the trusted
 * click and for running the loop, but none that submits a step; and research
 * configuration is validated in main before it can reach the runtime's
 * environment.
 */

const TASK = '00000000-0000-4000-8000-000000000001'
const GRANT = '00000000-0000-4000-8000-000000000002'

describe('the runtime routes main may reach', () => {
  it('allows exactly the six research routes', () => {
    for (const [method, path] of [
      ['GET', `/tasks/${TASK}/research`],
      ['POST', `/tasks/${TASK}/research/prepare`],
      ['POST', `/tasks/${TASK}/research/grant`],
      ['POST', `/tasks/${TASK}/research/revoke`],
      ['POST', `/tasks/${TASK}/research/steps`],
      ['POST', `/tasks/${TASK}/research/answer`]
    ] as const) {
      expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    }
  })

  it('refuses anything else that looks like one', () => {
    for (const [method, path] of [
      ['POST', `/tasks/${TASK}/research`],
      ['GET', `/tasks/${TASK}/research/steps`],
      ['POST', `/tasks/${TASK}/research/execute`],
      ['POST', `/tasks/${TASK}/research/session`],
      ['POST', `/tasks/${TASK}/research/steps?url=https://x.invalid/`],
      ['POST', `/tasks/${TASK}/research/grant/../../lifecycle/shutdown`],
      ['POST', '/v1/sessions/open'],
      ['POST', '/v1/dispatch'],
      ['GET', `/tasks/${TASK}/research/observations`]
    ] as const) {
      expect(isAllowedRuntimeRoute(method, path), path).toBe(false)
    }
  })
})

describe('research configuration reaching the runtime', () => {
  it('passes validated settings and refuses bad ones', () => {
    expect(validateRuntimeSettings({
      researchAnyPublicHost: true,
      researchHosts: ['GitHub.com'],
      researchTestOrigins: ['http://127.0.0.1:8822'],
      researchSearchEndpoint: 'https://search.example.com/?q={query}'
    })).toEqual({
      LUMI_RESEARCH_ANY_PUBLIC_HOST: 'true',
      LUMI_RESEARCH_HOSTS: 'github.com',
      LUMI_RESEARCH_TEST_ORIGINS: 'http://127.0.0.1:8822',
      LUMI_RESEARCH_SEARCH_ENDPOINT: 'https://search.example.com/?q={query}'
    })
    // Research is absent unless it is asked for.
    expect(validateRuntimeSettings({})).toEqual({})
    expect(validateRuntimeSettings({ researchAnyPublicHost: false })).toEqual({})
  })

  it('refuses a research host or test origin that is not public', () => {
    expect(() => validateRuntimeSettings({ researchHosts: ['localhost'] })).toThrow()
    expect(() => validateRuntimeSettings({ researchHosts: ['192.168.1.1'] })).toThrow()
    expect(() => validateRuntimeSettings({ researchTestOrigins: ['http://10.0.0.1:80'] })).toThrow()
  })

  it('refuses a search endpoint that is not one templated http(s) URL', () => {
    for (const endpoint of [
      'https://search.example.com/?q=fixed',
      'https://search.example.com/?q={query}&r={query}',
      'ftp://search.example.com/?q={query}',
      'javascript:alert(1){query}',
      'https://search.example.com/?q={query}"'
    ]) {
      expect(() => validateRuntimeSettings({ researchSearchEndpoint: endpoint }), endpoint).toThrow()
    }
  })

  it('an installed app opts into research explicitly, and its endpoint must be https', () => {
    const base = { databaseUrl: 'postgresql+asyncpg://lumi:x@127.0.0.1:5432/lumi_agent' }
    const off = parseRuntimeConfig(base)
    expect(off.kind === 'ok' && off.config.research).toBe(false)
    const on = parseRuntimeConfig({ ...base, research: true, researchSearchEndpoint: 'https://s.example.com/?q={query}' })
    expect(on.kind === 'ok' && on.config.research).toBe(true)
    expect(parseRuntimeConfig({ ...base, researchSearchEndpoint: 'http://s.example.com/?q={query}' }).kind).toBe('invalid')
    expect(parseRuntimeConfig({ ...base, researchHosts: ['localhost'] }).kind).toBe('invalid')
    expect(parseRuntimeConfig({ ...base, researchEverything: true }).kind).toBe('invalid')
  })
})

describe('what the renderer can ask for', () => {
  it('has a channel for the trusted click, and none that submits a step', () => {
    const channels = Object.keys(AGENT_IPC_CHANNELS)
    for (const channel of ['createResearchTask', 'grantResearchScope', 'declineResearchScope', 'runResearch', 'stopResearch']) {
      expect(channels).toContain(channel)
    }
    for (const forbidden of [
      'researchStep', 'submitResearchStep', 'researchNavigate', 'researchSearch',
      'researchOpen', 'researchObserve', 'executeResearchStep'
    ]) {
      expect(channels).not.toContain(forbidden)
    }
    // No channel name carries a route, a method or a URL.
    for (const value of Object.values(AGENT_IPC_CHANNELS)) {
      expect(value.startsWith('lifelens:agent:')).toBe(true)
      expect(value).not.toMatch(/https?:|\/\//)
    }
  })

  it('types the research API as ids, revisions and one objective string', () => {
    // A compile-time shape, asserted here so a later widening is deliberate.
    const api: Pick<AgentApi, 'createResearchTask' | 'grantResearchScope' | 'runResearch'> = {
      createResearchTask: async () => ({ ok: false, error: { code: 'request_failed', message: 'x' } }),
      grantResearchScope: async () => ({ ok: false, error: { code: 'request_failed', message: 'x' } }),
      runResearch: async () => ({ ok: false, error: { code: 'request_failed', message: 'x' } })
    }
    expect(typeof api.createResearchTask).toBe('function')
  })
})

describe('what a model can say', () => {
  it('has no field for an address, a selector, a script or a tool, in either role', () => {
    for (const schema of [RESEARCH_PLANNER_SCHEMA, RESEARCH_ANSWER_SCHEMA]) {
      expect(schema.additionalProperties).toBe(false)
      const fields = Object.keys(schema.properties)
      for (const forbidden of [
        'url', 'href', 'address', 'selector', 'xpath', 'script', 'javascript', 'code',
        'method', 'headers', 'cookies', 'file', 'path', 'tool', 'next_action', 'approve'
      ]) {
        expect(fields, forbidden).not.toContain(forbidden)
      }
    }
  })
})
