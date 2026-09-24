import {
  MODEL_TASK_CLASSES,
  TEXT_PROVIDER_IDS,
  type ModelTaskClass,
  type TextProviderId
} from '../../shared/model-contracts'
import { NO_DIAGNOSTICS, type DiagnosticsSink } from '../agent/diagnostics'
import { buildContext, type BuiltContext, type ContextInput } from './context-builder'
import { ModelProviderError, type ModelFailureKind, type ModelProvider, type ModelRequest } from './provider'

/**
 * Capability- and cost-aware routing for non-realtime model calls.
 *
 * The routing table is configuration, not code: each task class lists the
 * providers to try, in order, with its own input and output token budgets and
 * timeout. Domain code asks for a *task class*; it never names a vendor.
 *
 * Failover repeats only the model call. A model call has no side effects, so
 * trying another provider is safe; nothing a model returns is executed before
 * the caller validates it and hands it to the durable controller exactly once.
 * Output that fails validation counts as that provider failing.
 */

export interface RouteEntry {
  provider: TextProviderId
  /** Optional per-class model override, e.g. a cheaper variant. */
  model?: string
}

export interface RouteConfig {
  providers: RouteEntry[]
  maxInputTokens: number
  maxOutputTokens: number
  timeoutMs: number
  /** Needs an image-capable model. */
  vision?: boolean
}

export type RoutingTable = Record<ModelTaskClass, RouteConfig>

/**
 * Defaults. Cheap, fast models for structured extraction; Gemini where
 * multimodal input or a long context helps; the stronger OpenAI model as the
 * fallback for hard reasoning. A class with no configured provider simply
 * fails over to deterministic code in the caller.
 */
export const DEFAULT_ROUTES: RoutingTable = {
  intent_extraction: {
    providers: [{ provider: 'deepseek' }, { provider: 'gemini', model: 'gemini-2.5-flash-lite' }, { provider: 'openai' }],
    maxInputTokens: 2_000, maxOutputTokens: 400, timeoutMs: 12_000
  },
  constraint_extraction: {
    providers: [{ provider: 'deepseek' }, { provider: 'gemini', model: 'gemini-2.5-flash-lite' }, { provider: 'openai' }],
    maxInputTokens: 1_500, maxOutputTokens: 300, timeoutMs: 10_000
  },
  summarization: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash-lite' }, { provider: 'deepseek' }, { provider: 'openai' }],
    maxInputTokens: 4_000, maxOutputTokens: 256, timeoutMs: 12_000
  },
  conversation: {
    providers: [{ provider: 'gemini' }, { provider: 'deepseek' }, { provider: 'openai' }],
    maxInputTokens: 4_000, maxOutputTokens: 400, timeoutMs: 15_000
  },
  screen_understanding: {
    providers: [{ provider: 'gemini' }, { provider: 'openai' }],
    maxInputTokens: 8_000, maxOutputTokens: 900, timeoutMs: 30_000, vision: true
  },
  difficult_reasoning: {
    providers: [{ provider: 'openai' }, { provider: 'gemini', model: 'gemini-2.5-pro' }, { provider: 'deepseek' }],
    maxInputTokens: 16_000, maxOutputTokens: 2_000, timeoutMs: 60_000
  },
  // Milestone 7a. A bounded page observation plus the question; every
  // provider here receives page text only if the user's approval named it.
  page_answer: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 6_000, maxOutputTokens: 600, timeoutMs: 30_000
  },
  // Milestone 7b. One step per call, so the budget is per call and small; the
  // task-wide token budget is enforced by the research loop, not here.
  research_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 700, timeoutMs: 30_000
  },
  research_answer: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 12_000, maxOutputTokens: 900, timeoutMs: 45_000
  },
  // Milestone 8a S3. These list *candidates*, in the order the trusted card may
  // offer them. They are never a failover chain: a request for either class
  // must carry the grant's one-recipient `permits` rule (the router refuses
  // otherwise), and the first provider it attempts is the last.
  authenticated_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 700, timeoutMs: 30_000
  },
  authenticated_answer: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 12_000, maxOutputTokens: 900, timeoutMs: 45_000
  },
  // Milestone 8b S5. Same rules as the two above: candidates, never a failover chain.
  form_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 900, timeoutMs: 30_000
  },
  // Milestone 9 S2. A candidate list, never a failover chain: the request must carry the
  // approval's one-recipient `permits` rule and the first provider attempted is the last.
  desktop_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 700, timeoutMs: 30_000
  },
  // Milestone 9 S4. A candidate list, never a failover chain, like `desktop_planning`. The output
  // schema is a closed action proposal, not prose, so the budget is small.
  desktop_action_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 400, timeoutMs: 30_000
  },
  // Milestone 9 S5. A candidate list, never a failover chain, like `desktop_planning`/
  // `desktop_action_planning`. The ONE class in this table with `vision: true`: see
  // `PRIVATE_VISION_TASK_CLASSES` below for the structural rule that makes this the only class an
  // image can ever reach.
  desktop_vision: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }],
    maxInputTokens: 4_000, maxOutputTokens: 500, timeoutMs: 30_000, vision: true
  },
  // Milestone 10 S1. A candidate list, never a failover chain: the request carries the approval's
  // one-recipient `permits` rule and the first provider attempted is the last. Text only.
  document_compare: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 900, timeoutMs: 45_000
  },
  // Milestone 11 S2. One step per call, like `research_planning`: the task-wide budget is enforced by the
  // orchestration loop, not here. Not private (see `PRIVATE_TASK_CLASSES` below): its context is
  // controller-authored summaries only, never a private capability's own raw evidence.
  orchestration_planning: {
    providers: [{ provider: 'gemini', model: 'gemini-2.5-flash' }, { provider: 'openai' }, { provider: 'deepseek' }],
    maxInputTokens: 8_000, maxOutputTokens: 500, timeoutMs: 30_000
  }
}

/**
 * Task classes whose input can contain account-private text. Two structural
 * rules apply to them, and neither can be configured away:
 *
 * - **One recipient, zero failover.** `permits` is mandatory, and the first
 *   provider actually attempted is the last: a failure, a timeout or output
 *   that fails validation ends the run rather than trying another company (or
 *   another model of the same one) with the same private page.
 * - **No image, unless the class is also in `PRIVATE_VISION_TASK_CLASSES`.** An authenticated
 *   screenshot never reaches any provider that is merely private; only the one, separately reviewed,
 *   vision-capable class below may carry an image at all.
 */
export const PRIVATE_TASK_CLASSES: readonly ModelTaskClass[] = [
  'authenticated_planning', 'authenticated_answer', 'form_planning', 'desktop_planning',
  'desktop_action_planning', 'desktop_vision', 'document_compare'
]

/**
 * The ONE narrow exception to `PRIVATE_TASK_CLASSES`'s "no image" rule. Milestone 9 S5's trusted
 * vision-disclosure card is a SEPARATE approval from any text disclosure (S2/S4): naming a class here
 * does not weaken the image rule for any other private class, and the caller (`desktop-vision.ts`) is
 * the only place that ever sets `taskClass: 'desktop_vision'`, exactly the same trust boundary the five
 * other private classes already rely on for their own task class. Being private already means this
 * class gets `permits` + zero-failover for free (`isPrivate` breaks after one provider attempt
 * regardless of outcome); this constant adds exactly one more thing: permission to carry `request.image`.
 */
export const PRIVATE_VISION_TASK_CLASSES: readonly ModelTaskClass[] = ['desktop_vision']

/** A private request was made without the rule that names its one recipient. */
export class PrivateRouteError extends Error {
  constructor(readonly taskClass: ModelTaskClass, readonly reason: 'recipient_required' | 'image_forbidden') {
    super(`A private model call (${taskClass}) was refused (${reason}).`)
    this.name = 'PrivateRouteError'
  }
}

const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

/**
 * `LUMI_MODEL_ROUTES` override, JSON: {"intent_extraction": {"providers":
 * ["gemini:gemini-2.5-flash", "openai"], "maxOutputTokens": 300}}. Unknown
 * classes, providers or fields are refused so a typo cannot silently route
 * somewhere unexpected.
 */
export function parseRoutingOverrides(raw: string | undefined, base: RoutingTable = DEFAULT_ROUTES): RoutingTable {
  const table: RoutingTable = structuredClone(base)
  if (!raw?.trim()) return table
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    throw new Error('LUMI_MODEL_ROUTES must be JSON.')
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('LUMI_MODEL_ROUTES must be an object.')
  for (const [taskClass, override] of Object.entries(value)) {
    if (!(MODEL_TASK_CLASSES as readonly string[]).includes(taskClass)) throw new Error(`Unknown task class ${taskClass}.`)
    if (typeof override !== 'object' || override === null) throw new Error(`Route for ${taskClass} must be an object.`)
    const entry = override as Record<string, unknown>
    for (const key of Object.keys(entry)) {
      if (!['providers', 'maxInputTokens', 'maxOutputTokens', 'timeoutMs'].includes(key)) throw new Error(`Unknown route field ${key}.`)
    }
    const route = table[taskClass as ModelTaskClass]
    if (entry.providers !== undefined) {
      if (!Array.isArray(entry.providers) || entry.providers.length === 0 || entry.providers.length > 5) {
        throw new Error(`Route ${taskClass} needs 1-5 providers.`)
      }
      route.providers = entry.providers.map((item) => {
        if (typeof item !== 'string') throw new Error('Provider entries are strings.')
        const [provider, model] = item.split(':')
        if (!(TEXT_PROVIDER_IDS as readonly string[]).includes(provider) || provider === 'scripted') {
          throw new Error(`Unknown provider ${provider}.`)
        }
        if (model !== undefined && !MODEL_NAME.test(model)) throw new Error(`Invalid model name for ${provider}.`)
        return model ? { provider: provider as TextProviderId, model } : { provider: provider as TextProviderId }
      })
    }
    for (const [key, minimum, maximum] of [['maxInputTokens', 200, 200_000], ['maxOutputTokens', 16, 8_192], ['timeoutMs', 1_000, 120_000]] as const) {
      if (entry[key] === undefined) continue
      const number = entry[key]
      if (typeof number !== 'number' || !Number.isSafeInteger(number) || number < minimum || number > maximum) {
        throw new Error(`${taskClass}.${key} is out of range.`)
      }
      route[key] = number
    }
  }
  return table
}

export type ProviderResolver = (provider: TextProviderId, model?: string) => ModelProvider | undefined

export interface RouteAttempt {
  provider: TextProviderId
  model: string
  outcome: 'ok' | ModelFailureKind | 'invalid_output' | 'skipped_cooldown' | 'skipped_capability' | 'skipped_unconfigured' | 'skipped_not_permitted'
  latencyMs?: number
}

export class ModelRoutingError extends Error {
  constructor(readonly taskClass: ModelTaskClass, readonly attempts: RouteAttempt[]) {
    super(`No model could complete ${taskClass}.`)
    this.name = 'ModelRoutingError'
  }
}

export interface RoutedResult<T> {
  value: T
  provider: TextProviderId
  model: string
  attempts: RouteAttempt[]
  context: BuiltContext
}

export interface RouteRequest<T> {
  taskClass: ModelTaskClass
  context: ContextInput
  responseFormat: ModelRequest['responseFormat']
  jsonSchema?: Record<string, unknown>
  image?: ModelRequest['image']
  /** Turns raw model text into a closed value, or throws. */
  validate: (text: string) => T
  taskId?: string
  /**
   * A data-routing rule: providers this request's content may be sent to.
   * A provider it refuses is skipped before anything is sent, never tried.
   */
  permits?: (provider: ModelProvider) => boolean
}

const COOLDOWN_MS: Partial<Record<ModelFailureKind, number>> = {
  unavailable: 30_000,
  rate_limited: 60_000,
  not_configured: 5 * 60_000
}

export class ModelRouter {
  private readonly cooldowns = new Map<string, number>()

  constructor(
    private readonly resolve: ProviderResolver,
    private readonly routes: RoutingTable = DEFAULT_ROUTES,
    private readonly diagnostics: DiagnosticsSink = NO_DIAGNOSTICS,
    private readonly now: () => number = Date.now
  ) {}

  route(taskClass: ModelTaskClass): RouteConfig {
    return this.routes[taskClass]
  }

  /**
   * Is this provider inside a failure cooldown right now? Checked (by callers that spend a one-shot
   * approval) BEFORE the approval is spent, so a provider the router would skip cannot burn it.
   */
  isCoolingDown(provider: ModelProvider): boolean {
    return (this.cooldowns.get(`${provider.id}:${provider.model}`) ?? 0) > this.now()
  }

  /** Configured providers for a class, in route order. Never contacts one. */
  providersFor(taskClass: ModelTaskClass): ModelProvider[] {
    const found: ModelProvider[] = []
    for (const entry of this.routes[taskClass].providers) {
      const provider = this.resolve(entry.provider, entry.model)
      if (provider && provider.configured() && !found.includes(provider)) found.push(provider)
    }
    return found
  }

  async run<T>(request: RouteRequest<T>): Promise<RoutedResult<T>> {
    const config = this.routes[request.taskClass]
    const isPrivate = PRIVATE_TASK_CLASSES.includes(request.taskClass)
    // Checked before a context is built and before any provider is resolved, so
    // a caller that forgot the recipient rule sends nothing to anybody.
    if (isPrivate && request.permits === undefined) throw new PrivateRouteError(request.taskClass, 'recipient_required')
    const allowsImage = PRIVATE_VISION_TASK_CLASSES.includes(request.taskClass)
    if (isPrivate && (request.image !== undefined || config.vision) && !allowsImage) {
      throw new PrivateRouteError(request.taskClass, 'image_forbidden')
    }
    const context = buildContext(request.context, { maxInputTokens: config.maxInputTokens })
    const attempts: RouteAttempt[] = []
    let attemptNumber = 0
    for (const entry of config.providers) {
      const provider = this.resolve(entry.provider, entry.model)
      const model = provider?.model ?? entry.model ?? 'default'
      const key = `${entry.provider}:${model}`
      const skip = (outcome: RouteAttempt['outcome']): void => { attempts.push({ provider: entry.provider, model, outcome }) }
      if (!provider || !provider.configured()) { skip('skipped_unconfigured'); continue }
      if (request.permits && !request.permits(provider)) { skip('skipped_not_permitted'); continue }
      if ((config.vision || request.image) && !provider.capabilities.vision) { skip('skipped_capability'); continue }
      if (request.responseFormat === 'json' && !provider.capabilities.json) { skip('skipped_capability'); continue }
      if ((this.cooldowns.get(key) ?? 0) > this.now()) { skip('skipped_cooldown'); continue }

      attemptNumber += 1
      const started = this.now()
      let outcome: RouteAttempt['outcome']
      try {
        const response = await provider.generate({
          taskClass: request.taskClass,
          system: context.system,
          input: context.input,
          responseFormat: request.responseFormat,
          maxOutputTokens: config.maxOutputTokens,
          signal: AbortSignal.timeout(config.timeoutMs),
          ...(request.jsonSchema ? { jsonSchema: request.jsonSchema } : {}),
          ...(request.image ? { image: request.image } : {})
        })
        const latencyMs = Math.max(0, this.now() - started)
        let value: T
        try {
          value = request.validate(response.text)
        } catch {
          attempts.push({ provider: entry.provider, model, outcome: 'invalid_output', latencyMs })
          this.record(request, entry.provider, model, 'invalid_output', latencyMs, attemptNumber, context, response.usage)
          if (isPrivate) break
          continue
        }
        attempts.push({ provider: entry.provider, model, outcome: 'ok', latencyMs })
        this.record(request, entry.provider, model, 'ok', latencyMs, attemptNumber, context, response.usage)
        return { value, provider: entry.provider, model, attempts, context }
      } catch (error) {
        outcome = error instanceof ModelProviderError ? error.kind : 'unavailable'
        const latencyMs = Math.max(0, this.now() - started)
        attempts.push({ provider: entry.provider, model, outcome, latencyMs })
        this.record(request, entry.provider, model, outcome, latencyMs, attemptNumber, context)
        const cooldown = COOLDOWN_MS[outcome as ModelFailureKind]
        if (cooldown) this.cooldowns.set(key, this.now() + cooldown)
        if (error instanceof ModelProviderError && !error.failover) break
      }
      // One attempt for private content, whatever happened: no second provider.
      if (isPrivate) break
    }
    throw new ModelRoutingError(request.taskClass, attempts)
  }

  private record(
    request: RouteRequest<unknown>, provider: TextProviderId, model: string, result: string,
    latencyMs: number, attempt: number, context: BuiltContext, usage?: { inputTokens?: number; outputTokens?: number }
  ): void {
    this.diagnostics.record({
      kind: 'model_call',
      provider,
      model,
      taskClass: request.taskClass,
      latencyMs,
      attempt,
      contextTokens: context.approxTokens,
      ...(usage?.inputTokens !== undefined ? { inputTokens: usage.inputTokens } : {}),
      ...(usage?.outputTokens !== undefined ? { outputTokens: usage.outputTokens } : {}),
      ...(request.taskId ? { taskId: request.taskId } : {}),
      result
    })
  }
}
