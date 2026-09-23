import type { AgentError, AgentResult } from '../../shared/agent-contracts'
import type {
  AgentProjectApi,
  AgentProjectRecipeInput,
  AgentProjectRecipeView,
  AgentProjectRunCardView,
  AgentProjectRunView,
  AgentProjectScriptView,
  AgentProjectView
} from '../../shared/project-contracts'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import {
  parseLatestRun,
  parseProject,
  parseProjects,
  parseRecipe,
  parseRecipes,
  parseRun,
  parseScripts,
  projectProjectRuntimeError
} from './project-wire'

/**
 * The trusted domain client for Milestone 10 S3: registered projects, recipes and supervised runs.
 *
 * Its own class (not reachable from voice). Every consequential step is confirmed by a NATIVE dialog that
 * main builds from what the RUNTIME holds, never from what the renderer says:
 *
 * * a project folder comes from a native folder dialog, then a native confirmation carrying the warning
 *   "This recipe executes code from this project with your user-level permissions.";
 * * a recipe is registered only after a native confirmation that shows the exact script text package.json
 *   declares for the chosen name (read back from the runtime);
 * * a run is approved only after a native confirmation of the runtime's card (script, argv shape, env
 *   names, readiness, timeout) and the warning again.
 *
 * The renderer never supplies a path, a command, an executable or arguments. It picks ids and fills a
 * closed recipe form (a declared script name, readiness, timeout and env name/value pairs, which the
 * runtime screens for secrets).
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const SCRIPT = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$/
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const READY_PATH = /^\/[A-Za-z0-9._~/-]{0,127}$/
const TIMEOUTS = { read: 10_000, write: 30_000, start: 60_000 } as const

type ProjectFailureCode = 'invalid_request' | 'not_found' | 'project_state_changed' | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: ProjectFailureCode, message: string, currentRevision?: number): never {
  throw new AgentRequestError({ code, message, ...(currentRevision !== undefined ? { currentRevision } : {}) })
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

function id(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', `That ${what} reference is invalid.`)
  return value
}

function revision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) fail('invalid_request', 'That revision is invalid.')
  return value
}

function line(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string') fail('invalid_request', `Type ${what} first.`)
  const text = value.trim()
  if (!text || text.length > maximum || CONTROL_CHARS.test(text)) fail('invalid_request', `Type ${what} of up to ${maximum} characters.`)
  return text
}

/** The closed recipe form. Anything outside it is refused here, before the runtime sees it. */
export function recipeInput(value: unknown): AgentProjectRecipeInput {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail('invalid_request', 'That recipe is invalid.')
  const raw = value as Record<string, unknown>
  const allowed = new Set(['label', 'script', 'readinessKind', 'readyPort', 'readyPath', 'timeoutSeconds', 'env'])
  if (Object.keys(raw).some((key) => !allowed.has(key))) fail('invalid_request', 'That recipe is invalid.')
  const label = line(raw.label, 'a name for the recipe', 64)
  if (typeof raw.script !== 'string' || !SCRIPT.test(raw.script)) fail('invalid_request', 'Choose one of the project’s scripts.')
  const readinessKind = raw.readinessKind
  if (readinessKind !== 'http' && readinessKind !== 'exit_code') fail('invalid_request', 'Choose how Lumi knows the run is ready.')
  const timeout = raw.timeoutSeconds
  if (typeof timeout !== 'number' || !Number.isSafeInteger(timeout) || timeout < 5 || timeout > 600) fail('invalid_request', 'Choose a timeout of 5 to 600 seconds.')
  let readyPort: number | undefined
  let readyPath: string | undefined
  if (readinessKind === 'http') {
    if (typeof raw.readyPort !== 'number' || !Number.isSafeInteger(raw.readyPort) || raw.readyPort < 1024 || raw.readyPort > 65535) {
      fail('invalid_request', 'Choose a port from 1024 to 65535.')
    }
    readyPort = raw.readyPort
    const path = raw.readyPath === undefined ? '/' : raw.readyPath
    if (typeof path !== 'string' || !READY_PATH.test(path) || path.includes('..')) fail('invalid_request', 'Type a plain path such as /health.')
    readyPath = path
  } else if (raw.readyPort !== undefined || raw.readyPath !== undefined) {
    fail('invalid_request', 'An exit-code recipe has no port.')
  }
  const envValue = raw.env ?? {}
  if (typeof envValue !== 'object' || envValue === null || Array.isArray(envValue)) fail('invalid_request', 'Those variables are invalid.')
  const entries = Object.entries(envValue as Record<string, unknown>)
  if (entries.length > 16) fail('invalid_request', 'Use at most 16 variables.')
  const env: Record<string, string> = {}
  for (const [name, text] of entries) {
    if (!ENV_NAME.test(name) || typeof text !== 'string' || text.length > 256 || CONTROL_CHARS.test(text)) fail('invalid_request', 'Those variables are invalid.')
    env[name] = text
  }
  return { label, script: raw.script, readinessKind, ...(readyPort !== undefined ? { readyPort } : {}), ...(readyPath !== undefined ? { readyPath } : {}), timeoutSeconds: timeout, env }
}

export interface RecipeConfirmation {
  projectLabel: string
  script: string
  scriptText: string
  preText?: string
  postText?: string
  envNames: string[]
  readiness: string
  timeoutSeconds: number
}

export interface ProjectControllerDependencies {
  runtime: DesktopRuntimeRequester
  /** A native folder dialog, then a native confirmation with the execution warning. Undefined if cancelled. */
  chooseProject: (label: string) => Promise<string | undefined>
  /** A native confirmation showing the exact script text. True only if the person chose to register it. */
  confirmRecipe: (recipe: RecipeConfirmation) => Promise<boolean>
  /** A native confirmation of the runtime's run card, with the warning. True only on Allow. */
  confirmRun: (card: AgentProjectRunCardView) => Promise<boolean>
}

export class ProjectController implements AgentProjectApi {
  private readonly inFlight = new Set<string>()

  constructor(private readonly dependencies: ProjectControllerDependencies) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.dependencies.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was started.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectProjectRuntimeError(reply.status, reply.body))
    return reply
  }

  private async guarded<T>(key: string, parse: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      parse()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    if (this.inFlight.has(key)) return { ok: false, error: { code: 'busy', message: 'Lumi is already working on that.' } }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  // ---- projects ------------------------------------------------------------------------------------

  listProjects(): Promise<AgentResult<AgentProjectView[]>> {
    return this.guarded('projects:list', () => undefined, async () => parseProjects((await this.call('GET', '/projects', undefined, TIMEOUTS.read)).body))
  }

  addProject(labelValue: unknown): Promise<AgentResult<AgentProjectView | null>> {
    let label = ''
    return this.guarded('projects:add', () => {
      label = line(labelValue, 'a name for the project', 64)
    }, async () => {
      const path = await this.dependencies.chooseProject(label)
      if (!path) return null
      return parseProject((await this.call('POST', '/projects', { path, label }, TIMEOUTS.write)).body)
    })
  }

  revokeProject(projectIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentProjectView>> {
    let projectId = ''
    let expected = 0
    return this.guarded('projects:revoke', () => {
      projectId = id(projectIdValue, 'project')
      expected = revision(revisionValue)
    }, async () => parseProject((await this.call('POST', `/projects/${projectId}/revoke`, { expected_revision: expected }, TIMEOUTS.write)).body))
  }

  listProjectScripts(projectIdValue: unknown): Promise<AgentResult<AgentProjectScriptView[]>> {
    let projectId = ''
    return this.guarded('projects:scripts', () => {
      projectId = id(projectIdValue, 'project')
    }, async () => parseScripts((await this.call('GET', `/projects/${projectId}/scripts`, undefined, TIMEOUTS.read)).body))
  }

  // ---- recipes -------------------------------------------------------------------------------------

  createProjectRecipe(projectIdValue: unknown, recipeValue: unknown): Promise<AgentResult<AgentProjectRecipeView | null>> {
    let projectId = ''
    let recipe: AgentProjectRecipeInput | undefined
    return this.guarded('recipes:create', () => {
      projectId = id(projectIdValue, 'project')
      recipe = recipeInput(recipeValue)
    }, async () => {
      if (!recipe) fail('invalid_request', 'That recipe is invalid.')
      // The script text shown natively is what the RUNTIME reads from package.json now, not the renderer's.
      const projects = parseProjects((await this.call('GET', '/projects', undefined, TIMEOUTS.read)).body)
      const project = projects.find((item) => item.projectId === projectId)
      if (!project) fail('not_found', 'That project is not registered.')
      const scripts = parseScripts((await this.call('GET', `/projects/${projectId}/scripts`, undefined, TIMEOUTS.read)).body)
      const script = scripts.find((item) => item.name === recipe?.script)
      if (!script) fail('invalid_request', 'That script is not in the project’s package.json.')
      const find = (name: string): string | undefined => scripts.find((item) => item.name === name)?.text
      const pre = find(`pre${script.name}`)
      const post = find(`post${script.name}`)
      const confirmed = await this.dependencies.confirmRecipe({
        projectLabel: project.label,
        script: script.name,
        scriptText: script.text,
        ...(pre !== undefined ? { preText: pre } : {}),
        ...(post !== undefined ? { postText: post } : {}),
        envNames: Object.keys(recipe.env).sort(),
        readiness: recipe.readinessKind === 'http' ? `http://127.0.0.1:${recipe.readyPort ?? 0}${recipe.readyPath ?? '/'}` : 'exit code 0',
        timeoutSeconds: recipe.timeoutSeconds
      })
      if (!confirmed) return null
      const reply = await this.call('POST', `/projects/${projectId}/recipes`, {
        label: recipe.label, script: recipe.script, readiness_kind: recipe.readinessKind,
        ready_port: recipe.readyPort ?? null, ready_path: recipe.readyPath ?? null, timeout_seconds: recipe.timeoutSeconds, env: recipe.env,
        // Exactly what the native confirmation showed: the runtime refuses if package.json moved meanwhile.
        confirmed_script_text: script.text, confirmed_pre_text: pre ?? null, confirmed_post_text: post ?? null
      }, TIMEOUTS.write)
      return parseRecipe(reply.body)
    })
  }

  listProjectRecipes(): Promise<AgentResult<AgentProjectRecipeView[]>> {
    return this.guarded('recipes:list', () => undefined, async () => parseRecipes((await this.call('GET', '/project-recipes', undefined, TIMEOUTS.read)).body))
  }

  revokeProjectRecipe(recipeIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentProjectRecipeView>> {
    let recipeId = ''
    let expected = 0
    return this.guarded('recipes:revoke', () => {
      recipeId = id(recipeIdValue, 'recipe')
      expected = revision(revisionValue)
    }, async () => parseRecipe((await this.call('POST', `/project-recipes/${recipeId}/revoke`, { expected_revision: expected }, TIMEOUTS.write)).body))
  }

  // ---- runs ----------------------------------------------------------------------------------------

  createProjectRun(recipeIdValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    let recipeId = ''
    return this.guarded('runs:create', () => {
      recipeId = id(recipeIdValue, 'recipe')
    }, async () => parseRun((await this.call('POST', '/project-runs', { recipe_id: recipeId }, TIMEOUTS.write)).body))
  }

  getProjectRun(taskIdValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    let taskId = ''
    return this.guarded(`runs:get:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'run')
    }, async () => this.current(taskId))
  }

  getLatestProjectRun(): Promise<AgentResult<AgentProjectRunView | null>> {
    return this.guarded('runs:latest', () => undefined, async () => parseLatestRun((await this.call('GET', '/project-runs/latest', undefined, TIMEOUTS.read)).body))
  }

  private async current(taskId: string): Promise<AgentProjectRunView> {
    const view = parseRun((await this.call('GET', `/project-runs/${taskId}`, undefined, TIMEOUTS.read)).body)
    if (view.taskId !== taskId) throw new WireError('run.id')
    return view
  }

  private async card(taskId: string, grantId: string, expected: number): Promise<AgentProjectRunCardView> {
    const current = await this.current(taskId)
    if (!current.card || current.card.grantId !== grantId) fail('not_found', 'That approval does not belong to this run.')
    if (current.card.grantRevision !== expected) {
      fail('project_state_changed', 'The approval changed since you reviewed it. Review the current card.', current.card.grantRevision)
    }
    return current.card
  }

  grantProjectRun(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentProjectRunView | null>> {
    let taskId = ''
    let grantId = ''
    let expected = 0
    return this.guarded(`runs:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'run')
      grantId = id(grantIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      const card = await this.card(taskId, grantId, expected)
      if (!(await this.dependencies.confirmRun(card))) return null
      return parseRun((await this.call('POST', `/project-runs/${taskId}/grant`, { grant_id: grantId, expected_revision: expected }, TIMEOUTS.write)).body)
    })
  }

  declineProjectRun(taskIdValue: unknown, grantIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    let taskId = ''
    let grantId = ''
    let expected = 0
    return this.guarded(`runs:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'run')
      grantId = id(grantIdValue, 'approval')
      expected = revision(revisionValue)
    }, async () => {
      await this.card(taskId, grantId, expected)
      return parseRun((await this.call('POST', `/project-runs/${taskId}/revoke`, { grant_id: grantId, expected_revision: expected }, TIMEOUTS.write)).body)
    })
  }

  startProjectRun(taskIdValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    return this.step(taskIdValue, 'start', TIMEOUTS.start)
  }

  stopProjectRun(taskIdValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    return this.step(taskIdValue, 'stop', TIMEOUTS.write)
  }

  reconcileProjectRun(taskIdValue: unknown): Promise<AgentResult<AgentProjectRunView>> {
    return this.step(taskIdValue, 'reconcile', TIMEOUTS.write)
  }

  private step(taskIdValue: unknown, step: 'start' | 'stop' | 'reconcile', timeoutMs: number): Promise<AgentResult<AgentProjectRunView>> {
    let taskId = ''
    return this.guarded(`runs:step:${String(taskIdValue)}`, () => {
      taskId = id(taskIdValue, 'run')
    }, async () => {
      const view = parseRun((await this.call('POST', `/project-runs/${taskId}/${step}`, {}, timeoutMs)).body)
      if (view.taskId !== taskId) throw new WireError('run.id')
      return view
    })
  }
}
