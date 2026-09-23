import { DISCLOSURE_RECIPIENTS, type AgentDisclosureRecipient, type AgentError, type AgentTaskStatus } from '../../shared/agent-contracts'
import {
  DOCUMENT_FORMATS,
  DOCUMENT_PHASES,
  type AgentDocumentCardView,
  type AgentDocumentComparisonView,
  type AgentDocumentDisclosureStateView,
  type AgentDocumentFileView,
  type AgentDocumentFormat,
  type AgentDocumentShapeView,
  type AgentDocumentTaskView,
  type AgentDocumentView,
  type AgentFileRootView,
  type AgentListedFileView,
  type AgentLocalComparisonView,
  type AgentRootListingView
} from '../../shared/document-contracts'
import type { DocRef, DocumentProjection } from '../agent/document-comparer'
import { WireError, isRecord } from './agent-wire'

/**
 * Strict parsers from runtime JSON to the closed Milestone 10 S1 document DTOs. Same discipline as the
 * desktop wires: a violation rejects the whole response and only known, bounded fields are copied.
 *
 * A defence in depth on top of the runtime's own models: any string field that looks like an absolute
 * Windows path (`C:\`, `\\server`, `\\?\`) is refused wherever it appears, so a runtime regression that
 * leaked one could not carry it to the renderer.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const ABSOLUTE_PATH = /(^|[\s"'(])([A-Za-z]:[\\/]|\\\\|\/\/\?\/)/
const TASK_STATUSES: readonly AgentTaskStatus[] = [
  'CREATED', 'PLANNING', 'READY', 'WAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'OUTCOME_UNKNOWN',
  'RECONCILING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'
]
const GRANT_STATUSES = ['PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'COMPLETED'] as const
const DISCLOSURE_STATUSES = ['STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'] as const
const FINDING_KINDS = ['match', 'gap', 'difference'] as const
const REFS = ['d1', 'd2'] as const

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function optionalUuid(value: unknown, what: string): string | undefined {
  return value === null || value === undefined ? undefined : uuid(value, what)
}

function integer(value: unknown, what: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) throw new WireError(what)
  return value
}

function optionalInteger(value: unknown, what: string): number | undefined {
  return value === null || value === undefined ? undefined : integer(value, what)
}

function instant(value: unknown, what: string): string {
  if (typeof value !== 'string' || !INSTANT.test(value) || Number.isNaN(Date.parse(value))) throw new WireError(what)
  return value
}

function optionalInstant(value: unknown, what: string): string | undefined {
  return value === null || value === undefined ? undefined : instant(value, what)
}

function bool(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') throw new WireError(what)
  return value
}

function optionalBool(value: unknown, what: string): boolean | undefined {
  return value === null || value === undefined ? undefined : bool(value, what)
}

/** Bounded text that is never an absolute path. */
function text(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum || ABSOLUTE_PATH.test(value)) throw new WireError(what)
  return value
}

function optionalText(value: unknown, what: string, maximum: number): string | undefined {
  return value === null || value === undefined ? undefined : text(value, what, maximum)
}

/** Document text shown to the person: bounded, but it may legitimately mention a path. Never forwarded. */
function documentText(value: unknown, what: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) throw new WireError(what)
  return value
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function format(value: unknown, what: string): AgentDocumentFormat {
  return member(DOCUMENT_FORMATS, value, what)
}

function recipient(value: unknown, what: string): AgentDisclosureRecipient {
  return member(DISCLOSURE_RECIPIENTS, value, what)
}

function model(value: unknown, what: string): string {
  if (typeof value !== 'string' || !MODEL.test(value)) throw new WireError(what)
  return value
}

function flags(value: unknown, what: string): Record<string, number> {
  const raw = record(value, what)
  const out: Record<string, number> = {}
  for (const [key, count] of Object.entries(raw)) {
    if (!CODE.test(key)) throw new WireError(what)
    out[key] = integer(count, what)
  }
  return out
}

function list(value: unknown, what: string, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new WireError(what)
  return value
}

// ---- roots --------------------------------------------------------------------------------------

export function parseFileRoot(value: unknown): AgentFileRootView {
  const root = record(value, 'file_root')
  return {
    rootId: uuid(root.root_id, 'file_root.id'),
    label: text(root.label, 'file_root.label', 64),
    canRead: bool(root.can_read, 'file_root.can_read'),
    canCreate: bool(root.can_create, 'file_root.can_create'),
    canModify: bool(root.can_modify, 'file_root.can_modify'),
    revision: integer(root.revision, 'file_root.revision', 1),
    createdAt: instant(root.created_at, 'file_root.created_at')
  }
}

export function parseFileRoots(value: unknown): AgentFileRootView[] {
  return list(record(value, 'file_roots').roots, 'file_roots.roots', 64).map(parseFileRoot)
}

export function parseRootListing(value: unknown): AgentRootListingView {
  const listing = record(value, 'root_listing')
  return {
    rootId: uuid(listing.root_id, 'root_listing.id'),
    truncated: bool(listing.truncated, 'root_listing.truncated'),
    files: list(listing.files, 'root_listing.files', 200).map((raw): AgentListedFileView => {
      const item = record(raw, 'root_listing.file')
      return {
        relativePath: text(item.relative_path, 'root_listing.file.relative_path', 512),
        name: text(item.name, 'root_listing.file.name', 255),
        sizeBytes: integer(item.size_bytes, 'root_listing.file.size'),
        modifiedAt: instant(item.modified_at, 'root_listing.file.modified_at'),
        format: format(item.format, 'root_listing.file.format')
      }
    })
  }
}

// ---- document tasks -----------------------------------------------------------------------------

function parseFile(value: unknown): AgentDocumentFileView {
  const item = record(value, 'document.file')
  const relativePath = optionalText(item.relative_path, 'document.file.relative_path', 512)
  const rootId = optionalUuid(item.root_id, 'document.file.root_id')
  return {
    fileId: uuid(item.file_id, 'document.file.id'),
    source: member(['ROOT_FILE', 'DROPPED_FILE'] as const, item.source, 'document.file.source'),
    displayName: text(item.display_name, 'document.file.name', 255),
    ...(relativePath !== undefined ? { relativePath } : {}),
    ...(rootId !== undefined ? { rootId } : {}),
    format: format(item.format, 'document.file.format'),
    sizeBytes: integer(item.size_bytes, 'document.file.size'),
    addedAt: instant(item.added_at, 'document.file.added_at')
  }
}

function parseDocument(value: unknown): AgentDocumentView {
  const item = record(value, 'document')
  const preview = item.preview === null || item.preview === undefined ? undefined : documentText(item.preview, 'document.preview', 2_000)
  const pageCount = optionalInteger(item.page_count, 'document.page_count')
  return {
    documentId: uuid(item.document_id, 'document.id'),
    fileId: uuid(item.file_id, 'document.file_id'),
    format: format(item.format, 'document.format'),
    ...(pageCount !== undefined ? { pageCount } : {}),
    textChars: integer(item.text_chars, 'document.text_chars'),
    truncated: bool(item.truncated, 'document.truncated'),
    flags: flags(item.flags, 'document.flags'),
    ...(preview !== undefined ? { preview } : {}),
    expiresAt: instant(item.expires_at, 'document.expires_at'),
    purged: bool(item.purged, 'document.purged')
  }
}

function parseCard(value: unknown): AgentDocumentCardView {
  const card = record(value, 'document.card')
  const expiresAt = optionalInstant(card.expires_at, 'document.card.expires_at')
  const textBytes = optionalInteger(card.text_bytes, 'document.card.text_bytes')
  const redactionCount = optionalInteger(card.redaction_count, 'document.card.redaction_count')
  const truncated = optionalBool(card.truncated, 'document.card.truncated')
  return {
    grantId: uuid(card.grant_id, 'document.card.grant_id'),
    grantRevision: integer(card.grant_revision, 'document.card.grant_revision', 1),
    grantStatus: member(GRANT_STATUSES, card.grant_status, 'document.card.grant_status'),
    ...(expiresAt ? { expiresAt } : {}),
    provider: recipient(card.recipient, 'document.card.recipient'),
    model: model(card.model, 'document.card.model'),
    purpose: text(card.purpose, 'document.card.purpose', 400),
    documents: list(card.documents, 'document.card.documents', 2).map((raw) => {
      const item = record(raw, 'document.card.document')
      const excerpt = item.excerpt === null || item.excerpt === undefined ? undefined : documentText(item.excerpt, 'document.card.excerpt', 8_192)
      return {
        docRef: member(REFS, item.doc_ref, 'document.card.doc_ref'),
        documentId: uuid(item.document_id, 'document.card.document_id'),
        label: text(item.label, 'document.card.label', 255),
        ...(excerpt !== undefined ? { excerpt } : {})
      }
    }),
    maxExcerptBytes: integer(card.max_excerpt_bytes, 'document.card.max_excerpt_bytes', 1),
    ...(textBytes !== undefined ? { textBytes } : {}),
    ...(redactionCount !== undefined ? { redactionCount } : {}),
    ...(truncated !== undefined ? { truncated } : {}),
    redactionPolicy: text(card.redaction_policy, 'document.card.redaction_policy', 64)
  }
}

function parseDisclosure(value: unknown): AgentDocumentDisclosureStateView {
  const item = record(value, 'document.disclosure')
  const errorCode = optionalText(item.error_code, 'document.disclosure.error_code', 40)
  if (errorCode !== undefined && !CODE.test(errorCode)) throw new WireError('document.disclosure.error_code')
  const finishedAt = optionalInstant(item.finished_at, 'document.disclosure.finished_at')
  return {
    disclosureId: uuid(item.disclosure_id, 'document.disclosure.id'),
    status: member(DISCLOSURE_STATUSES, item.status, 'document.disclosure.status'),
    ...(errorCode !== undefined ? { errorCode } : {}),
    startedAt: instant(item.started_at, 'document.disclosure.started_at'),
    ...(finishedAt ? { finishedAt } : {}),
    textBytes: integer(item.text_bytes, 'document.disclosure.text_bytes'),
    redactionCount: integer(item.redaction_count, 'document.disclosure.redaction_count'),
    truncated: bool(item.truncated, 'document.disclosure.truncated')
  }
}

function parseAnswer(value: unknown): AgentDocumentComparisonView {
  const item = record(value, 'document.answer')
  const summary = item.summary === null || item.summary === undefined ? undefined : documentText(item.summary, 'document.answer.summary', 800)
  const reason = optionalText(item.reason, 'document.answer.reason', 24)
  return {
    kind: member(['comparison', 'cannot_compare'] as const, item.kind, 'document.answer.kind'),
    ...(summary !== undefined ? { summary } : {}),
    ...(reason !== undefined ? { reason } : {}),
    findings: list(item.findings, 'document.answer.findings', 8).map((raw) => {
      const finding = record(raw, 'document.answer.finding')
      return {
        kind: member(FINDING_KINDS, finding.kind, 'document.answer.finding.kind'),
        text: documentText(finding.text, 'document.answer.finding.text', 300),
        evidence: list(finding.evidence, 'document.answer.finding.evidence', 3).map((rawEvidence) => {
          const evidence = record(rawEvidence, 'document.answer.evidence')
          return {
            docRef: member(REFS, evidence.doc_ref, 'document.answer.evidence.doc_ref'),
            quote: documentText(evidence.quote, 'document.answer.evidence.quote', 200)
          }
        })
      }
    }),
    provider: recipient(item.recipient, 'document.answer.recipient'),
    model: model(item.model, 'document.answer.model'),
    createdAt: instant(item.created_at, 'document.answer.created_at')
  }
}

export function parseDocumentTask(value: unknown): AgentDocumentTaskView {
  const task = record(value, 'document_task')
  return {
    taskId: uuid(task.task_id, 'document_task.id'),
    taskStatus: member(TASK_STATUSES, task.task_status, 'document_task.status'),
    taskRevision: integer(task.task_revision, 'document_task.revision', 1),
    objective: text(task.objective, 'document_task.objective', 300),
    phase: member(DOCUMENT_PHASES, task.phase, 'document_task.phase'),
    files: list(task.files, 'document_task.files', 4).map(parseFile),
    documents: list(task.documents, 'document_task.documents', 4).map(parseDocument),
    ...(task.card === null || task.card === undefined ? {} : { card: parseCard(task.card) }),
    ...(task.disclosure === null || task.disclosure === undefined ? {} : { disclosure: parseDisclosure(task.disclosure) }),
    ...(task.answer === null || task.answer === undefined ? {} : { answer: parseAnswer(task.answer) })
  }
}

export function parseLatestDocumentTask(value: unknown): AgentDocumentTaskView | null {
  const body = record(value, 'latest_document_task')
  return body.task === null ? null : parseDocumentTask(body.task)
}

function parseShape(value: unknown, what: string): AgentDocumentShapeView {
  const shape = record(value, what)
  return {
    characters: integer(shape.characters, `${what}.characters`),
    words: integer(shape.words, `${what}.words`),
    lines: integer(shape.lines, `${what}.lines`),
    headings: list(shape.headings, `${what}.headings`, 20).map((item) => documentText(item, `${what}.heading`, 48))
  }
}

export function parseLocalComparison(value: unknown): AgentLocalComparisonView {
  const comparison = record(value, 'local_comparison')
  const terms = (raw: unknown, what: string, maximum: number): string[] =>
    list(raw, what, maximum).map((item) => documentText(item, what, 64))
  const overlap = comparison.overlap
  if (typeof overlap !== 'number' || !Number.isFinite(overlap) || overlap < 0 || overlap > 1) throw new WireError('local_comparison.overlap')
  return {
    first: parseShape(comparison.first, 'local_comparison.first'),
    second: parseShape(comparison.second, 'local_comparison.second'),
    sharedTerms: terms(comparison.shared_terms, 'local_comparison.shared', 40),
    onlyFirst: terms(comparison.only_first, 'local_comparison.only_first', 25),
    onlySecond: terms(comparison.only_second, 'local_comparison.only_second', 25),
    overlap
  }
}

// ---- the claimed provider context ----------------------------------------------------------------

export interface DocumentProviderContext {
  disclosureId: string
  taskId: string
  recipient: AgentDisclosureRecipient
  model: string
  projection: DocumentProjection
}

/** What a committed claim releases: the purpose and the redacted excerpts under `d1`/`d2`. Nothing else. */
export function parseDocumentProviderContext(value: unknown): DocumentProviderContext {
  const context = record(value, 'document_claim')
  const projection = record(context.projection, 'document_claim.projection')
  if (projection.schema_version !== 1 || projection.classification !== 'document_private' || projection.trust !== 'untrusted_environment') {
    throw new WireError('document_claim.projection')
  }
  const allowed = new Set(['schema_version', 'classification', 'trust', 'purpose', 'documents'])
  if (Object.keys(projection).some((key) => !allowed.has(key))) throw new WireError('document_claim.projection.keys')
  const documents = list(projection.documents, 'document_claim.documents', 2).map((raw) => {
    const item = record(raw, 'document_claim.document')
    if (Object.keys(item).some((key) => !['doc_ref', 'excerpt', 'truncated'].includes(key))) throw new WireError('document_claim.document.keys')
    return {
      docRef: member(REFS, item.doc_ref, 'document_claim.doc_ref') as DocRef,
      excerpt: documentText(item.excerpt, 'document_claim.excerpt', 8_192),
      truncated: bool(item.truncated, 'document_claim.truncated')
    }
  })
  if (documents.length === 0) throw new WireError('document_claim.documents')
  return {
    disclosureId: uuid(context.disclosure_id, 'document_claim.disclosure_id'),
    taskId: uuid(context.task_id, 'document_claim.task_id'),
    recipient: recipient(context.recipient, 'document_claim.recipient'),
    model: model(context.model, 'document_claim.model'),
    projection: { purpose: text(projection.purpose, 'document_claim.purpose', 400), documents }
  }
}

// ---- errors -------------------------------------------------------------------------------------

/** Fixed, app-authored text for the closed runtime reasons worth explaining. Never a runtime message. */
const REASONS: Record<string, string> = {
  file_changed: 'That file changed after you added it, so it is no longer the approved file. Add it again.',
  file_missing: 'That file is no longer there.',
  root_revoked: 'That folder is no longer approved.',
  root_changed: 'That folder was replaced or moved, so it is no longer the approved folder.',
  root_already_approved: 'That folder is already approved.',
  root_protected: 'Lumi cannot be given access to a system, program or application-data folder, or to a whole home folder. Choose a documents folder.',
  protected_folders_unknown: 'Lumi could not confirm which folders are protected, so it approved nothing.',
  not_listable: 'Lumi only reads files its folder listing shows.',
  root_too_broad: 'That folder is too broad. Choose a documents folder.',
  permission_missing: 'That folder is not approved for reading.',
  modify_not_supported: 'Lumi cannot be given permission to modify files.',
  reparse_point: 'Lumi does not follow links or junctions.',
  outside_root: 'That file is outside the approved folder.',
  hardlinked_file: 'Lumi does not read files that have more than one name on disk.',
  file_too_large: 'That file is larger than Lumi reads (10 MB).',
  unsupported_format: 'Lumi reads PDF, Word (.docx) and text files only.',
  type_mismatch: 'That file’s contents do not match its type.',
  macro_document_refused: 'Lumi does not read documents that contain macros.',
  encrypted_pdf: 'That PDF is encrypted.',
  unsupported_pdf: 'Lumi could not find readable text in that PDF.',
  too_many_pages: 'That PDF has more pages than Lumi reads (50).',
  extraction_timeout: 'Reading that document took too long, so Lumi stopped.',
  too_many_files: 'A document task holds at most four files.',
  document_expired: 'That document’s text has expired. Add the file again.',
  document_changed: 'A document on the card changed. Nothing was sent.',
  grant_expired: 'That approval expired. Nothing was sent.',
  disclosure_already_made: 'This task already sent its one comparison.'
}

const DEFAULTS: Record<string, string> = {
  document_state_changed: 'That document is no longer available as approved. Nothing was sent.',
  document_refused: 'Lumi refused that document request. Nothing was read or sent.'
}

/** Closed codes and fixed messages only; never a runtime message, a path or a file name. */
export function projectDocumentRuntimeError(status: number, body: unknown): AgentError {
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined
  const code = typeof error?.code === 'string' && CODE.test(error.code) ? error.code : undefined
  const reason = typeof error?.reason === 'string' && CODE.test(error.reason) ? error.reason : undefined
  if (code === 'document_state_changed' || code === 'document_refused') {
    return { code, message: (reason !== undefined ? REASONS[reason] : undefined) ?? DEFAULTS[code] }
  }
  if (code === 'task_not_found') return { code: 'not_found', message: 'That document task no longer exists.' }
  if (code === 'task_not_accepting_actions') return { code: 'not_accepting_actions', message: 'That task has ended.' }
  return { code: status === 422 ? 'invalid_request' : 'request_failed', message: 'Lumi could not complete that document request.' }
}
