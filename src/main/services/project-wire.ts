import type { AgentError, AgentTaskStatus } from '../../shared/agent-contracts'
import {
  PROJECT_RUN_PHASES,
  RUN_WARNING,
  type AgentProjectRecipeView,
  type AgentProjectRunCardView,
  type AgentProjectRunView,
  type AgentProjectScriptView,
  type AgentProjectView
} from '../../shared/project-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 10 S3 DTOs. A violation rejects the whole
 * response. Labels, codes and names are refused if they look like an absolute Windows path. Script text and
 * log lines are display text (they may legitimately mention a path) and are bounded, never forwarded.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const STATUS = /^[A-Z][A-Z_]{0,31}$/
const SCRIPT = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$/
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const READY_PATH = /^\/[A-Za-z0-9._~/-]{0,127}$/
const ABSOLUTE_PATH = /(^|[\s"'(])([A-Za-z]:[\\/]|\\\\|\/\/\?\/)/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const RECIPE_STATUSES = ['ACTIVE', 'INVALIDATED', 'REVOKED'] as const
const READINESS = ['http', 'exit_code'] as const

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function integer(value: unknown, what: string, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum || value > maximum) throw new WireError(what)
  return value
}

function optional<T>(value: unknown, parse: (value: unknown) => T): T | undefined {
  return value === null || value === undefined ? undefined : parse(value)
}

function label(value: unknown, what: string, maximum = 64): string {
  if (typeof value !== 'string' || value.length > maximum || ABSOLUTE_PATH.test(value)) throw new WireError(what)
  return value
}

function display(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) throw new WireError(what)
  return value
}

function pattern(value: unknown, what: string, shape: RegExp): string {
  if (typeof value !== 'string' || !shape.test(value)) throw new WireError(what)
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function list(value: unknown, what: string, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new WireError(what)
  return value
}

function envNames(value: unknown, what: string): string[] {
  return list(value, what, 16).map((name) => pattern(name, what, ENV_NAME))
}

export function parseProject(value: unknown): AgentProjectView {
  const raw = record(value, 'project')
  return {
    projectId: uuid(raw.project_id, 'project.project_id'),
    label: label(raw.label, 'project.label'),
    revision: integer(raw.revision, 'project.revision', 1),
    createdAt: pattern(raw.created_at, 'project.created_at', INSTANT)
  }
}

export function parseProjects(value: unknown): AgentProjectView[] {
  return list(record(value, 'projects').projects, 'projects.projects', 100).map(parseProject)
}

export function parseScripts(value: unknown): AgentProjectScriptView[] {
  return list(record(value, 'scripts').scripts, 'scripts.scripts', 200).map((item) => {
    const raw = record(item, 'script')
    return { name: pattern(raw.name, 'script.name', SCRIPT), text: display(raw.text, 'script.text', 2000) }
  })
}

export function parseRecipe(value: unknown): AgentProjectRecipeView {
  const raw = record(value, 'recipe')
  const preText = optional(raw.pre_text, (v) => display(v, 'recipe.pre_text', 2000))
  const postText = optional(raw.post_text, (v) => display(v, 'recipe.post_text', 2000))
  const readyPort = optional(raw.ready_port, (v) => integer(v, 'recipe.ready_port', 1024, 65535))
  const readyPath = optional(raw.ready_path, (v) => pattern(v, 'recipe.ready_path', READY_PATH))
  const invalidReason = optional(raw.invalid_reason, (v) => pattern(v, 'recipe.invalid_reason', CODE))
  return {
    recipeId: uuid(raw.recipe_id, 'recipe.recipe_id'),
    projectId: uuid(raw.project_id, 'recipe.project_id'),
    label: label(raw.label, 'recipe.label'),
    script: pattern(raw.script, 'recipe.script', SCRIPT),
    scriptText: display(raw.script_text, 'recipe.script_text', 2000),
    ...(preText !== undefined ? { preText } : {}),
    ...(postText !== undefined ? { postText } : {}),
    envNames: envNames(raw.env_names, 'recipe.env_names'),
    readinessKind: member(READINESS, raw.readiness_kind, 'recipe.readiness_kind'),
    ...(readyPort !== undefined ? { readyPort } : {}),
    ...(readyPath !== undefined ? { readyPath } : {}),
    timeoutSeconds: integer(raw.timeout_seconds, 'recipe.timeout_seconds', 5, 600),
    status: member(RECIPE_STATUSES, raw.status, 'recipe.status'),
    ...(invalidReason ? { invalidReason } : {}),
    revision: integer(raw.revision, 'recipe.revision', 1)
  }
}

export function parseRecipes(value: unknown): AgentProjectRecipeView[] {
  return list(record(value, 'recipes').recipes, 'recipes.recipes', 200).map(parseRecipe)
}

function card(value: unknown): AgentProjectRunCardView {
  const raw = record(value, 'run.card')
  if (raw.warning !== RUN_WARNING) throw new WireError('run.card.warning')
  if (raw.stop_policy !== 'terminate_job') throw new WireError('run.card.stop_policy')
  const argv = list(raw.argv, 'run.card.argv', 4).map((item) => display(item, 'run.card.argv', 64))
  // The argument vector's shape is fixed: node.exe, npm-cli.js, "run", <the recipe's script>.
  if (argv.length !== 4 || argv[0] !== 'node.exe' || argv[1] !== 'npm-cli.js' || argv[2] !== 'run') throw new WireError('run.card.argv')
  const script = pattern(raw.script, 'run.card.script', SCRIPT)
  if (argv[3] !== script) throw new WireError('run.card.argv')
  const expiresAt = optional(raw.expires_at, (v) => pattern(v, 'run.card.expires_at', INSTANT))
  const preText = optional(raw.pre_text, (v) => display(v, 'run.card.pre_text', 2000))
  const postText = optional(raw.post_text, (v) => display(v, 'run.card.post_text', 2000))
  const readyPort = optional(raw.ready_port, (v) => integer(v, 'run.card.ready_port', 1024, 65535))
  const readyPath = optional(raw.ready_path, (v) => pattern(v, 'run.card.ready_path', READY_PATH))
  return {
    grantId: uuid(raw.grant_id, 'run.card.grant_id'),
    grantRevision: integer(raw.grant_revision, 'run.card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, raw.grant_status, 'run.card.grant_status'),
    ...(expiresAt ? { expiresAt } : {}),
    recipeId: uuid(raw.recipe_id, 'run.card.recipe_id'),
    recipeRevision: integer(raw.recipe_revision, 'run.card.recipe_revision', 1),
    projectLabel: label(raw.project_label, 'run.card.project_label'),
    label: label(raw.label, 'run.card.label'),
    script,
    scriptText: display(raw.script_text, 'run.card.script_text', 2000),
    ...(preText !== undefined ? { preText } : {}),
    ...(postText !== undefined ? { postText } : {}),
    argv,
    envNames: envNames(raw.env_names, 'run.card.env_names'),
    readinessKind: member(READINESS, raw.readiness_kind, 'run.card.readiness_kind'),
    ...(readyPort !== undefined ? { readyPort } : {}),
    ...(readyPath !== undefined ? { readyPath } : {}),
    timeoutSeconds: integer(raw.timeout_seconds, 'run.card.timeout_seconds', 5, 600),
    stopPolicy: 'terminate_job',
    warning: RUN_WARNING
  }
}

export function parseRun(value: unknown): AgentProjectRunView {
  const raw = record(value, 'run')
  const startStatus = optional(raw.start_status, (v) => pattern(v, 'run.start_status', STATUS))
  const errorCode = optional(raw.error_code, (v) => pattern(v, 'run.error_code', CODE))
  const exitCode = optional(raw.exit_code, (v) => integer(v, 'run.exit_code', -(2 ** 31), 2 ** 32))
  const parsedCard = optional(raw.card, card)
  return {
    taskId: uuid(raw.task_id, 'run.task_id'),
    taskStatus: member(TASK_STATUSES, raw.task_status, 'run.task_status'),
    taskRevision: integer(raw.task_revision, 'run.task_revision', 1),
    phase: member(PROJECT_RUN_PHASES, raw.phase, 'run.phase'),
    ...(startStatus ? { startStatus } : {}),
    ...(errorCode ? { errorCode } : {}),
    ...(exitCode !== undefined ? { exitCode } : {}),
    activeProcesses: integer(raw.active_processes, 'run.active_processes', 0, 10_000),
    ready: raw.ready === true,
    logTail: list(raw.log_tail, 'run.log_tail', 200).map((line) => display(line, 'run.log_tail', 400)),
    ...(parsedCard ? { card: parsedCard } : {})
  }
}

export function parseLatestRun(value: unknown): AgentProjectRunView | null {
  const raw = record(value, 'latest_run')
  return raw.run === null || raw.run === undefined ? null : parseRun(raw.run)
}

const REASONS: Record<string, string> = {
  missing_dependency: 'BLOCKED: a dependency this script needs is missing. Lumi never installs dependencies; install them yourself, then try again.',
  recipe_changed: 'The project changed since this recipe was registered (its package.json, lockfile, script or Node.js). Register the recipe again.',
  recipe_revoked: 'That recipe was removed.',
  run_already_active: 'This project is already running. Stop it first.',
  port_in_use: 'Another program is already using that port. Nothing was started.',
  script_not_declared: 'That script is not in the project’s package.json.',
  overlaps_download_folder: 'That folder is approved for saving downloads, so Lumi will never run code from it.',
  package_json_missing: 'That folder has no package.json, so it is not a Node.js project.',
  node_not_found: 'Lumi could not find Node.js under Program Files. A per-user Node.js install is not used.',
  project_already_registered: 'That project is already registered.',
  ancestor_npm_context: 'A folder above this project is itself an npm project (or has node_modules or .npmrc), which could change what npm runs, so Lumi will not run it.',
  hook_not_displayable: 'This script has a pre/post hook Lumi cannot show you, so Lumi will not run it.',
  script_changed_during_review: 'The project’s package.json changed while you were reviewing. Nothing was registered; review it again.',
  project_npmrc_refused: 'That project has its own .npmrc, which could change what npm runs, so Lumi will not run it.',
  grant_not_active: 'Allow the run on the card first.',
  grant_changed: 'That approval changed since you reviewed it.',
  effect_locked: 'An earlier action is still uncertain. Check it first; nothing was started.',
  wrong_phase: 'That step is not available now.',
  env_name_secret: 'That variable name looks like a secret. Lumi never passes secrets to a project run.',
  env_name_reserved: 'That variable is set by Lumi or could change which code runs.',
  env_value_secret: 'That value looks like a secret. Lumi never passes secrets to a project run.'
}

const DEFAULTS: Record<string, string> = {
  project_state_changed: 'That project run can no longer go ahead as approved. Nothing was started.',
  project_refused: 'Lumi refused that project request. Nothing was started.'
}

export function projectProjectRuntimeError(status: number, body: unknown): AgentError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined
  const code = typeof error?.code === 'string' && CODE.test(error.code) ? error.code : undefined
  const reason = typeof error?.reason === 'string' && CODE.test(error.reason) ? error.reason : undefined
  if (code === 'project_state_changed' || code === 'project_refused') {
    return { code, message: (reason !== undefined ? REASONS[reason] : undefined) ?? DEFAULTS[code] }
  }
  if (code === 'effect_locked') return { code, message: REASONS.effect_locked }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That run no longer exists.' }
  if (code === 'task_not_accepting_actions') return { code: 'not_accepting_actions', message: 'That task has ended.' }
  return { code: status === 422 ? 'invalid_request' : 'request_failed', message: 'Lumi could not complete that project request.' }
}
