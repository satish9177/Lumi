import {
  DESKTOP_ACTION_OPERATIONS,
  DESKTOP_ACTION_STATUSES,
  DESKTOP_INVOKE_EFFECTS,
  DESKTOP_SCROLL_STEPS,
  type AgentDesktopActionResultView,
  type AgentDesktopActionView,
  type AgentDesktopScrollTargetList,
  type AgentError,
  type AgentRegisteredApp
} from '../../shared/agent-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 9 S3 DTOs.
 *
 * Same discipline as `agent-wire.ts`: a violation rejects the whole response and only known, bounded
 * fields are ever copied. Deliberately never read, even if the runtime sent it: a window handle, process id
 * or path, a program path or argument, an AutomationId, class name, RuntimeId, screen position, snapshot or
 * digest. The result is a whitelist, so a field the runtime adds later cannot reach the renderer unreviewed.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CONTROL_REF = /^u(?:[1-9]\d?|1\d\d|200)$/
const APP_ID = /^[a-z][a-z0-9_-]{0,31}$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const ROLE = /^[a-z_]{1,32}$/
const OUTCOMES = ['SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function bounded(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) throw new WireError(what)
  return value
}

function optional<T>(value: unknown, read: (value: unknown) => T): T | undefined {
  return value === null || value === undefined ? undefined : read(value)
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function integer(value: unknown, what: string, minimum: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) throw new WireError(what)
  return value
}

function percent(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) throw new WireError(what)
  return value
}

function parseResult(value: unknown): AgentDesktopActionResultView {
  const body = record(value, 'desktop.action.result')
  const result: AgentDesktopActionResultView = {}
  const outcome = optional(body.outcome, (item) => {
    if (typeof item !== 'string' || !CODE.test(item)) throw new WireError('desktop.action.result.outcome')
    return item
  })
  if (outcome !== undefined) result.outcome = outcome
  const before = optional(body.percent_before, (item) => percent(item, 'desktop.action.result.before'))
  if (before !== undefined) result.percentBefore = before
  const after = optional(body.percent_after, (item) => percent(item, 'desktop.action.result.after'))
  if (after !== undefined) result.percentAfter = after
  for (const [wire, key] of [
    ['human_input_during', 'humanInputDuring'], ['observation_invalidated', 'observationInvalidated'], ['focused', 'focused']
  ] as const) {
    const flag = body[wire]
    if (flag !== null && flag !== undefined) {
      if (typeof flag !== 'boolean') throw new WireError(`desktop.action.result.${wire}`)
      result[key] = flag
    }
  }
  const follow = optional(body.follow_up_observation_id, (item) => uuid(item, 'desktop.action.result.follow_up'))
  if (follow !== undefined) result.followUpObservationId = follow
  return result
}

export function parseDesktopAction(value: unknown): AgentDesktopActionView {
  const body = record(value, 'desktop.action')
  const view: AgentDesktopActionView = {
    actionId: uuid(body.action_id, 'desktop.action.id'),
    taskId: uuid(body.task_id, 'desktop.action.task_id'),
    revision: integer(body.revision, 'desktop.action.revision', 1),
    status: member(DESKTOP_ACTION_STATUSES, body.status, 'desktop.action.status'),
    operation: member(DESKTOP_ACTION_OPERATIONS, body.operation, 'desktop.action.operation'),
    applicationLabel: bounded(body.application_label, 'desktop.action.label', 64)
  }
  const title = optional(body.window_title, (item) => bounded(item, 'desktop.action.title', 120))
  if (title !== undefined) view.windowTitle = title
  const role = optional(body.control_role, (item) => {
    if (typeof item !== 'string' || !ROLE.test(item)) throw new WireError('desktop.action.role')
    return item
  })
  if (role !== undefined) view.controlRole = role
  const name = optional(body.control_name, (item) => bounded(item, 'desktop.action.control_name', 120))
  if (name !== undefined) view.controlName = name
  const step = optional(body.step, (item) => member(DESKTOP_SCROLL_STEPS, item, 'desktop.action.step'))
  if (step !== undefined) view.step = step
  const appId = optional(body.app_id, (item) => {
    if (typeof item !== 'string' || !APP_ID.test(item)) throw new WireError('desktop.action.app_id')
    return item
  })
  if (appId !== undefined) view.appId = appId
  const trustedValue = optional(body.value, (item) => bounded(item, 'desktop.action.value', 4_000))
  if (trustedValue !== undefined) view.value = trustedValue
  const containerRole = optional(body.container_role, (item) => {
    if (typeof item !== 'string' || !ROLE.test(item)) throw new WireError('desktop.action.container_role')
    return item
  })
  if (containerRole !== undefined) view.containerRole = containerRole
  const containerName = optional(body.container_name, (item) => bounded(item, 'desktop.action.container_name', 120))
  if (containerName !== undefined) view.containerName = containerName
  const optionRole = optional(body.option_role, (item) => {
    if (typeof item !== 'string' || !ROLE.test(item)) throw new WireError('desktop.action.option_role')
    return item
  })
  if (optionRole !== undefined) view.optionRole = optionRole
  const optionName = optional(body.option_name, (item) => bounded(item, 'desktop.action.option_name', 120))
  if (optionName !== undefined) view.optionName = optionName
  const effect = optional(body.effect, (item) => member(DESKTOP_INVOKE_EFFECTS, item, 'desktop.action.effect'))
  if (effect !== undefined) view.effect = effect
  const expires = optional(body.expires_at, (item) => {
    if (typeof item !== 'string' || !INSTANT.test(item) || Number.isNaN(Date.parse(item))) throw new WireError('desktop.action.expires')
    return item
  })
  if (expires !== undefined) view.expiresAt = expires
  const outcome = optional(body.attempt_outcome, (item) => member(OUTCOMES, item, 'desktop.action.outcome'))
  if (outcome !== undefined) view.attemptOutcome = outcome
  const error = optional(body.error_code, (item) => {
    if (typeof item !== 'string' || !CODE.test(item)) throw new WireError('desktop.action.error')
    return item
  })
  if (error !== undefined) view.errorCode = error
  const result = optional(body.result, parseResult)
  if (result !== undefined) view.result = result
  return view
}

export function parseLatestDesktopAction(value: unknown): AgentDesktopActionView | null {
  const body = record(value, 'desktop.action.latest')
  return body.action === null || body.action === undefined ? null : parseDesktopAction(body.action)
}

export function parseRegisteredApps(value: unknown): AgentRegisteredApp[] {
  const body = record(value, 'desktop.apps')
  if (!Array.isArray(body.apps) || body.apps.length > 16) throw new WireError('desktop.apps.list')
  return body.apps.map((item) => {
    const app = record(item, 'desktop.apps.item')
    if (typeof app.app_id !== 'string' || !APP_ID.test(app.app_id)) throw new WireError('desktop.apps.id')
    return { appId: app.app_id, label: bounded(app.label, 'desktop.apps.label', 64) }
  })
}

export function parseScrollTargets(value: unknown): AgentDesktopScrollTargetList {
  const body = record(value, 'desktop.scroll_targets')
  if (!Array.isArray(body.targets) || body.targets.length > 20) throw new WireError('desktop.scroll_targets.list')
  return {
    observationId: uuid(body.observation_id, 'desktop.scroll_targets.observation'),
    targets: body.targets.map((item) => {
      const target = record(item, 'desktop.scroll_targets.item')
      if (typeof target.control_ref !== 'string' || !CONTROL_REF.test(target.control_ref)) throw new WireError('desktop.scroll_targets.ref')
      if (typeof target.role !== 'string' || !ROLE.test(target.role)) throw new WireError('desktop.scroll_targets.role')
      return { controlRef: target.control_ref, role: target.role, name: bounded(target.name, 'desktop.scroll_targets.name', 120) }
    })
  }
}

/**
 * What the person is told when an action is refused. Every message says truthfully whether anything changed:
 * a refusal raised before an effect changed nothing; `desktop_effect_uncertain` and a lost answer do not say
 * so, and are never retried.
 */
export function describeDesktopActionRefusal(reason: string | undefined): string {
  switch (reason) {
    case 'desktop_action_open':
      return 'Another desktop action is still waiting or running. Finish or cancel it first. Nothing was changed.'
    case 'desktop_action_stale':
    case 'desktop_action_observation_stale':
    case 'stale_surface':
    case 'stale_worker_generation':
    case 'surface_unavailable':
    case 'surface_changed':
    case 'stale_control':
    case 'element_missing':
    case 'element_ambiguous':
    case 'element_changed':
      return 'That window or control changed since you chose it. Choose it again. Nothing was changed.'
    case 'desktop_action_not_approvable':
    case 'desktop_action_not_declinable':
      return 'That approval is no longer available. Nothing was changed.'
    case 'human_input_detected':
      return 'You used the keyboard or mouse, so Lumi stopped. Nothing was changed.'
    case 'elevated_window_refused':
    case 'integrity_unverifiable':
      return 'Lumi does not act on windows running with higher privileges. Nothing was changed.'
    case 'credential_surface':
      return 'That window has a password or sign-in field, so Lumi will not act on it. Nothing was changed.'
    case 'surface_not_focusable':
      return 'That window is minimized or hidden. Lumi does not restore windows. Nothing was changed.'
    case 'not_scrollable':
      return 'That control cannot be scrolled. Nothing was changed.'
    case 'app_not_registered':
      return 'That application is not on Lumi’s list of applications it may open. Nothing was started.'
    case 'launch_refused':
      return 'Windows did not start that application. Nothing was left running.'
    case 'desktop_automation_disabled':
    case 'desktop_automation_unsupported':
    case 'desktop_worker_unavailable':
      return 'Lumi’s desktop helper is not available. Nothing was changed.'
    case 'not_a_value_control':
    case 'read_only_control':
      return 'That control cannot be set. Nothing was changed.'
    case 'sensitive_target_refused':
      return 'Lumi will not write into a terminal, security window or file picker. Nothing was changed.'
    case 'not_selectable':
    case 'option_wrong_container':
      return 'That option could not be selected. Nothing was changed.'
    case 'not_invokable':
    case 'unsupported_or_unknown_effect':
      return 'Lumi does not yet support that control. Nothing was changed.'
    case 'desktop_action_unresolved':
      return 'Lumi does not know whether a previous step happened. Report what you saw before doing anything else.'
    case 'desktop_action_not_reconcilable':
      return 'That action is not waiting on a report. Nothing was changed.'
    case 'desktop_plan_not_ready':
    case 'desktop_plan_already_opened':
      return 'That plan is not ready to review. Nothing was changed.'
    case 'desktop_action_invalid':
      return 'That plan no longer matches the window. Inspect it again. Nothing was changed.'
    default:
      return 'Lumi could not do that. Nothing was changed.'
  }
}

export function projectDesktopActionError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const code = typeof body?.code === 'string' ? body.code : ''
  const reason = typeof body?.reason === 'string' && CODE.test(body.reason) ? body.reason : undefined
  if (code === 'desktop_refused' || code === 'desktop_action_refused') {
    return { code: 'desktop_refused', message: describeDesktopActionRefusal(reason) }
  }
  if (code === 'invalid_request') return { code: 'invalid_request', message: 'Lumi refused that desktop request. Nothing was changed.' }
  if (code === 'effect_locked') {
    // Milestone 10 S5: an earlier consequential action (on any executor) is in flight or unresolved.
    return { code: 'effect_locked', message: 'An earlier action may already have happened and is not checked yet. Check it first; nothing was changed.' }
  }
  if (code === 'action_not_found' || code === 'task_not_found') return { code: 'not_found', message: 'That desktop action no longer exists.' }
  if (code === 'approval_not_usable' || code === 'action_revision_conflict' || code === 'action_transition_invalid') {
    return { code: 'desktop_read_stale', message: 'That approval is no longer available. Nothing was changed.' }
  }
  return {
    code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed',
    message: 'The agent runtime could not complete that request.'
  }
}
