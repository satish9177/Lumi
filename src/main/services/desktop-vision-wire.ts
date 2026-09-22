import {
  DESKTOP_FALLBACK_REASONS,
  DESKTOP_VISION_PHASES,
  DISCLOSURE_RECIPIENTS,
  type AgentDesktopCaptureCardView,
  type AgentDesktopCaptureStateView,
  type AgentDesktopDisclosureCardView,
  type AgentDesktopDisclosureStateView,
  type AgentDesktopVisionView,
  type AgentDisclosureRecipient,
  type AgentError,
  type AgentTaskStatus,
  type AgentVisionCandidate
} from '../../shared/agent-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 9 S5 visual-fallback DTOs. Same
 * discipline as `desktop-planning-wire.ts`: a violation rejects the whole response rather than
 * guessing. Never read: a window handle, process id or path, a pixel, a coordinate, or an
 * executable action -- there is nowhere in this shape to put one.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const ATTEMPT_STATUSES = ['STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const

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

function finiteNumber(value: unknown, what: string, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) throw new WireError(what)
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

function recipient(value: unknown, what: string): AgentDisclosureRecipient {
  return member(DISCLOSURE_RECIPIENTS, value, what)
}

function model(value: unknown, what: string): string {
  if (typeof value !== 'string' || !MODEL.test(value)) throw new WireError(what)
  return value
}

// ---- cards and state --------------------------------------------------------------------------

function parseCaptureCard(value: unknown): AgentDesktopCaptureCardView {
  const card = record(value, 'desktop.vision.capture_card')
  const expiresAt = optionalInstant(card.expires_at, 'desktop.vision.capture_card.expires_at')
  return {
    grantId: uuid(card.grant_id, 'desktop.vision.capture_card.grant_id'),
    grantRevision: integer(card.grant_revision, 'desktop.vision.capture_card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, card.grant_status, 'desktop.vision.capture_card.grant_status'),
    ...(expiresAt ? { expiresAt } : {}),
    applicationLabel: bounded(card.application_label, 'desktop.vision.capture_card.application', 64),
    windowTitle: bounded(card.window_title, 'desktop.vision.capture_card.title', 120),
    fallbackReason: member(DESKTOP_FALLBACK_REASONS, card.fallback_reason, 'desktop.vision.capture_card.fallback_reason')
  }
}

function parseCaptureState(value: unknown): AgentDesktopCaptureStateView {
  const capture = record(value, 'desktop.vision.capture')
  const finishedAt = optionalInstant(capture.finished_at, 'desktop.vision.capture.finished_at')
  const errorCode = optionalBounded(capture.error_code, 'desktop.vision.capture.error_code', 40)
  if (errorCode !== undefined && !CODE.test(errorCode)) throw new WireError('desktop.vision.capture.error_code')
  const width = optionalCount(capture.width, 'desktop.vision.capture.width')
  const height = optionalCount(capture.height, 'desktop.vision.capture.height')
  const dpi = optionalCount(capture.dpi, 'desktop.vision.capture.dpi')
  return {
    captureId: uuid(capture.capture_id, 'desktop.vision.capture.id'),
    status: member(ATTEMPT_STATUSES, capture.status, 'desktop.vision.capture.status'),
    ...(errorCode !== undefined ? { errorCode } : {}),
    startedAt: instant(capture.started_at, 'desktop.vision.capture.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    ...(width !== undefined ? { width } : {}),
    ...(height !== undefined ? { height } : {}),
    ...(dpi !== undefined ? { dpi } : {})
  }
}

function parseDisclosureCard(value: unknown): AgentDesktopDisclosureCardView {
  const card = record(value, 'desktop.vision.disclosure_card')
  const expiresAt = optionalInstant(card.expires_at, 'desktop.vision.disclosure_card.expires_at')
  return {
    grantId: uuid(card.grant_id, 'desktop.vision.disclosure_card.grant_id'),
    grantRevision: integer(card.grant_revision, 'desktop.vision.disclosure_card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, card.grant_status, 'desktop.vision.disclosure_card.grant_status'),
    ...(expiresAt ? { expiresAt } : {}),
    applicationLabel: bounded(card.application_label, 'desktop.vision.disclosure_card.application', 64),
    windowTitle: bounded(card.window_title, 'desktop.vision.disclosure_card.title', 120),
    provider: recipient(card.provider, 'desktop.vision.disclosure_card.provider'),
    model: model(card.model, 'desktop.vision.disclosure_card.model'),
    purpose: bounded(card.purpose, 'desktop.vision.disclosure_card.purpose', 400)
  }
}

function parseCandidate(value: unknown): AgentVisionCandidate {
  const item = record(value, 'desktop.vision.candidate')
  if (item.schema_version !== 1 || item.kind !== 'candidate') throw new WireError('desktop.vision.candidate.shape')
  const region = record(item.region, 'desktop.vision.candidate.region')
  const observedText = optionalBounded(item.observed_text, 'desktop.vision.candidate.observed_text', 200)
  return {
    label: bounded(item.label, 'desktop.vision.candidate.label', 120),
    region: {
      x: finiteNumber(region.x, 'desktop.vision.candidate.region.x', 0, 1),
      y: finiteNumber(region.y, 'desktop.vision.candidate.region.y', 0, 1),
      w: finiteNumber(region.w, 'desktop.vision.candidate.region.w', 0, 1),
      h: finiteNumber(region.h, 'desktop.vision.candidate.region.h', 0, 1)
    },
    confidence: finiteNumber(item.confidence, 'desktop.vision.candidate.confidence', 0, 1),
    ...(observedText !== undefined ? { observedText } : {})
  }
}

function parseDisclosureState(value: unknown): AgentDesktopDisclosureStateView {
  const disclosure = record(value, 'desktop.vision.disclosure')
  const finishedAt = optionalInstant(disclosure.finished_at, 'desktop.vision.disclosure.finished_at')
  const errorCode = optionalBounded(disclosure.error_code, 'desktop.vision.disclosure.error_code', 40)
  if (errorCode !== undefined && !CODE.test(errorCode)) throw new WireError('desktop.vision.disclosure.error_code')
  const candidateCount = optionalCount(disclosure.candidate_count, 'desktop.vision.disclosure.candidate_count')
  return {
    disclosureId: uuid(disclosure.disclosure_id, 'desktop.vision.disclosure.id'),
    status: member(ATTEMPT_STATUSES, disclosure.status, 'desktop.vision.disclosure.status'),
    ...(errorCode !== undefined ? { errorCode } : {}),
    startedAt: instant(disclosure.started_at, 'desktop.vision.disclosure.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    ...(candidateCount !== undefined ? { candidateCount } : {})
  }
}

export function parseDesktopVision(value: unknown): AgentDesktopVisionView {
  const vision = record(value, 'desktop.vision')
  const candidates = vision.candidates
  return {
    taskId: uuid(vision.task_id, 'desktop.vision.task_id'),
    taskStatus: member(TASK_STATUSES, vision.task_status, 'desktop.vision.task_status'),
    taskRevision: integer(vision.task_revision, 'desktop.vision.task_revision', 1),
    objective: bounded(vision.objective, 'desktop.vision.objective', 500),
    phase: member(DESKTOP_VISION_PHASES, vision.phase, 'desktop.vision.phase'),
    ...(vision.capture_card !== null && vision.capture_card !== undefined ? { captureCard: parseCaptureCard(vision.capture_card) } : {}),
    ...(vision.capture !== null && vision.capture !== undefined ? { capture: parseCaptureState(vision.capture) } : {}),
    ...(vision.disclosure_card !== null && vision.disclosure_card !== undefined ? { disclosureCard: parseDisclosureCard(vision.disclosure_card) } : {}),
    ...(vision.disclosure !== null && vision.disclosure !== undefined ? { disclosure: parseDisclosureState(vision.disclosure) } : {}),
    ...(Array.isArray(candidates) ? { candidates: candidates.map(parseCandidate) } : {})
  }
}

// ---- what a claim releases --------------------------------------------------------------------

export interface RawCaptureClaim {
  captureId: string
  imageBase64: string
  width: number
  height: number
  dpi: number
}

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

function imageBase64(value: unknown, what: string): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 16_000_000 || !BASE64.test(value)) {
    throw new WireError(what)
  }
  return value
}

export function parseRawCapture(value: unknown): RawCaptureClaim {
  const body = record(value, 'desktop.vision.raw_capture')
  return {
    captureId: uuid(body.capture_id, 'desktop.vision.raw_capture.capture_id'),
    imageBase64: imageBase64(body.image_base64, 'desktop.vision.raw_capture.image_base64'),
    width: integer(body.width, 'desktop.vision.raw_capture.width', 1),
    height: integer(body.height, 'desktop.vision.raw_capture.height', 1),
    dpi: integer(body.dpi, 'desktop.vision.raw_capture.dpi', 1)
  }
}

export interface DisclosureProviderClaim {
  disclosureId: string
  taskId: string
  purpose: string
  recipient: AgentDisclosureRecipient
  model: string
  capture: RawCaptureClaim
}

export function parseDisclosureProviderContext(value: unknown): DisclosureProviderClaim {
  const body = record(value, 'desktop.vision.disclosure_claim')
  return {
    disclosureId: uuid(body.disclosure_id, 'desktop.vision.disclosure_claim.id'),
    taskId: uuid(body.task_id, 'desktop.vision.disclosure_claim.task_id'),
    purpose: bounded(body.purpose, 'desktop.vision.disclosure_claim.purpose', 400),
    recipient: recipient(body.recipient, 'desktop.vision.disclosure_claim.recipient'),
    model: model(body.model, 'desktop.vision.disclosure_claim.model'),
    capture: parseRawCapture(record(body.capture, 'desktop.vision.disclosure_claim.capture'))
  }
}

// ---- errors ---------------------------------------------------------------------------------

export function describeDesktopVisionStale(reason: string | undefined): string {
  switch (reason) {
    case 'grant_expired':
    case 'observation_stale':
      return 'That approval expired. Nothing was sent.'
    case 'observation_unavailable':
    case 'observation_changed':
      return 'The window this was about is no longer available. Try again.'
    case 'capture_not_succeeded':
      return 'The screenshot has not succeeded yet, so there is nothing to send.'
    case 'grant_changed':
      return 'The approval changed since you reviewed it. Review the current card.'
    case 'grant_not_pending':
    case 'grant_not_active':
    case 'grant_not_found':
      return 'That approval is no longer usable.'
    case 'disclosure_already_recorded':
      return 'That result was already recorded.'
    case 'disclosure_already_open':
      return 'A vision disclosure for this capture is already open.'
    default:
      return 'That desktop visual-fallback request is no longer current.'
  }
}

/** Project a runtime error to a closed, app-authored `AgentError`. Never desktop text, never a pixel. */
export function projectDesktopVisionRuntimeError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const code = typeof body?.code === 'string' ? body.code : ''
  const reason = typeof body?.reason === 'string' && CODE.test(body.reason) ? body.reason : undefined
  if (code === 'desktop_refused') return { code: 'desktop_refused', message: 'That desktop operation was refused.' }
  if (code === 'desktop_vision_state_changed') return { code: 'desktop_read_stale', message: describeDesktopVisionStale(reason) }
  if (code === 'desktop_vision_refused' || code === 'invalid_request') {
    return { code: 'invalid_request', message: 'Lumi refused that visual-fallback request.' }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That desktop visual-fallback task no longer exists.' }
  return {
    code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed',
    message: 'The agent runtime could not complete that request.'
  }
}
