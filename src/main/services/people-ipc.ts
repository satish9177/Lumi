/**
 * Validating everything the renderer sends about people, and bounding
 * everything main sends back.
 *
 * This module is the boundary. Two rules hold across it, and both are enforced
 * here rather than trusted to the handlers:
 *
 *  - **Inbound is parsed, never cast.** Every payload arrives as `unknown` and
 *    leaves as a narrow type or throws. A renderer compromised by a malicious
 *    page is still only able to send what these parsers accept.
 *
 *  - **Outbound is projected, never forwarded.** No object that main holds is
 *    passed to the renderer directly. Each view is rebuilt field by field from
 *    an explicit list, so a field added to a stored type later — an embedding,
 *    a reference path, a similarity — cannot reach the renderer by inheriting a
 *    spread. `projectProfile` is the only way a profile crosses, and it names
 *    its six fields.
 *
 * ## On profile ids
 *
 * The renderer does receive opaque profile ids, because the settings UI has to
 * name which person a Rename or Delete applies to and there is no safer handle.
 * What matters is that an id is inert everywhere else:
 *
 *  - the search query contract rejects UUID-shaped strings outright, so an id
 *    offered as a `people_labels` entry fails validation rather than resolving;
 *  - Realtime never receives one, and its tool schema has no field that would
 *    accept one;
 *  - resolution runs label → profile in main only, and never id → profile from
 *    an outside caller.
 *
 * An id is therefore a handle for management operations and nothing else. See
 * people-ipc.test.ts, which asserts each of those three properties.
 */

import type {
  PeopleEnrolmentView,
  PeopleFaceCandidateView,
  PeopleProfileView
} from '../../shared/contracts'
import { MAX_LABEL_LENGTH } from '../vision/person-profiles'

/** A profile id is a UUID we generated. Nothing else is accepted as one. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Bounds a preview data URL so a fabricated one cannot be unbounded. */
const MAX_PREVIEW_DATA_URL_CHARS = 64 * 1024

/** C0 and C1 control characters. Written as escapes so the source stays plain ASCII. */
const CONTROL_CHARACTERS = /[\u0000-\u001F\u007F-\u009F]/g

export class PeopleIpcError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PeopleIpcError'
  }
}

/**
 * A label as the user typed it.
 *
 * Bounded and stripped of control characters, but *not* otherwise sanitized:
 * the label is stored and displayed as text, never interpolated into an
 * instruction, so escaping is the display layer's job. Removing control
 * characters is about the storage format, not about defusing an attack — a
 * label reading "ignore previous instructions" is a perfectly valid name for a
 * person to choose, and it is safe precisely because nothing ever executes it.
 */
export function parseLabel(value: unknown): string {
  if (typeof value !== 'string') {
    throw new PeopleIpcError('A name must be text.')
  }
  const cleaned = value
    .normalize('NFKC')
    // C0 and C1 control characters, including the newlines and tabs someone
    // would reach for to break a label out of whatever renders it.
    .replace(CONTROL_CHARACTERS, ' ')
    .trim()
    .replace(/\s+/g, ' ')
  if (cleaned.length === 0) {
    throw new PeopleIpcError('Enter a name for this person.')
  }
  if (cleaned.length > MAX_LABEL_LENGTH) {
    throw new PeopleIpcError(`Use ${MAX_LABEL_LENGTH} characters or fewer.`)
  }
  return cleaned
}

/**
 * A profile id the renderer is handing back.
 *
 * Shape-checked rather than trusted, so a caller cannot pass a path, a label,
 * or a probe string where an id belongs and have it reach a store lookup. The
 * lookup itself still returns undefined for an id that does not exist; this
 * check exists so the *shape* of the failure is uniform and so nothing
 * path-like ever reaches a function that takes ids.
 */
export function parseProfileId(value: unknown): string {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) {
    throw new PeopleIpcError('That person is no longer saved.')
  }
  return value
}

/** An enrolment draft id. Same shape and the same reasoning. */
export function parseEnrolmentId(value: unknown): string {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) {
    throw new PeopleIpcError('That setup is no longer open. Start again.')
  }
  return value
}

/** A temporary candidate id, generated per draft and never persisted. */
export function parseCandidateId(value: unknown): string {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) {
    throw new PeopleIpcError('That choice is no longer available. Pick a face again.')
  }
  return value
}

/**
 * A trusted-result or dropped-file id the renderer already legitimately holds.
 *
 * Deliberately *not* a path. The renderer never had a path to offer, and a
 * parameter here that accepted one would be a way to read an arbitrary file
 * through the enrolment pipeline. Main resolves the id against its own registry
 * of approved results; an id it does not recognise resolves to nothing.
 */
export function parseTrustedId(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 128) {
    throw new PeopleIpcError('Lumi can’t open that photo anymore.')
  }
  if (value.includes('/') || value.includes('\\') || /^[a-zA-Z]:/.test(value)) {
    // Something path-shaped arrived where an opaque id belongs.
    throw new PeopleIpcError('Lumi can’t open that photo anymore.')
  }
  return value
}

export function parseBoolean(value: unknown): boolean {
  if (typeof value !== 'boolean') {
    throw new PeopleIpcError('That setting takes yes or no.')
  }
  return value
}

// --- outbound projections ---------------------------------------------------

/**
 * Rebuilds a profile view field by field.
 *
 * Not a spread and not a pick helper: an explicit construction, so that adding
 * a field to the stored profile type can never widen what the renderer sees.
 * The compiler will complain here if the view type changes; it would say
 * nothing at all about a spread that started carrying an embedding.
 */
export function projectProfile(source: {
  id: string
  label: string
  referenceCount: number
  status: 'ready' | 'needs_rescan' | 'needs_reenrolment'
  createdAt: string
  updatedAt: string
  checked: number
  matched: number
}): PeopleProfileView {
  return {
    id: source.id,
    label: boundedText(source.label, MAX_LABEL_LENGTH),
    referenceCount: boundedCount(source.referenceCount),
    status: source.status,
    createdAt: boundedText(source.createdAt, 40),
    updatedAt: boundedText(source.updatedAt, 40),
    checked: boundedCount(source.checked),
    matched: boundedCount(source.matched)
  }
}

/**
 * A face offered for selection.
 *
 * The preview is a locally rendered crop as a data URL. It is bounded and
 * checked to be an image data URL rather than passed through: a data URL is
 * inert as an `<img src>`, but a `text/html` one would not be, and the renderer
 * should never be in a position where the difference depends on how it is used.
 */
export function projectCandidate(source: {
  candidateId: string
  previewDataUrl: string
  selectable: boolean
  note?: string
}): PeopleFaceCandidateView {
  if (!source.previewDataUrl.startsWith('data:image/')) {
    throw new PeopleIpcError('That preview could not be prepared.')
  }
  if (source.previewDataUrl.length > MAX_PREVIEW_DATA_URL_CHARS) {
    throw new PeopleIpcError('That preview could not be prepared.')
  }
  return {
    candidateId: source.candidateId,
    previewDataUrl: source.previewDataUrl,
    selectable: source.selectable,
    ...(source.note ? { note: boundedText(source.note, 200) } : {})
  }
}

export function projectEnrolment(source: {
  enrolmentId: string
  label: string
  acceptedReferences: number
  requiredReferences: number
  maximumReferences: number
  candidates?: Array<{ candidateId: string; previewDataUrl: string; selectable: boolean; note?: string }>
  lastRejection?: string
}): PeopleEnrolmentView {
  const candidates = source.candidates?.map(projectCandidate)
  return {
    enrolmentId: source.enrolmentId,
    label: boundedText(source.label, MAX_LABEL_LENGTH),
    acceptedReferences: boundedCount(source.acceptedReferences),
    requiredReferences: boundedCount(source.requiredReferences),
    maximumReferences: boundedCount(source.maximumReferences),
    readyToCreate:
      source.acceptedReferences >= source.requiredReferences && (candidates === undefined || candidates.length === 0),
    ...(candidates && candidates.length > 0 ? { candidates } : {}),
    ...(source.lastRejection ? { lastRejection: boundedText(source.lastRejection, 200) } : {})
  }
}

function boundedText(value: unknown, limit: number): string {
  return typeof value === 'string' ? value.slice(0, limit) : ''
}

function boundedCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0
}
