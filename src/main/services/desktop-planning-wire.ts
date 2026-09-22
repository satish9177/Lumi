import {
  DESKTOP_PLAN_PHASES,
  DISCLOSURE_RECIPIENTS,
  type AgentDesktopPlanCardView,
  type AgentDesktopPlanStateView,
  type AgentDesktopPlanValueView,
  type AgentDesktopPlanView,
  type AgentDesktopValueDescriptor,
  type AgentError,
  type AgentProposedAction,
  type AgentTaskStatus
} from '../../shared/agent-contracts'
import type { DesktopProjection, DesktopProjectionNode } from '../agent/desktop-reader'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 9 S4 planning DTOs. Same discipline as
 * `desktop-read-wire.ts`: a violation rejects the whole response rather than guessing, and only
 * known, bounded fields are ever copied. Never read: a window handle, process id or path, an
 * AutomationId, class name, framework id, RuntimeId, screen position, pattern list, snapshot, snapshot
 * digest, another observation, or a candidate value's raw text inside the released provider context.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const CONTROL_REF = /^u(?:[1-9]\d?|1\d\d|200)$/
const VALUE_REF = /^v([1-9]|10)$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const PLAN_STATUSES = ['STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const
const ROLE = /^[a-z_]{1,32}$/

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function integer(value: unknown, what: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) throw new WireError(what)
  return value
}

function bool(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') throw new WireError(what)
  return value
}

function instant(value: unknown, what: string): string {
  if (typeof value !== 'string' || !INSTANT.test(value) || Number.isNaN(Date.parse(value))) throw new WireError(what)
  return value
}

function optionalInstant(value: unknown, what: string): string | undefined {
  return value === null || value === undefined ? undefined : instant(value, what)
}

function bounded(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) throw new WireError(what)
  return value
}

function optionalBounded(value: unknown, what: string, maximum: number): string | undefined {
  return value === null || value === undefined ? undefined : bounded(value, what, maximum)
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function optionalCount(value: unknown, what: string): number | undefined {
  return value === null || value === undefined ? undefined : integer(value, what)
}

function optionalBool(value: unknown, what: string): boolean | undefined {
  return value === null || value === undefined ? undefined : bool(value, what)
}

function recipient(value: unknown, what: string): AgentDesktopPlanCardView['recipient'] {
  return member(DISCLOSURE_RECIPIENTS, value, what)
}

function model(value: unknown, what: string): string {
  if (typeof value !== 'string' || !MODEL.test(value)) throw new WireError(what)
  return value
}

function stringList(value: unknown, what: string, maximum: number): string[] {
  if (!Array.isArray(value) || value.length > maximum) throw new WireError(what)
  return value.map((item) => {
    if (typeof item !== 'string' || !CODE.test(item)) throw new WireError(what)
    return item
  })
}

// ---- the card --------------------------------------------------------------------------------

function parsePlanValue(value: unknown): AgentDesktopPlanValueView {
  const item = record(value, 'desktop.plan.value')
  const ref = item.value_ref
  if (typeof ref !== 'string' || !VALUE_REF.test(ref)) throw new WireError('desktop.plan.value.ref')
  return {
    valueRef: ref,
    classification: bounded(item.classification, 'desktop.plan.value.classification', 32),
    length: integer(item.length, 'desktop.plan.value.length'),
    value: bounded(item.value, 'desktop.plan.value.text', 500)
  }
}

function parseCard(value: unknown): AgentDesktopPlanCardView {
  const card = record(value, 'desktop.plan.card')
  if (!Array.isArray(card.values) || card.values.length > 4) throw new WireError('desktop.plan.card.values')
  return {
    grantId: uuid(card.grant_id, 'desktop.plan.card.grant_id'),
    grantRevision: integer(card.grant_revision, 'desktop.plan.card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, card.grant_status, 'desktop.plan.card.grant_status'),
    ...(optionalInstant(card.expires_at, 'desktop.plan.card.expires_at') ? { expiresAt: optionalInstant(card.expires_at, 'desktop.plan.card.expires_at') as string } : {}),
    recipient: recipient(card.recipient, 'desktop.plan.card.recipient'),
    model: model(card.model, 'desktop.plan.card.model'),
    observedAt: instant(card.observed_at, 'desktop.plan.card.observed_at'),
    applicationLabel: bounded(card.application_label, 'desktop.plan.card.application', 64),
    windowTitle: bounded(card.window_title, 'desktop.plan.card.title', 120),
    maxNodes: integer(card.max_nodes, 'desktop.plan.card.max_nodes', 1),
    maxTextBytes: integer(card.max_text_bytes, 'desktop.plan.card.max_text_bytes', 1),
    redactionPolicy: bounded(card.redaction_policy, 'desktop.plan.card.redaction_policy', 64),
    observationAvailable: bool(card.observation_available, 'desktop.plan.card.available'),
    ...(optionalCount(card.node_count, 'desktop.plan.card.node_count') !== undefined ? { nodeCount: optionalCount(card.node_count, 'desktop.plan.card.node_count') as number } : {}),
    ...(optionalCount(card.text_bytes, 'desktop.plan.card.text_bytes') !== undefined ? { textBytes: optionalCount(card.text_bytes, 'desktop.plan.card.text_bytes') as number } : {}),
    ...(optionalCount(card.redaction_count, 'desktop.plan.card.redaction_count') !== undefined ? { redactionCount: optionalCount(card.redaction_count, 'desktop.plan.card.redaction_count') as number } : {}),
    ...(optionalBool(card.truncated, 'desktop.plan.card.truncated') !== undefined ? { truncated: optionalBool(card.truncated, 'desktop.plan.card.truncated') as boolean } : {}),
    truncation: stringList(card.truncation, 'desktop.plan.card.truncation', 8),
    values: card.values.map(parsePlanValue)
  }
}

function parseProposedAction(value: unknown): AgentProposedAction {
  const item = record(value, 'desktop.plan.proposed_action')
  if (item.action === 'invoke') {
    const ref = item.control_ref
    if (typeof ref !== 'string' || !CONTROL_REF.test(ref)) throw new WireError('desktop.plan.proposed_action.control_ref')
    return { action: 'invoke', controlRef: ref }
  }
  if (item.action === 'set_value') {
    const ref = item.control_ref
    const vref = item.value_ref
    if (typeof ref !== 'string' || !CONTROL_REF.test(ref)) throw new WireError('desktop.plan.proposed_action.control_ref')
    if (typeof vref !== 'string' || !VALUE_REF.test(vref)) throw new WireError('desktop.plan.proposed_action.value_ref')
    return { action: 'set_value', controlRef: ref, valueRef: vref }
  }
  if (item.action === 'select') {
    const container = item.container_ref
    const option = item.option_ref
    if (typeof container !== 'string' || !CONTROL_REF.test(container)) throw new WireError('desktop.plan.proposed_action.container_ref')
    if (typeof option !== 'string' || !CONTROL_REF.test(option)) throw new WireError('desktop.plan.proposed_action.option_ref')
    return { action: 'select', containerRef: container, optionRef: option }
  }
  throw new WireError('desktop.plan.proposed_action.action')
}

function parsePlanState(value: unknown): AgentDesktopPlanStateView {
  const plan = record(value, 'desktop.plan.state')
  const finishedAt = optionalInstant(plan.finished_at, 'desktop.plan.state.finished_at')
  const errorCode = optionalBounded(plan.error_code, 'desktop.plan.state.error_code', 40)
  if (errorCode !== undefined && !CODE.test(errorCode)) throw new WireError('desktop.plan.state.error_code')
  return {
    planId: uuid(plan.plan_id, 'desktop.plan.state.id'),
    status: member(PLAN_STATUSES, plan.status, 'desktop.plan.state.status'),
    ...(errorCode !== undefined ? { errorCode } : {}),
    startedAt: instant(plan.started_at, 'desktop.plan.state.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    nodeCount: integer(plan.node_count, 'desktop.plan.state.node_count'),
    textBytes: integer(plan.text_bytes, 'desktop.plan.state.text_bytes'),
    redactionCount: integer(plan.redaction_count, 'desktop.plan.state.redaction_count'),
    truncated: bool(plan.truncated, 'desktop.plan.state.truncated'),
    ...(plan.proposed_action !== null && plan.proposed_action !== undefined ? { proposedAction: parseProposedAction(plan.proposed_action) } : {})
  }
}

export function parseDesktopPlan(value: unknown): AgentDesktopPlanView {
  const plan = record(value, 'desktop.plan')
  const actionId = plan.action_id
  return {
    taskId: uuid(plan.task_id, 'desktop.plan.task_id'),
    taskStatus: member(TASK_STATUSES, plan.task_status, 'desktop.plan.task_status'),
    taskRevision: integer(plan.task_revision, 'desktop.plan.task_revision', 1),
    objective: bounded(plan.objective, 'desktop.plan.objective', 500),
    phase: member(DESKTOP_PLAN_PHASES, plan.phase, 'desktop.plan.phase'),
    ...(plan.card !== null && plan.card !== undefined ? { card: parseCard(plan.card) } : {}),
    ...(plan.plan !== null && plan.plan !== undefined ? { plan: parsePlanState(plan.plan) } : {}),
    ...(typeof actionId === 'string' && UUID.test(actionId) ? { actionId } : {})
  }
}

export function parseLatestDesktopPlan(value: unknown): AgentDesktopPlanView | null {
  const body = record(value, 'desktop.plan.latest')
  return body.plan === null || body.plan === undefined ? null : parseDesktopPlan(body.plan)
}

// ---- the released provider context ------------------------------------------------------------

export interface DesktopPlanClaim {
  planId: string
  taskId: string
  objective: string
  recipient: AgentDesktopPlanCardView['recipient']
  model: string
  projection: DesktopProjection
  values: AgentDesktopValueDescriptor[]
}

function parseNode(value: unknown): DesktopProjectionNode {
  const node = record(value, 'desktop.plan.node')
  const ref = node.control_ref
  if (typeof ref !== 'string' || !CONTROL_REF.test(ref)) throw new WireError('desktop.plan.node.ref')
  const parent = node.parent_ref
  if (parent !== null && parent !== undefined && (typeof parent !== 'string' || !CONTROL_REF.test(parent))) throw new WireError('desktop.plan.node.parent')
  if (typeof node.role !== 'string' || !ROLE.test(node.role)) throw new WireError('desktop.plan.node.role')
  const name = optionalBounded(node.name, 'desktop.plan.node.name', 400)
  const text = optionalBounded(node.text, 'desktop.plan.node.text', 400)
  const selected = optionalBool(node.selected, 'desktop.plan.node.selected')
  const expanded = optionalBool(node.expanded, 'desktop.plan.node.expanded')
  const checked = node.checked === null || node.checked === undefined ? undefined : member(['on', 'off', 'mixed'] as const, node.checked, 'desktop.plan.node.checked')
  return {
    controlRef: ref,
    ...(typeof parent === 'string' ? { parentRef: parent } : {}),
    role: node.role,
    ...(name !== undefined ? { name } : {}),
    ...(text !== undefined ? { text } : {}),
    enabled: bool(node.enabled, 'desktop.plan.node.enabled'),
    visible: bool(node.visible, 'desktop.plan.node.visible'),
    focused: bool(node.focused, 'desktop.plan.node.focused'),
    ...(selected !== undefined ? { selected } : {}),
    ...(checked !== undefined ? { checked } : {}),
    ...(expanded !== undefined ? { expanded } : {})
  }
}

function parseValueDescriptor(value: unknown): AgentDesktopValueDescriptor {
  const item = record(value, 'desktop.plan.claim.value')
  const ref = item.value_ref
  if (typeof ref !== 'string' || !VALUE_REF.test(ref)) throw new WireError('desktop.plan.claim.value.ref')
  return {
    valueRef: ref,
    classification: bounded(item.classification, 'desktop.plan.claim.value.classification', 32),
    length: integer(item.length, 'desktop.plan.claim.value.length')
  }
}

export function parseDesktopPlanClaim(value: unknown): DesktopPlanClaim {
  const body = record(value, 'desktop.plan.claim')
  const projection = record(body.projection, 'desktop.plan.claim.projection')
  if (!Array.isArray(projection.nodes) || projection.nodes.length > 200) throw new WireError('desktop.plan.claim.nodes')
  const nodes = projection.nodes.map(parseNode)
  if (projection.classification !== 'desktop_private' || projection.trust !== 'untrusted_environment') throw new WireError('desktop.plan.claim.classification')
  if (!Array.isArray(body.values) || body.values.length > 4) throw new WireError('desktop.plan.claim.values')
  return {
    planId: uuid(body.plan_id, 'desktop.plan.claim.plan_id'),
    taskId: uuid(body.task_id, 'desktop.plan.claim.task_id'),
    objective: bounded(body.objective, 'desktop.plan.claim.objective', 500),
    recipient: recipient(body.recipient, 'desktop.plan.claim.recipient'),
    model: model(body.model, 'desktop.plan.claim.model'),
    projection: {
      observedAt: instant(projection.observed_at, 'desktop.plan.claim.observed_at'),
      truncated: bool(projection.truncated, 'desktop.plan.claim.truncated'),
      truncation: stringList(projection.truncation, 'desktop.plan.claim.truncation', 8),
      nodeCount: integer(projection.node_count, 'desktop.plan.claim.node_count'),
      nodes
    },
    values: body.values.map(parseValueDescriptor)
  }
}

// ---- errors ---------------------------------------------------------------------------------

export function describeDesktopPlanStale(reason: string | undefined): string {
  switch (reason) {
    case 'grant_expired':
    case 'observation_stale':
      return 'That approval expired. Inspect the window again to get a new one. Nothing was sent.'
    case 'observation_unavailable':
    case 'observation_changed':
      return 'The snapshot you approved is no longer available. Inspect the window again. Nothing was sent.'
    case 'grant_changed':
      return 'The approval changed since you reviewed it. Review the current card.'
    case 'grant_not_pending':
    case 'grant_not_active':
    case 'grant_not_found':
      return 'That approval is no longer usable. Nothing further will be sent.'
    case 'plan_already_recorded':
      return 'That plan was already recorded. Nothing further will be sent.'
    default:
      return 'That desktop plan request is no longer current.'
  }
}

/** Project a runtime error to a closed, app-authored `AgentError`. Never desktop text. */
export function projectDesktopPlanRuntimeError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const code = typeof body?.code === 'string' ? body.code : ''
  const reason = typeof body?.reason === 'string' && CODE.test(body.reason) ? body.reason : undefined
  if (code === 'desktop_refused') return { code: 'desktop_refused', message: 'That desktop operation was refused. Nothing was inspected.' }
  if (code === 'desktop_plan_state_changed') return { code: 'desktop_read_stale', message: describeDesktopPlanStale(reason) }
  if (code === 'desktop_plan_refused' || code === 'invalid_request') {
    return { code: 'invalid_request', message: 'Lumi refused that desktop plan request. Nothing was sent.' }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That desktop plan no longer exists.' }
  return {
    code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed',
    message: 'The agent runtime could not complete that request.'
  }
}
