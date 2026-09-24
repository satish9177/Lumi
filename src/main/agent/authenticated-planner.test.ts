import { describe, expect, it } from 'vitest'
import {
  AUTHENTICATED_PLANNER_SCHEMA,
  AuthenticatedPlanError,
  authenticatedObservationLines,
  parseAuthenticatedDecision,
  scriptedAuthenticatedDecision
} from './authenticated-planner'
import { AUTHENTICATED_OPERATIONS, type AgentAuthenticatedObservationView } from '../../shared/agent-contracts'
import {
  ModelRouter, DEFAULT_ROUTES, PRIVATE_TASK_CLASSES, PRIVATE_VISION_TASK_CLASSES, PrivateRouteError
} from '../models/model-router'
import type { ModelProvider, ModelRequest, ModelResponse } from '../models/provider'

const ALL = { operations: [...AUTHENTICATED_OPERATIONS] }

function parses(reply: unknown): void {
  parseAuthenticatedDecision(JSON.stringify(reply), ALL)
}

function refused(reply: unknown, capabilities = ALL): string {
  try {
    parseAuthenticatedDecision(typeof reply === 'string' ? reply : JSON.stringify(reply), capabilities)
  } catch (error) {
    expect(error).toBeInstanceOf(AuthenticatedPlanError)
    return (error as AuthenticatedPlanError).code
  }
  throw new Error('the reply was accepted')
}

describe('the authenticated planner vocabulary', () => {
  it('is exactly navigate, observe, reveal, tab and history (plus finish and stop)', () => {
    expect([...AUTHENTICATED_OPERATIONS]).toEqual(['navigate', 'observe', 'reveal', 'tab', 'history'])
    expect(AUTHENTICATED_PLANNER_SCHEMA.properties.operation.enum).toEqual(['navigate', 'observe', 'reveal', 'tab', 'history'])
    expect(Object.keys(AUTHENTICATED_PLANNER_SCHEMA.properties).sort()).toEqual([
      'action', 'direction', 'observation', 'operation', 'reason', 'ref', 'stop_reason', 'tab', 'tab_action', 'target'
    ])
  })

  it('accepts one well-formed step of each kind', () => {
    parses({ action: 'step', operation: 'navigate', tab: 't1', target: 'link', observation: 'o1', ref: 'l2' })
    parses({ action: 'step', operation: 'observe', tab: 't1' })
    parses({ action: 'step', operation: 'reveal', tab: 't1', target: 'block', observation: 'o1', ref: 'b4' })
    parses({ action: 'step', operation: 'reveal', tab: 't1', target: 'link', observation: 'o1', ref: 'l4' })
    parses({ action: 'step', operation: 'history', tab: 't1', direction: 'back' })
    parses({ action: 'step', operation: 'tab', tab_action: 'open' })
    parses({ action: 'step', operation: 'tab', tab_action: 'close', tab: 't2' })
    parses({ action: 'finish', reason: 'done' })
    parses({ action: 'stop', stop_reason: 'outside_scope', reason: 'cannot' })
  })

  it.each([
    ['url', { url: 'https://evil.example' }],
    ['host', { host: 'evil.example' }],
    ['origin', { origin: 'https://evil.example' }],
    ['selector', { selector: 'button.delete' }],
    ['xpath', { xpath: '//button' }],
    ['javascript', { javascript: 'fetch("/x", {method: "POST"})' }],
    ['coordinates', { x: 10, y: 20 }],
    ['method', { method: 'POST' }],
    ['headers', { headers: { cookie: 'a=b' } }],
    ['cookies', { cookies: 'a=b' }],
    ['provider', { provider: 'openai' }],
    ['recipient', { recipient: 'openai' }],
    ['text', { text_to_type: 'hunter2' }],
    ['key', { key: 'Enter' }]
  ])('refuses a reply carrying %s, whole', (_name, extra) => {
    expect(refused({ action: 'step', operation: 'observe', tab: 't1', ...extra })).toBe('extra_fields')
  })

  it.each(['click', 'invoke', 'type', 'press', 'submit', 'upload', 'download', 'set_value', 'prepare_form', 'public_search', 'scroll'])(
    'has no %s operation', (operation) => {
      expect(refused({ action: 'step', operation, tab: 't1' })).toBe('operation')
    }
  )

  it('refuses a step outside the confirmed scope', () => {
    expect(refused({ action: 'step', operation: 'navigate', tab: 't1', target: 'link', observation: 'o1', ref: 'l1' }, { operations: ['observe'] }))
      .toBe('outside_scope')
  })

  it.each([
    [{ action: 'step', operation: 'navigate', tab: 't1', target: 'link', observation: 'o1', ref: 'https://x.example' }, 'invalid_ref'],
    [{ action: 'step', operation: 'navigate', tab: 't9', target: 'link', observation: 'o1', ref: 'l1' }, 'invalid_ref'],
    [{ action: 'step', operation: 'navigate', tab: 't1', target: 'link', observation: 'o1', ref: 'b1' }, 'invalid_ref'],
    [{ action: 'step', operation: 'navigate', tab: 't1', target: 'seed', observation: 'o1', ref: 's1' }, 'target'],
    [{ action: 'step', operation: 'reveal', tab: 't1', target: 'coordinate', observation: 'o1', ref: 'b1' }, 'target'],
    [{ action: 'step', operation: 'history', tab: 't1', direction: 'sideways' }, 'direction'],
    [{ action: 'step', operation: 'tab', tab_action: 'close' }, 'invalid_ref'],
    ['not json', 'not_json'],
    ['[]', 'malformed']
  ])('refuses a malformed ref, target or shape (%#)', (reply, code) => {
    expect(refused(reply as never)).toBe(code)
  })

  it('cannot express a tab beyond the account-reading budget', () => {
    expect(refused({ action: 'step', operation: 'observe', tab: 't4' })).toBe('invalid_ref')
  })
})

describe('what the planner is shown', () => {
  const observation: AgentAuthenticatedObservationView = {
    observationId: '00000000-0000-4000-8000-000000000001', ref: 'o1', sequence: 1, kind: 'page', operation: 'observe',
    tab: 't1', documentEpoch: 1, host: 'github.com', title: 'Your repositories', settled: true, truncated: false,
    observedAt: '2026-09-20T10:00:00+00:00', contentHash: 'a'.repeat(64),
    blocks: [{ id: 'b1', text: 'lumi-notes - Private' }], links: [{ ref: 'l1', text: 'Billing', host: 'github.com' }],
    openTabs: ['t1'], redactions: { email: 1 }
  }

  it('prints refs, redacted text and a host -- never an address', () => {
    const lines = authenticatedObservationLines({ observations: [observation] })
    expect(lines).toContain('[o1 b1] lumi-notes - Private')
    expect(lines).toContain('[o1 l1] link: Billing (github.com)')
    expect(lines.join('\n')).not.toMatch(/https?:\/\//)
  })

  it('summarises older observations rather than resending their text', () => {
    const many = Array.from({ length: 6 }, (_, index) => ({ ...observation, ref: `o${index + 1}`, sequence: index + 1 }))
    const lines = authenticatedObservationLines({ observations: many })
    expect(lines.filter((line) => line.includes('earlier, text no longer shown'))).toHaveLength(3)
  })
})

describe('the deterministic stand-in', () => {
  it('observes first, then finishes when the page answers the question', () => {
    const first = scriptedAuthenticatedDecision('Which of my repositories are private?', [], ALL)
    expect(first).toMatchObject({ kind: 'step', step: { operation: 'observe', tab: 't1' } })
    const lines = ['[o1] page in tab t1 on github.com: "Your repositories"', '[o1 b1] lumi-notes - Private', '[o1 b2] Your repositories']
    expect(scriptedAuthenticatedDecision('Which of my repositories are private?', lines, ALL)).toMatchObject({ kind: 'finish' })
  })
})

class RecordingProvider implements ModelProvider {
  readonly capabilities = { json: true, vision: true, contextTokens: 8_000 }
  readonly calls: ModelRequest[] = []
  constructor(readonly id: 'gemini' | 'openai', readonly model: string) {}
  configured(): boolean { return true }
  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    return { text: '{}', provider: this.id, model: this.model, usage: {} }
  }
}

describe('the router, for account-private classes', () => {
  const context = { rules: 'rules', utterance: 'question', untrusted: { label: 'pages', lines: ['[o1 b1] private text'] } }

  it('lists exactly the eight private classes, and exactly one is vision-capable', () => {
    expect([...PRIVATE_TASK_CLASSES].sort()).toEqual([
      'authenticated_answer', 'authenticated_planning', 'desktop_action_planning', 'desktop_planning',
      'desktop_vision', 'document_compare', 'form_planning', 'orchestration_planning'
    ])
    expect([...PRIVATE_VISION_TASK_CLASSES]).toEqual(['desktop_vision'])
    for (const taskClass of PRIVATE_TASK_CLASSES) {
      const expectVision = PRIVATE_VISION_TASK_CLASSES.includes(taskClass) ? true : undefined
      expect(DEFAULT_ROUTES[taskClass].vision).toBe(expectVision)
    }
  })

  it.each(PRIVATE_TASK_CLASSES)('refuses %s without the recipient rule, before any provider is called', async (taskClass) => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const b = new RecordingProvider('openai', 'gpt')
    const router = new ModelRouter((id) => (id === 'gemini' ? a : b))
    await expect(router.run({ taskClass, context, responseFormat: 'json', validate: () => 1 }))
      .rejects.toMatchObject({ reason: 'recipient_required' })
    await expect(router.run({ taskClass, context, responseFormat: 'json', validate: () => 1 }))
      .rejects.toBeInstanceOf(PrivateRouteError)
    expect(a.calls.length + b.calls.length).toBe(0)
  })

  const NON_VISION_PRIVATE_CLASSES = PRIVATE_TASK_CLASSES.filter(
    (taskClass) => !PRIVATE_VISION_TASK_CLASSES.includes(taskClass)
  )

  it.each(NON_VISION_PRIVATE_CLASSES)('refuses an image for %s, before any provider is called', async (taskClass) => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const router = new ModelRouter(() => a)
    await expect(router.run({
      taskClass, context, responseFormat: 'json', validate: () => 1, permits: () => true,
      image: { mimeType: 'image/png', base64: 'AAAA' }
    })).rejects.toMatchObject({ reason: 'image_forbidden' })
    expect(a.calls).toHaveLength(0)
  })

  it('accepts an image only for desktop_vision, the one reviewed exception, and still tries just one provider', async () => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const b = new RecordingProvider('openai', 'gpt')
    const router = new ModelRouter((id) => (id === 'gemini' ? a : b))
    await router.run({
      taskClass: 'desktop_vision', context, responseFormat: 'json', validate: () => 1, permits: () => true,
      image: { mimeType: 'image/png', base64: 'AAAA' }
    })
    expect(a.calls).toHaveLength(1)
    expect(a.calls[0].image).toEqual({ mimeType: 'image/png', base64: 'AAAA' })
    expect(b.calls).toHaveLength(0)
  })

  it('a text-only desktop_vision request is refused: this class is vision-capable, not vision-only, but the caller always supplies an image', async () => {
    // Documents the actual invariant precisely: `config.vision: true` means the router will only try
    // a vision-capable provider for this class, whether or not THIS request happens to carry an image.
    // desktop-vision.ts is the only caller and always sets `image`; nothing here forces that at the
    // router layer, so this test exists to make the boundary explicit rather than assumed.
    const nonVisionOnly = new RecordingProvider('openai', 'gpt')
    nonVisionOnly.capabilities.vision = false
    const router = new ModelRouter(() => nonVisionOnly)
    await expect(router.run({
      taskClass: 'desktop_vision', context, responseFormat: 'json', validate: () => 1, permits: () => true
    })).rejects.toBeInstanceOf(Error)
    expect(nonVisionOnly.calls).toHaveLength(0)
  })

  it('attempts at most one provider for a private class even when the rule permits several', async () => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const b = new RecordingProvider('openai', 'gpt')
    a.generate = async (request) => { a.calls.push(request); throw new Error('boom') }
    const router = new ModelRouter((id) => (id === 'gemini' ? a : b))
    await expect(router.run({
      taskClass: 'authenticated_answer', context, responseFormat: 'json', validate: () => 1, permits: () => true
    })).rejects.toThrow()
    expect(a.calls.length + b.calls.length).toBe(1)
  })

  it('counts an answer that fails validation as the one attempt, and tries nobody else', async () => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const b = new RecordingProvider('openai', 'gpt')
    const router = new ModelRouter((id) => (id === 'gemini' ? a : b))
    await expect(router.run({
      taskClass: 'authenticated_answer', context, responseFormat: 'json',
      validate: () => { throw new Error('not grounded') }, permits: () => true
    })).rejects.toThrow()
    expect(a.calls.length + b.calls.length).toBe(1)
  })

  it('keeps failover for public classes exactly as it was', async () => {
    const a = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const b = new RecordingProvider('openai', 'gpt')
    a.generate = async (request) => { a.calls.push(request); throw new Error('boom') }
    const router = new ModelRouter((id) => (id === 'gemini' ? a : b))
    await router.run({ taskClass: 'research_answer', context, responseFormat: 'json', validate: () => 1 })
    expect(a.calls).toHaveLength(1)
    expect(b.calls).toHaveLength(1)
  })
})
