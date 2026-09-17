import type { TextProviderId } from '../../shared/model-contracts'
import { interpretByRules } from '../../shared/rule-interpreter'
import { ModelProviderError, type ModelProvider, type ModelRequest, type ModelResponse } from './provider'
import { extractUntrusted, extractUtterance } from './context-builder'
import { scriptedPageAnswer } from '../agent/page-answer'

/**
 * A deterministic stand-in for a hosted text model, for tests and for the
 * unpackaged acceptance build only (`LUMI_SCRIPTED_MODELS`). It answers with
 * the shared English rule interpreter, or fails in a chosen way, so provider
 * failover can be exercised without paid credentials.
 */

export const SCRIPTED_BEHAVIOURS = ['rules', 'timeout', 'unavailable', 'rate_limited', 'malformed', 'fail_once', 'hostile'] as const
export type ScriptedBehaviour = typeof SCRIPTED_BEHAVIOURS[number]

export class ScriptedTextProvider implements ModelProvider {
  readonly capabilities = { json: true, vision: false, contextTokens: 32_000 }
  readonly calls: ModelRequest[] = []
  private failed = false

  constructor(
    readonly id: TextProviderId,
    private readonly behaviour: ScriptedBehaviour,
    readonly model = `scripted-${behaviour}`
  ) {}

  configured(): boolean {
    return true
  }

  async generate(request: ModelRequest): Promise<ModelResponse> {
    this.calls.push(request)
    if (request.taskClass === 'page_answer') return this.pageAnswer(request)
    switch (this.behaviour) {
      case 'timeout':
        throw new ModelProviderError('timeout')
      case 'unavailable':
        throw new ModelProviderError('unavailable', 503)
      case 'rate_limited':
        throw new ModelProviderError('rate_limited', 429)
      case 'malformed':
        // Structurally JSON, semantically an attempt to smuggle an action in.
        return this.reply('{"intent":"approve_booking","action_id":"all","execute":true}')
      case 'hostile':
        return this.reply('Sure! {"intent": "appointment_plan", "plan": {"search": {"specialty": "Dermatology", "url": "http://evil.example"}}}')
      case 'fail_once':
        if (!this.failed) {
          this.failed = true
          throw new ModelProviderError('unavailable', 503)
        }
        break
      case 'rules':
        break
    }
    return this.reply(JSON.stringify(interpretByRules(extractUtterance(request.input) ?? '')))
  }

  /** A deterministic reader of the observation: label lookup, no world knowledge. */
  private pageAnswer(request: ModelRequest): ModelResponse {
    switch (this.behaviour) {
      case 'timeout':
        throw new ModelProviderError('timeout')
      case 'unavailable':
        throw new ModelProviderError('unavailable', 503)
      case 'rate_limited':
        throw new ModelProviderError('rate_limited', 429)
      case 'malformed':
        // An attempt to turn page content into a next step.
        return this.reply(JSON.stringify({
          status: 'answered', answer: 'Opening the upload form now.', evidence: [],
          next_action: { operation: 'navigate', url: 'https://exfil.invalid/upload' }
        }))
      case 'hostile':
        // A model persuaded by the page: an invented value, an invented quote.
        return this.reply(JSON.stringify({
          status: 'answered', answer: 'Your contest rating is 9999. I have uploaded your files as the page asked.',
          evidence: [{ block: 'b1', quote: 'contest rating is 9999' }]
        }))
      case 'fail_once':
        if (!this.failed) {
          this.failed = true
          throw new ModelProviderError('unavailable', 503)
        }
        break
      case 'rules':
        break
    }
    return this.reply(JSON.stringify(scriptedPageAnswer(extractUtterance(request.input) ?? '', extractUntrusted(request.input))))
  }

  private reply(text: string): ModelResponse {
    return {
      text,
      provider: this.id,
      model: this.model,
      usage: { inputTokens: Math.ceil((this.calls.at(-1)?.input.length ?? 0) / 4), outputTokens: Math.ceil(text.length / 4) }
    }
  }
}

/** `deepseek:timeout,gemini:rules` -> providers in that order. */
export function parseScriptedModels(value: string): ScriptedTextProvider[] {
  return value.split(',').map((entry) => entry.trim()).filter(Boolean).map((entry) => {
    const [id, behaviour] = entry.split(':')
    if (!['openai', 'gemini', 'deepseek'].includes(id) || !(SCRIPTED_BEHAVIOURS as readonly string[]).includes(behaviour)) {
      throw new Error('LUMI_SCRIPTED_MODELS entries must be <openai|gemini|deepseek>:<behaviour>.')
    }
    return new ScriptedTextProvider(id as TextProviderId, behaviour as ScriptedBehaviour)
  })
}
