import {
  DESKTOP_READ_PHASES,
  DISCLOSURE_RECIPIENTS,
  type AgentDesktopAnswerView,
  type AgentDesktopCardView,
  type AgentDesktopDisclosureView,
  type AgentDesktopReadView,
  type AgentDesktopSurface,
  type AgentDesktopSurfaceList,
  type AgentError,
  type AgentTaskStatus
} from '../../shared/agent-contracts'
import type { DesktopProjection, DesktopProjectionNode } from '../agent/desktop-reader'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 9 S2 DTOs.
 *
 * Same discipline as `agent-wire.ts`: a violation rejects the whole response rather than guessing, and
 * only known, bounded fields are ever copied. What is deliberately never read, even where the runtime
 * could send it: a window handle, process id or path, an AutomationId, class name, framework id,
 * RuntimeId, coordinate, pattern list, snapshot, snapshot digest or another observation.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const CONTROL_REF = /^u(?:[1-9]\d?|1\d\d|200)$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const DISCLOSURE_STATUSES = ['STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const
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

// ---- surfaces ---------------------------------------------------------------------------------

function parseSurface(value: unknown): AgentDesktopSurface {
  const surface = record(value, 'desktop.surface')
  const ref = surface.surface_ref
  if (typeof ref !== 'string' || !SURFACE_REF.test(ref)) throw new WireError('desktop.surface.ref')
  return {
    surfaceRef: ref,
    surfaceEpoch: integer(surface.surface_epoch, 'desktop.surface.epoch', 1),
    applicationLabel: bounded(surface.application_label, 'desktop.surface.application', 64),
    windowTitle: bounded(surface.window_title, 'desktop.surface.title', 120),
    visible: bool(surface.visible, 'desktop.surface.visible'),
    minimized: bool(surface.minimized, 'desktop.surface.minimized')
  }
}

export function parseDesktopSurfaceList(value: unknown): AgentDesktopSurfaceList {
  const body = record(value, 'desktop.surfaces')
  if (!Array.isArray(body.surfaces) || body.surfaces.length > 16) throw new WireError('desktop.surfaces.list')
  return {
    workerGeneration: uuid(body.worker_generation, 'desktop.surfaces.generation'),
    surfaces: body.surfaces.map(parseSurface),
    truncated: bool(body.truncated, 'desktop.surfaces.truncated')
  }
}

// ---- one desktop read -----------------------------------------------------------------------

function recipient(value: unknown, what: string): AgentDesktopCardView['recipient'] {
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

function parseCard(value: unknown): AgentDesktopCardView {
  const card = record(value, 'desktop.card')
  return {
    grantId: uuid(card.grant_id, 'desktop.card.grant_id'),
    grantRevision: integer(card.grant_revision, 'desktop.card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, card.grant_status, 'desktop.card.grant_status'),
    ...(optionalInstant(card.expires_at, 'desktop.card.expires_at') ? { expiresAt: optionalInstant(card.expires_at, 'desktop.card.expires_at') as string } : {}),
    recipient: recipient(card.recipient, 'desktop.card.recipient'),
    model: model(card.model, 'desktop.card.model'),
    observedAt: instant(card.observed_at, 'desktop.card.observed_at'),
    applicationLabel: bounded(card.application_label, 'desktop.card.application', 64),
    windowTitle: bounded(card.window_title, 'desktop.card.title', 120),
    maxNodes: integer(card.max_nodes, 'desktop.card.max_nodes', 1),
    maxTextBytes: integer(card.max_text_bytes, 'desktop.card.max_text_bytes', 1),
    redactionPolicy: bounded(card.redaction_policy, 'desktop.card.redaction_policy', 64),
    observationAvailable: bool(card.observation_available, 'desktop.card.available'),
    ...(optionalCount(card.node_count, 'desktop.card.node_count') !== undefined ? { nodeCount: optionalCount(card.node_count, 'desktop.card.node_count') as number } : {}),
    ...(optionalCount(card.text_bytes, 'desktop.card.text_bytes') !== undefined ? { textBytes: optionalCount(card.text_bytes, 'desktop.card.text_bytes') as number } : {}),
    ...(optionalCount(card.redaction_count, 'desktop.card.redaction_count') !== undefined ? { redactionCount: optionalCount(card.redaction_count, 'desktop.card.redaction_count') as number } : {}),
    ...(optionalBool(card.truncated, 'desktop.card.truncated') !== undefined ? { truncated: optionalBool(card.truncated, 'desktop.card.truncated') as boolean } : {}),
    truncation: stringList(card.truncation, 'desktop.card.truncation', 8)
  }
}

function parseDisclosure(value: unknown): AgentDesktopDisclosureView {
  const disclosure = record(value, 'desktop.disclosure')
  const finishedAt = optionalInstant(disclosure.finished_at, 'desktop.disclosure.finished_at')
  const errorCode = optionalBounded(disclosure.error_code, 'desktop.disclosure.error_code', 40)
  if (errorCode !== undefined && !CODE.test(errorCode)) throw new WireError('desktop.disclosure.error_code')
  return {
    disclosureId: uuid(disclosure.disclosure_id, 'desktop.disclosure.id'),
    status: member(DISCLOSURE_STATUSES, disclosure.status, 'desktop.disclosure.status'),
    ...(errorCode !== undefined ? { errorCode } : {}),
    startedAt: instant(disclosure.started_at, 'desktop.disclosure.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    nodeCount: integer(disclosure.node_count, 'desktop.disclosure.node_count'),
    textBytes: integer(disclosure.text_bytes, 'desktop.disclosure.text_bytes'),
    redactionCount: integer(disclosure.redaction_count, 'desktop.disclosure.redaction_count'),
    truncated: bool(disclosure.truncated, 'desktop.disclosure.truncated')
  }
}

function parseAnswer(value: unknown): AgentDesktopAnswerView {
  const answer = record(value, 'desktop.answer')
  if (!Array.isArray(answer.evidence) || answer.evidence.length > 6) throw new WireError('desktop.answer.evidence')
  const text = optionalBounded(answer.answer, 'desktop.answer.answer', 1_200)
  const reason = optionalBounded(answer.reason, 'desktop.answer.reason', 24)
  if (reason !== undefined && !CODE.test(reason)) throw new WireError('desktop.answer.reason')
  const observedAt = optionalInstant(answer.observed_at, 'desktop.answer.observed_at')
  return {
    kind: member(['answer', 'cannot_answer'] as const, answer.kind, 'desktop.answer.kind'),
    ...(text !== undefined ? { answer: text } : {}),
    ...(reason !== undefined ? { reason } : {}),
    evidence: answer.evidence.map((raw) => {
      const item = record(raw, 'desktop.answer.evidence.item')
      if (typeof item.control_ref !== 'string' || !CONTROL_REF.test(item.control_ref)) throw new WireError('desktop.answer.control_ref')
      return { controlRef: item.control_ref, quote: bounded(item.quote, 'desktop.answer.quote', 200) }
    }),
    recipient: recipient(answer.recipient, 'desktop.answer.recipient'),
    model: model(answer.model, 'desktop.answer.model'),
    ...(observedAt ? { observedAt } : {}),
    createdAt: instant(answer.created_at, 'desktop.answer.created_at')
  }
}

export function parseDesktopRead(value: unknown): AgentDesktopReadView {
  const read = record(value, 'desktop.read')
  return {
    taskId: uuid(read.task_id, 'desktop.read.task_id'),
    taskStatus: member(TASK_STATUSES, read.task_status, 'desktop.read.task_status'),
    taskRevision: integer(read.task_revision, 'desktop.read.task_revision', 1),
    objective: bounded(read.objective, 'desktop.read.objective', 500),
    phase: member(DESKTOP_READ_PHASES, read.phase, 'desktop.read.phase'),
    ...(read.card !== null && read.card !== undefined ? { card: parseCard(read.card) } : {}),
    ...(read.disclosure !== null && read.disclosure !== undefined ? { disclosure: parseDisclosure(read.disclosure) } : {}),
    ...(read.answer !== null && read.answer !== undefined ? { answer: parseAnswer(read.answer) } : {})
  }
}

export function parseLatestDesktopRead(value: unknown): AgentDesktopReadView | null {
  const body = record(value, 'desktop.latest')
  return body.read === null || body.read === undefined ? null : parseDesktopRead(body.read)
}

// ---- the released provider context ------------------------------------------------------------

export interface DesktopClaim {
  disclosureId: string
  taskId: string
  objective: string
  recipient: AgentDesktopCardView['recipient']
  model: string
  projection: DesktopProjection
}

function parseNode(value: unknown): DesktopProjectionNode {
  const node = record(value, 'desktop.node')
  const ref = node.control_ref
  if (typeof ref !== 'string' || !CONTROL_REF.test(ref)) throw new WireError('desktop.node.ref')
  const parent = node.parent_ref
  if (parent !== null && parent !== undefined && (typeof parent !== 'string' || !CONTROL_REF.test(parent))) throw new WireError('desktop.node.parent')
  if (typeof node.role !== 'string' || !ROLE.test(node.role)) throw new WireError('desktop.node.role')
  const name = optionalBounded(node.name, 'desktop.node.name', 400)
  const text = optionalBounded(node.text, 'desktop.node.text', 400)
  const selected = optionalBool(node.selected, 'desktop.node.selected')
  const expanded = optionalBool(node.expanded, 'desktop.node.expanded')
  const checked = node.checked === null || node.checked === undefined ? undefined : member(['on', 'off', 'mixed'] as const, node.checked, 'desktop.node.checked')
  return {
    controlRef: ref,
    ...(typeof parent === 'string' ? { parentRef: parent } : {}),
    role: node.role,
    ...(name !== undefined ? { name } : {}),
    ...(text !== undefined ? { text } : {}),
    enabled: bool(node.enabled, 'desktop.node.enabled'),
    visible: bool(node.visible, 'desktop.node.visible'),
    focused: bool(node.focused, 'desktop.node.focused'),
    ...(selected !== undefined ? { selected } : {}),
    ...(checked !== undefined ? { checked } : {}),
    ...(expanded !== undefined ? { expanded } : {})
  }
}

export function parseDesktopClaim(value: unknown): DesktopClaim {
  const body = record(value, 'desktop.claim')
  const projection = record(body.projection, 'desktop.claim.projection')
  if (!Array.isArray(projection.nodes) || projection.nodes.length > 200) throw new WireError('desktop.claim.nodes')
  const nodes = projection.nodes.map(parseNode)
  if (projection.classification !== 'desktop_private' || projection.trust !== 'untrusted_environment') throw new WireError('desktop.claim.classification')
  return {
    disclosureId: uuid(body.disclosure_id, 'desktop.claim.disclosure_id'),
    taskId: uuid(body.task_id, 'desktop.claim.task_id'),
    objective: bounded(body.objective, 'desktop.claim.objective', 500),
    recipient: recipient(body.recipient, 'desktop.claim.recipient'),
    model: model(body.model, 'desktop.claim.model'),
    projection: {
      observedAt: instant(projection.observed_at, 'desktop.claim.observed_at'),
      truncated: bool(projection.truncated, 'desktop.claim.truncated'),
      truncation: stringList(projection.truncation, 'desktop.claim.truncation', 8),
      nodeCount: integer(projection.node_count, 'desktop.claim.node_count'),
      nodes
    }
  }
}

// ---- errors ---------------------------------------------------------------------------------

/** App-authored wording for a closed desktop refusal reason. Desktop text never reaches this function. */
export function describeDesktopRefusal(reason: string | undefined): string {
  switch (reason) {
    case 'desktop_automation_disabled':
      return 'Desktop reading is not turned on in this build. Nothing was inspected.'
    case 'desktop_automation_unsupported':
      return 'Desktop reading is only available on Windows. Nothing was inspected.'
    case 'desktop_worker_unavailable':
      return 'Lumi could not start its desktop reader. Nothing was inspected.'
    case 'desktop_observation_timeout':
      return 'That window took too long to read, so Lumi stopped. Nothing was sent anywhere.'
    case 'stale_surface':
    case 'stale_worker_generation':
    case 'surface_unavailable':
    case 'surface_changed':
      return 'That window changed or went away. Choose it again from the list. Nothing was sent anywhere.'
    case 'elevated_window_refused':
    case 'integrity_unverifiable':
      return 'Lumi does not read windows running with higher privileges. Nothing was inspected.'
    case 'credential_surface':
      return 'That window has a password or sign-in field, so Lumi did not read any of it.'
    case 'desktop_backend_failed':
      return 'Lumi could not read that window. Nothing was sent anywhere.'
    default:
      return 'Lumi could not read that window. Nothing was sent anywhere.'
  }
}

export function describeDesktopStale(reason: string | undefined): string {
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
    default:
      return 'That desktop request is no longer current.'
  }
}

/** Project a runtime error to a closed, app-authored `AgentError`. Never desktop text. */
export function projectDesktopRuntimeError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const code = typeof body?.code === 'string' ? body.code : ''
  const reason = typeof body?.reason === 'string' && CODE.test(body.reason) ? body.reason : undefined
  if (code === 'desktop_refused') return { code: 'desktop_refused', message: describeDesktopRefusal(reason) }
  if (code === 'desktop_disclosure_state_changed') return { code: 'desktop_read_stale', message: describeDesktopStale(reason) }
  if (code === 'desktop_disclosure_refused' || code === 'invalid_request') {
    return { code: 'invalid_request', message: 'Lumi refused that desktop request. Nothing was sent.' }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That desktop read no longer exists.' }
  return {
    code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed',
    message: 'The agent runtime could not complete that request.'
  }
}
