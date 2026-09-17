import type { TextProviderId } from '../../shared/model-contracts'
import type { DiagnosticsSink } from '../agent/diagnostics'
import { ApplicationDefaultCredentials, type GoogleTokenSource } from './google-auth'
import { ModelRouter, parseRoutingOverrides, type ProviderResolver } from './model-router'
import type { ModelProvider } from './provider'
import { parseScriptedModels } from './scripted-provider'
import { DeepSeekProvider, GeminiVertexProvider, OpenAITextProvider } from './text-providers'

/**
 * Builds the model router from Electron main's own configuration.
 *
 * | Variable | Meaning |
 * | --- | --- |
 * | `OPENAI_API_KEY` | OpenAI text + realtime |
 * | `LUMI_OPENAI_TEXT_MODEL` | OpenAI text model (default `LUMI_REASONING_MODEL` or gpt-5.6-terra) |
 * | `DEEPSEEK_API_KEY` | DeepSeek text |
 * | `LUMI_DEEPSEEK_MODEL` | default `deepseek-chat` |
 * | `LUMI_VERTEX_ENABLED=1` | use Google Application Default Credentials for Vertex AI |
 * | `LUMI_VERTEX_PROJECT` / `GOOGLE_CLOUD_PROJECT` | project (else the ADC quota project) |
 * | `LUMI_VERTEX_LOCATION` | default `us-central1` |
 * | `LUMI_GEMINI_TEXT_MODEL` | default `gemini-2.5-flash` |
 * | `LUMI_MODEL_ROUTES` | JSON routing override, see model-router.ts |
 * | `LUMI_SCRIPTED_MODELS` | unpackaged builds only: deterministic stand-ins |
 *
 * Keys are read lazily from the environment each call and are never copied
 * into the renderer, the Python runtime or the browser worker.
 */

export interface ModelEnvironment {
  environment?: NodeJS.ProcessEnv
  allowScripted: boolean
  diagnostics?: DiagnosticsSink
  googleTokens?: GoogleTokenSource
  fetch?: typeof globalThis.fetch
}

export function vertexEnabled(environment: NodeJS.ProcessEnv): boolean {
  return environment.LUMI_VERTEX_ENABLED === '1'
}

export function vertexLocation(environment: NodeJS.ProcessEnv): string {
  const location = environment.LUMI_VERTEX_LOCATION?.trim() || 'us-central1'
  return /^[a-z]+(?:-[a-z]+\d*)*$/.test(location) ? location : 'us-central1'
}

export function createModelRouter(options: ModelEnvironment): { router: ModelRouter; scripted: boolean } {
  const environment = options.environment ?? process.env
  const fetchImpl = options.fetch ?? globalThis.fetch
  const scriptedSpec = options.allowScripted ? environment.LUMI_SCRIPTED_MODELS?.trim() : undefined
  const routes = parseRoutingOverrides(environment.LUMI_MODEL_ROUTES)

  if (scriptedSpec) {
    // Scripted stand-ins replace the whole table for intent extraction so a
    // test controls exactly which "provider" fails and which answers.
    const providers = parseScriptedModels(scriptedSpec)
    const byId = new Map<TextProviderId, ModelProvider>(providers.map((provider) => [provider.id, provider]))
    routes.intent_extraction = { ...routes.intent_extraction, providers: providers.map((provider) => ({ provider: provider.id })) }
    routes.page_answer = { ...routes.page_answer, providers: providers.map((provider) => ({ provider: provider.id })) }
    const resolve: ProviderResolver = (id) => byId.get(id)
    return { router: new ModelRouter(resolve, routes, options.diagnostics), scripted: true }
  }

  const google = vertexEnabled(environment)
    ? options.googleTokens ?? new ApplicationDefaultCredentials({ environment, fetch: fetchImpl })
    : undefined
  const cache = new Map<string, ModelProvider>()
  const resolve: ProviderResolver = (id, model) => {
    const key = `${id}:${model ?? ''}`
    const cached = cache.get(key)
    if (cached) return cached
    let provider: ModelProvider | undefined
    switch (id) {
      case 'openai':
        provider = new OpenAITextProvider(
          model ?? (environment.LUMI_OPENAI_TEXT_MODEL?.trim() || environment.LUMI_REASONING_MODEL?.trim() || 'gpt-5.6-terra'),
          () => environment.OPENAI_API_KEY?.trim() || undefined,
          fetchImpl
        )
        break
      case 'deepseek':
        provider = new DeepSeekProvider(
          model ?? (environment.LUMI_DEEPSEEK_MODEL?.trim() || 'deepseek-chat'),
          () => environment.DEEPSEEK_API_KEY?.trim() || undefined,
          fetchImpl
        )
        break
      case 'gemini':
        provider = new GeminiVertexProvider(
          model ?? (environment.LUMI_GEMINI_TEXT_MODEL?.trim() || 'gemini-2.5-flash'),
          google,
          vertexLocation(environment),
          fetchImpl
        )
        break
      case 'scripted':
        provider = undefined
    }
    if (provider) cache.set(key, provider)
    return provider
  }
  return { router: new ModelRouter(resolve, routes, options.diagnostics), scripted: false }
}
