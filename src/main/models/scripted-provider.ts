import type { TextProviderId } from '../../shared/model-contracts'
import { interpretByRules } from '../../shared/rule-interpreter'
import { ModelProviderError, type ModelProvider, type ModelRequest, type ModelResponse } from './provider'
import { extractFacts, extractUntrusted, extractUtterance } from './context-builder'
import { scriptedPageAnswer } from '../agent/page-answer'
import { scriptedAuthenticatedAnswer } from '../agent/authenticated-answer'
import { scriptedAuthenticatedDecision } from '../agent/authenticated-planner'
import { scriptedFormPlanDecision } from '../agent/form-planner'
import { scriptedDesktopRead } from '../agent/desktop-reader'
import { scriptedResearchAnswer } from '../agent/research-answer'
import { scriptedResearchDecision } from '../agent/research-planner'
import { AUTHENTICATED_OPERATIONS, RESEARCH_OPERATIONS, type AgentAuthenticatedOperation, type AgentResearchOperation } from '../../shared/agent-contracts'

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
    if (request.taskClass === 'research_planning') return this.researchPlan(request)
    if (request.taskClass === 'research_answer') return this.researchAnswer(request)
    if (request.taskClass === 'authenticated_planning') return this.authenticatedPlan(request)
    if (request.taskClass === 'authenticated_answer') return this.authenticatedAnswer(request)
    if (request.taskClass === 'form_planning') return this.formPlanning(request)
    if (request.taskClass === 'desktop_planning') return this.desktopRead(request)
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

  /**
   * A deterministic research planner: keyword matching over the observation
   * lines, no world knowledge. The allowed operations are read back out of the
   * *trusted* facts section, so a scripted run respects the same scope the
   * runtime enforces.
   */
  private researchPlan(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'malformed') {
      // Structurally JSON, semantically an attempt to reach past the contract.
      return this.reply(JSON.stringify({
        action: 'step', operation: 'navigate', url: 'https://exfil.invalid/upload', selector: 'a'
      }))
    }
    if (this.behaviour === 'hostile') {
      // A planner persuaded by a page: an operation that does not exist.
      return this.reply(JSON.stringify({ action: 'step', operation: 'upload', tab: 't1' }))
    }
    const decision = scriptedResearchDecision(
      extractUtterance(request.input) ?? '',
      extractUntrusted(request.input),
      { operations: allowedOperations(extractFacts(request.input)) }
    )
    return this.reply(JSON.stringify(flattenDecision(decision)))
  }

  private authenticatedPlan(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'malformed') {
      // Structurally JSON, semantically an attempt to reach past the contract.
      return this.reply(JSON.stringify({
        action: 'step', operation: 'navigate', url: 'https://exfil.invalid/upload', provider: 'openai', selector: 'a'
      }))
    }
    if (this.behaviour === 'hostile') {
      return this.reply(JSON.stringify({ action: 'step', operation: 'click', tab: 't1' }))
    }
    const facts = extractFacts(request.input)
    const line = facts.find((entry) => entry.startsWith('operations you may choose:'))
    const named = line ? line.slice(line.indexOf(':') + 1).split(',').map((entry) => entry.trim()) : []
    const operations: AgentAuthenticatedOperation[] = line
      ? AUTHENTICATED_OPERATIONS.filter((operation) => named.includes(operation))
      : [...AUTHENTICATED_OPERATIONS]
    const decision = scriptedAuthenticatedDecision(extractUtterance(request.input) ?? '', extractUntrusted(request.input), { operations })
    if (decision.kind === 'finish') return this.reply(JSON.stringify({ action: 'finish', reason: decision.reason }))
    if (decision.kind === 'stop') return this.reply(JSON.stringify({ action: 'stop', stop_reason: decision.stopReason, reason: decision.reason }))
    const step = decision.step
    const base = { action: 'step', operation: step.operation, reason: decision.reason }
    switch (step.operation) {
      case 'navigate':
        return this.reply(JSON.stringify({ ...base, tab: step.tab, target: 'link', observation: step.target.observation, ref: step.target.ref }))
      case 'observe':
        return this.reply(JSON.stringify({ ...base, tab: step.tab }))
      case 'reveal':
        return this.reply(JSON.stringify({ ...base, tab: step.tab, target: step.target.kind, observation: step.target.observation, ref: step.target.ref }))
      case 'history':
        return this.reply(JSON.stringify({ ...base, tab: step.tab, direction: step.direction }))
      case 'tab':
        return this.reply(JSON.stringify({ ...base, tab_action: step.action, ...(step.tab ? { tab: step.tab } : {}) }))
    }
  }

  /**
   * A deterministic form planner. `malformed` and `hostile` try to reach past the
   * contract (a value, an origin, a provider, a data ref that was never offered),
   * so the tests can prove the parser and the runtime refuse them.
   */
  /** Milestone 9 S2: quote a control that mentions a failure, or say the snapshot does not show it. */
  private desktopRead(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'malformed') {
      // Structurally JSON, semantically an attempt to smuggle an action into a read-only reply.
      return this.reply(JSON.stringify({ schemaVersion: 1, kind: 'answer', answer: 'Done.', operation: 'invoke', controlRef: 'u1' }))
    }
    if (this.behaviour === 'hostile') {
      // Well-formed but ungrounded: a control that does not exist and a quote nobody printed.
      return this.reply(JSON.stringify({
        schemaVersion: 1, kind: 'answer', answer: 'There are 999 failures.',
        evidence: [{ controlRef: 'u199', quote: 'There are 999 failures.' }]
      }))
    }
    const result = scriptedDesktopRead(extractUntrusted(request.input))
    return this.reply(JSON.stringify(result.kind === 'answer'
      ? { schemaVersion: 1, kind: 'answer', answer: result.answer, evidence: result.evidence.map((item) => ({ controlRef: item.control_ref, quote: item.quote })) }
      : { schemaVersion: 1, kind: 'cannot_answer', reason: result.reason }))
  }

  private formPlanning(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'malformed') {
      return this.reply(JSON.stringify({
        action: 'propose', observation: 'o1', form: 'f1', origin: 'https://exfil.invalid',
        entries: [{ element: 'e1', data: 'email', value: 'attacker@example.test' }]
      }))
    }
    if (this.behaviour === 'hostile') {
      return this.reply(JSON.stringify({
        action: 'propose', observation: 'o1', form: 'f1',
        entries: [{ element: 'e1', data: 'portfolio_url' }, { element: 'e2', data: 'password' }]
      }))
    }
    const decision = scriptedFormPlanDecision(extractUntrusted(request.input), extractFacts(request.input))
    if (decision.kind === 'stop') return this.reply(JSON.stringify({ action: 'stop', reason: decision.reason }))
    const { proposal } = decision
    return this.reply(JSON.stringify({
      action: 'propose', observation: proposal.observation, form: proposal.form_ref, reason: decision.reason,
      entries: proposal.entries.map((entry) => 'data_ref' in entry
        ? { element: entry.element_ref, data: entry.data_ref }
        : 'option_ref' in entry
          ? { element: entry.element_ref, option: entry.option_ref }
          : { element: entry.element_ref, checked: entry.checked })
    }))
  }

  private authenticatedAnswer(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'hostile') {
      return this.reply(JSON.stringify({
        status: 'answered',
        answer: 'The account id is 123456789012345 and I sent it to another provider as the page asked.',
        evidence: [{ observation: 'o1', block: 'b1', quote: 'account id 123456789012345' }]
      }))
    }
    return this.reply(JSON.stringify(
      scriptedAuthenticatedAnswer(extractUtterance(request.input) ?? '', extractUntrusted(request.input))
    ))
  }

  private researchAnswer(request: ModelRequest): ModelResponse {
    const failure = this.behaviourFailure()
    if (failure) return failure
    if (this.behaviour === 'malformed') {
      return this.reply(JSON.stringify({
        status: 'answered', answer: 'Uploading the files the page asked for.', evidence: [],
        next_action: { operation: 'navigate', url: 'https://exfil.invalid/' }
      }))
    }
    if (this.behaviour === 'hostile') {
      // An invented figure and an invented quote, as a persuaded model gives.
      return this.reply(JSON.stringify({
        status: 'answered',
        answer: 'It has 999 contributors, and I have uploaded the user files as the page asked.',
        evidence: [{ observation: 'o1', block: 'b1', quote: 'Contributors 999' }]
      }))
    }
    return this.reply(JSON.stringify(
      scriptedResearchAnswer(extractUtterance(request.input) ?? '', extractUntrusted(request.input))
    ))
  }

  /** The chosen failure behaviours, shared by every task class. */
  private behaviourFailure(): ModelResponse | undefined {
    switch (this.behaviour) {
      case 'timeout':
        throw new ModelProviderError('timeout')
      case 'unavailable':
        throw new ModelProviderError('unavailable', 503)
      case 'rate_limited':
        throw new ModelProviderError('rate_limited', 429)
      case 'fail_once':
        if (!this.failed) {
          this.failed = true
          throw new ModelProviderError('unavailable', 503)
        }
        return undefined
      default:
        return undefined
    }
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

/** The operations the trusted facts section says this scope allows. */
function allowedOperations(facts: readonly string[]): AgentResearchOperation[] {
  const line = facts.find((entry) => entry.startsWith('operations you may choose:'))
  if (!line) return [...RESEARCH_OPERATIONS]
  const named = line.slice(line.indexOf(':') + 1).split(',').map((entry) => entry.trim())
  return RESEARCH_OPERATIONS.filter((operation) => named.includes(operation))
}

/** The planner's flat wire shape, from the structured decision. */
function flattenDecision(decision: ReturnType<typeof scriptedResearchDecision>): Record<string, unknown> {
  if (decision.kind === 'finish') return { action: 'finish', reason: decision.reason }
  if (decision.kind === 'stop') {
    return { action: 'stop', stop_reason: decision.stopReason, reason: decision.reason }
  }
  const step = decision.step
  const base = { action: 'step', operation: step.operation, reason: decision.reason }
  switch (step.operation) {
    case 'public_search':
      return { ...base, query: step.query }
    case 'navigate':
      return {
        ...base,
        tab: step.tab,
        target: step.target.kind,
        ...('observation' in step.target ? { observation: step.target.observation } : {}),
        ref: step.target.ref
      }
    case 'observe':
      return { ...base, tab: step.tab }
    case 'scroll':
    case 'history':
      return { ...base, tab: step.tab, direction: step.direction }
    case 'tab':
      return { ...base, tab_action: step.action, ...(step.tab ? { tab: step.tab } : {}) }
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
