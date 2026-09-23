import type { AgentError, AgentTaskStatus } from '../../shared/agent-contracts'
import {
  TRANSFER_KINDS,
  TRANSFER_PHASES,
  type AgentTransferCardView,
  type AgentTransferView
} from '../../shared/transfer-contracts'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parser from runtime JSON to the closed Milestone 10 S2 transfer DTO. A violation rejects the whole
 * response and only known, bounded fields are copied. Any string that looks like an absolute Windows path
 * is refused wherever it appears, so a runtime regression that leaked a quarantine or destination path
 * could not carry it to the renderer.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const STATUS = /^[A-Z][A-Z_]{0,31}$/
const SHA256 = /^[0-9a-f]{64}$/
const ABSOLUTE_PATH = /(^|[\s"'(])([A-Za-z]:[\\/]|\\\\|\/\/\?\/)/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const MAX_BYTES = 10 * 1024 * 1024

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

function text(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum || ABSOLUTE_PATH.test(value)) throw new WireError(what)
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function pattern(value: unknown, what: string, shape: RegExp): string {
  if (typeof value !== 'string' || !shape.test(value)) throw new WireError(what)
  return value
}

function sourceUrl(value: unknown, what: string): string {
  const url = text(value, what, 2048)
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    throw new WireError(what)
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') throw new WireError(what)
  return url
}

function card(value: unknown): AgentTransferCardView {
  const raw = record(value, 'transfer.card')
  if (raw.overwrite !== false) throw new WireError('transfer.card.overwrite')
  const expiresAt = optional(raw.expires_at, (v) => pattern(v, 'transfer.card.expires_at', INSTANT))
  return {
    grantId: uuid(raw.grant_id, 'transfer.card.grant_id'),
    grantRevision: integer(raw.grant_revision, 'transfer.card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, raw.grant_status, 'transfer.card.grant_status'),
    ...(expiresAt ? { expiresAt } : {}),
    sourceUrl: sourceUrl(raw.source_url, 'transfer.card.source_url'),
    sourceOrigin: sourceUrl(raw.source_origin, 'transfer.card.source_origin'),
    intent: text(raw.intent, 'transfer.card.intent', 300),
    destRootId: uuid(raw.dest_root_id, 'transfer.card.dest_root_id'),
    destRootLabel: text(raw.dest_root_label, 'transfer.card.dest_root_label', 64),
    destName: text(raw.dest_name, 'transfer.card.dest_name', 255),
    expectedKind: member(TRANSFER_KINDS, raw.expected_kind, 'transfer.card.expected_kind'),
    maxBytes: integer(raw.max_bytes, 'transfer.card.max_bytes', 1, MAX_BYTES),
    overwrite: false
  }
}

export function parseTransfer(value: unknown): AgentTransferView {
  const raw = record(value, 'transfer')
  const errorCode = optional(raw.error_code, (v) => pattern(v, 'transfer.error_code', CODE))
  const downloadStatus = optional(raw.download_status, (v) => pattern(v, 'transfer.download_status', STATUS))
  const placeStatus = optional(raw.place_status, (v) => pattern(v, 'transfer.place_status', STATUS))
  const length = optional(raw.length, (v) => integer(v, 'transfer.length', 0, MAX_BYTES))
  const sha256 = optional(raw.sha256, (v) => pattern(v, 'transfer.sha256', SHA256))
  const kind = optional(raw.kind, (v) => member(TRANSFER_KINDS, v, 'transfer.kind'))
  const parsedCard = optional(raw.card, card)
  return {
    taskId: uuid(raw.task_id, 'transfer.task_id'),
    taskStatus: member(TASK_STATUSES, raw.task_status, 'transfer.task_status'),
    taskRevision: integer(raw.task_revision, 'transfer.task_revision', 1),
    transferId: uuid(raw.transfer_id, 'transfer.transfer_id'),
    phase: member(TRANSFER_PHASES, raw.phase, 'transfer.phase'),
    status: pattern(raw.status, 'transfer.status', STATUS),
    ...(errorCode ? { errorCode } : {}),
    ...(downloadStatus ? { downloadStatus } : {}),
    ...(placeStatus ? { placeStatus } : {}),
    ...(length !== undefined ? { length } : {}),
    ...(sha256 ? { sha256 } : {}),
    ...(kind ? { kind } : {}),
    destRootLabel: text(raw.dest_root_label, 'transfer.dest_root_label', 64),
    destName: text(raw.dest_name, 'transfer.dest_name', 255),
    ...(parsedCard ? { card: parsedCard } : {})
  }
}

export function parseLatestTransfer(value: unknown): AgentTransferView | null {
  const raw = record(value, 'latest_transfer')
  return raw.transfer === null || raw.transfer === undefined ? null : parseTransfer(raw.transfer)
}

const REASONS: Record<string, string> = {
  destination_exists: 'A file with that name is already in the folder. Lumi never replaces a file; choose another name.',
  destination_type_refused: 'Lumi only saves PDF, Word (.docx) or text files.',
  reserved_name: 'That file name is reserved by Windows. Choose another name.',
  not_a_file_name: 'Type just a file name, not a folder or path.',
  permission_missing: 'That folder was not approved for saving downloads.',
  root_revoked: 'That folder is no longer approved.',
  root_not_found: 'That folder is no longer approved.',
  root_changed: 'That folder changed since it was approved. Nothing was saved.',
  url_invalid: 'That address cannot be downloaded by Lumi.',
  url_no_longer_allowed: 'That address is no longer allowed. Nothing was downloaded.',
  url_not_canonical: 'Use the plain address, without a user name or unusual characters.',
  download_type_refused: 'The download was not a PDF, Word or text document, so Lumi refused it and saved nothing.',
  quarantine_changed: 'The downloaded copy changed before it could be saved, so Lumi saved nothing.',
  provenance_missing: 'The downloaded copy lost its internet marker, so Lumi saved nothing.',
  grant_not_active: 'Approve the download on the card first.',
  grant_expired: 'That approval expired. Nothing was downloaded.',
  grant_changed: 'That approval changed since you reviewed it.',
  wrong_phase: 'That step is not available now.',
  nothing_to_reconcile: 'There is nothing uncertain to check.',
  downloads_not_configured: 'Downloads are not available in this build.'
}

const DEFAULTS: Record<string, string> = {
  transfer_state_changed: 'That download can no longer go ahead as approved. Nothing more was done.',
  transfer_refused: 'Lumi refused that download request. Nothing was downloaded or saved.'
}

export function projectTransferRuntimeError(status: number, body: unknown): AgentError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined
  const code = typeof error?.code === 'string' && CODE.test(error.code) ? error.code : undefined
  const reason = typeof error?.reason === 'string' && CODE.test(error.reason) ? error.reason : undefined
  if (code === 'transfer_state_changed' || code === 'transfer_refused') {
    return { code, message: (reason !== undefined ? REASONS[reason] : undefined) ?? DEFAULTS[code] }
  }
  if (code === 'effect_locked') {
    return { code, message: 'An earlier download or save with the same effect is still uncertain. Check it first; nothing was done.' }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That download no longer exists.' }
  if (code === 'task_not_accepting_actions') return { code: 'not_accepting_actions', message: 'That task has ended.' }
  return { code: status === 422 ? 'invalid_request' : 'request_failed', message: 'Lumi could not complete that download request.' }
}
