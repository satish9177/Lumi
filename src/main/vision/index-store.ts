/**
 * The local photo index: an append-only metadata journal beside a flat vector
 * file, versioned as a whole by generation directories.
 *
 * Layout under %APPDATA%\lifelens\photo-index\:
 *   CURRENT           a one-line pointer naming the active generation directory
 *   gen-000001\       one generation:
 *     records.jsonl     one JSON record per line; later lines supersede earlier
 *     vectors.bin       fixed-width float32 rows; row N lives at N * 512 * 4
 *     index-meta.json   format/model/generation versions, row count
 *
 * Two independent crash-safety mechanisms:
 *
 *  - Within a generation, ordinary writes rely on ordering: the vector is
 *    appended and flushed first, then a journal line claims that row. A crash in
 *    between leaves an orphan row (reclaimed by compaction); a crash mid-line
 *    leaves a torn final line (dropped on load). Neither points a record at
 *    bytes that were never written.
 *
 *  - Compaction never rewrites a generation in place. It writes a brand-new
 *    generation directory, validates it, then flips the single CURRENT pointer
 *    with one atomic rename. A crash before the flip leaves the previous
 *    generation active and whole; a crash after leaves it fully replaced. There
 *    is no moment at which a reader can observe a mix of one generation's
 *    journal with another's vectors — the failure the old two-file rename could
 *    silently produce.
 *
 * All mutations are serialized through an internal queue, so two overlapping
 * writers can never claim the same vector row or interleave a compaction with an
 * append, regardless of what the caller promises.
 *
 * Nothing here stores an absolute path, image bytes, a thumbnail, OCR output,
 * face data, or anything derived from OpenAI.
 */

import { createHash } from 'node:crypto'
import { appendFile, mkdir, open, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { MAX_OCR_TEXT_CHARS, MAX_OCR_TOKENS, MAX_OCR_TOKEN_LENGTH } from '../../shared/ocr-text'
import { FACE_MODEL_VERSION, OCR_MODEL_VERSION } from './extras-manifest'
import { FACE_EMBED_MODEL_VERSION, PEOPLE_INDEX_VERSION } from './people-manifest'
import { MAX_PROFILES } from './person-profiles'
import { CLIP_EMBEDDING_LENGTH } from './protocol'
import {
  INDEX_JOURNAL_FILE,
  INDEX_META_FILE,
  INDEX_POINTER_FILE,
  INDEX_VECTOR_FILE,
  photoIndexDirectory
} from './model-location'

/** Bumped whenever the on-disk layout changes incompatibly. */
export const INDEX_FORMAT_VERSION = 2
export const VECTOR_ROW_BYTES = CLIP_EMBEDDING_LENGTH * 4

export const INDEX_STATUSES = ['pending', 'indexed', 'failed', 'skipped', 'deleted'] as const
export type IndexStatus = (typeof INDEX_STATUSES)[number]

/** Bounded, app-authored reasons an image was not indexed. */
export const INDEX_FAILURE_CODES = [
  'decode_failed',
  'too_large',
  'too_many_pixels',
  'unsupported_format',
  'not_a_real_file',
  'outside_approved_root',
  'file_locked',
  'inference_failed'
] as const
export type IndexFailureCode = (typeof INDEX_FAILURE_CODES)[number]

/**
 * Phase-2 signals are tracked per image and per signal, because a photo can
 * legitimately be embedded but not yet read for text, or counted for faces but
 * not yet embedded. `undefined` is a distinct and important fourth state:
 * "never looked at". It must never be reported as zero faces or as no text.
 */
export const PHASE2_STATUSES = ['pending', 'done', 'failed', 'skipped'] as const
export type Phase2Status = (typeof PHASE2_STATUSES)[number]

export const OCR_FAILURE_CODES = [
  'decode_failed',
  'unsupported_format',
  'too_many_pixels',
  'file_locked',
  'ocr_failed',
  'ocr_timeout',
  'ocr_unavailable'
] as const
export type OcrFailureCode = (typeof OCR_FAILURE_CODES)[number]

export const FACE_FAILURE_CODES = [
  'decode_failed',
  'unsupported_format',
  'too_many_pixels',
  'file_locked',
  'detection_failed',
  'face_model_unavailable'
] as const
export type FaceFailureCode = (typeof FACE_FAILURE_CODES)[number]

export const MAX_STORED_FACE_COUNT = 50

// --- Phase 3: labelled-person matching -------------------------------------

/**
 * What a stored per-(photo, profile) outcome can say.
 *
 * Only *terminal* answers are written to disk. `not_checked` is the absence of a
 * record — including a record invalidated by a model, index or profile-revision
 * change — and `checking` is in-flight coordinator state that outlives no
 * process. Persisting either would let a crash mid-scan resurrect as a claim,
 * and "we never looked" must always be recoverable from what is *missing*
 * rather than from what was written.
 *
 * Note what is not here: a tier meaning "definitely". `likely` is the ceiling,
 * matching the vocabulary in face-matching.ts, and there is nothing in this
 * schema that could carry a stronger claim even if some future caller wanted to
 * make one.
 */
export const PEOPLE_MATCH_STATUSES = [
  'not_checked',
  'checking',
  'likely',
  'possible',
  'checked_no_reliable_match',
  'failed_retryable',
  'failed_permanent',
  'profile_unavailable'
] as const
export type PeopleMatchStatus = (typeof PEOPLE_MATCH_STATUSES)[number]

/** The subset that is ever written to the journal. */
const PERSISTED_PEOPLE_MATCH_STATUSES: readonly PeopleMatchStatus[] = [
  'likely',
  'possible',
  'checked_no_reliable_match',
  'failed_retryable',
  'failed_permanent',
  'profile_unavailable'
]

export const PEOPLE_FAILURE_CODES = [
  'decode_failed',
  'unsupported_format',
  'too_many_pixels',
  'file_locked',
  'outside_approved_root',
  'detection_failed',
  'alignment_failed',
  'embedding_failed',
  'face_model_unavailable',
  'profile_store_unavailable'
] as const
export type PeopleFailureCode = (typeof PEOPLE_FAILURE_CODES)[number]

/** Failures that a later attempt could plausibly resolve. */
export const RETRYABLE_PEOPLE_FAILURES: readonly PeopleFailureCode[] = [
  'file_locked',
  'detection_failed',
  'embedding_failed',
  'face_model_unavailable',
  'profile_store_unavailable'
]

/** One profile cannot appear twice, and there are only ever MAX_PROFILES of them. */
export const MAX_PEOPLE_MATCH_RECORDS = MAX_PROFILES

/** A photo cannot record more matching faces than Phase 2 would ever count. */
export const MAX_MATCHING_FACES = MAX_STORED_FACE_COUNT

/**
 * One profile's outcome for one photo.
 *
 * Every field here is bounded and non-reversible. There is deliberately no place
 * to put a similarity value, a face box, a landmark, a crop, an embedding, a
 * reference path, or the user's label — the schema is the enforcement, not a
 * convention the writer is trusted to follow. A caller holding one of these
 * learns that a profile reached a tier, and nothing about the face that did it.
 */
export interface PeopleMatchRecord {
  /** Opaque profile id. Meaningless outside the local profile store. */
  profileId: string
  status: PeopleMatchStatus
  /** How many visible faces in this photo reached at least `possible`. */
  matchingFaces: number
  /** The profile's evidence revision at scan time; see StoredPersonProfile. */
  profileRevision: number
}

export interface PeopleVersions {
  /** The embedding model the outcomes were produced by. */
  model: number
  /** The matching rules the outcomes were produced under. */
  index: number
}

export interface PhotoIndexRecord {
  imageId: string
  rootId: string
  /** Slash-separated, relative to the approved root. Never absolute. */
  relativePath: string
  name: string
  mtimeMs: number
  sizeBytes: number
  width?: number
  height?: number
  /** Row in vectors.bin, present only when status is 'indexed'. */
  vectorRow?: number
  modelVersion: number
  status: IndexStatus
  failureCode?: IndexFailureCode
  attempts: number
  updatedAtMs: number

  // --- Phase 2: local text ------------------------------------------------
  /** Absent means this image has never been read for text. */
  ocrStatus?: Phase2Status
  /** Deterministically normalized, bounded. Untrusted local data — never sent anywhere. */
  ocrText?: string
  /** Compact searchable form of the same text. */
  ocrTokens?: string[]
  ocrVersion?: number
  ocrFailureCode?: OcrFailureCode
  ocrAttempts?: number

  // --- Phase 2: visible-face counting -------------------------------------
  /** Absent means this image has never been scanned for faces. */
  faceStatus?: Phase2Status
  /** Faces above the confident threshold. Detection only — never identity. */
  visibleFaceCount?: number
  /** Faces between the uncertain and confident thresholds. */
  uncertainFaceCount?: number
  faceVersion?: number
  faceFailureCode?: FaceFailureCode
  faceAttempts?: number

  // --- Phase 3: labelled-person matching ----------------------------------
  /** Absent means this image has never been checked against any profile. */
  peopleStatus?: Phase2Status
  /** Stamped with FACE_EMBED_MODEL_VERSION; a new model drops these rows only. */
  peopleModelVersion?: number
  /** Stamped with PEOPLE_INDEX_VERSION; new matching rules drop these rows only. */
  peopleIndexVersion?: number
  peopleFailureCode?: PeopleFailureCode
  peopleAttempts?: number
  /** One entry per profile checked. Never a vector, a box, or a label. */
  peopleMatches?: PeopleMatchRecord[]
}

/** Coverage of each Phase-2 signal, for the separate settings progress lines. */
export interface Phase2Counts {
  /** Live records eligible for Phase-2 work. */
  total: number
  ocrDone: number
  ocrFailed: number
  ocrSkipped: number
  faceDone: number
  faceFailed: number
  faceSkipped: number
}

export interface Phase2Versions {
  ocr: number
  face: number
}

interface IndexMeta {
  formatVersion: number
  modelVersion: number
  /** Cross-check against the generation directory the pointer resolves to. */
  generation: number
  rowCount: number
}

export interface IndexCounts {
  total: number
  indexed: number
  pending: number
  failed: number
  skipped: number
}

/** Rewriting is only worth the I/O once a real fraction of the file is dead. */
const COMPACTION_MIN_ROWS = 64
const COMPACTION_DEAD_FRACTION = 0.3

const GENERATION_PATTERN = /^gen-\d+$/

export function computeImageId(rootId: string, relativePath: string): string {
  return createHash('sha256')
    .update(`${rootId} ${relativePath.toLocaleLowerCase('en-US')}`)
    .digest('hex')
    .slice(0, 24)
}

/** A short, non-reversible prefix safe to put in a log line. */
export function imageIdPrefix(imageId: string): string {
  return imageId.slice(0, 8)
}

export class PhotoIndexStore {
  private readonly directory: string
  private readonly pointerPath: string

  private activeDir = ''
  private generation = 0
  private records = new Map<string, PhotoIndexRecord>()
  private rowCount = 0
  private modelVersion = 0
  /**
   * Deliberately *not* part of `index-meta.json`, and deliberately not part of
   * the whole-index validity check. A new OCR or face model must invalidate
   * only its own per-record fields; making it a generation-level version would
   * throw away every CLIP vector to gain a better text reader, which is exactly
   * the re-index this phase must avoid.
   */
  private phase2Versions: Phase2Versions = { ocr: OCR_MODEL_VERSION, face: FACE_MODEL_VERSION }
  /**
   * Held apart from both the generation version and the Phase-2 versions for the
   * same reason those were split: bumping the face-embedding model must drop
   * match records and leave every CLIP vector, OCR result and face count intact.
   */
  private peopleVersions: PeopleVersions = {
    model: FACE_EMBED_MODEL_VERSION,
    index: PEOPLE_INDEX_VERSION
  }
  private loaded = false
  /** Journal lines written since load, used to decide when to compact. */
  private appendedLines = 0
  /** Serializes every mutation so overlapping writers cannot corrupt state. */
  private mutation: Promise<unknown> = Promise.resolve()

  constructor(userDataDir: string) {
    this.directory = photoIndexDirectory(userDataDir)
    this.pointerPath = join(this.directory, INDEX_POINTER_FILE)
  }

  /**
   * Loads the active generation for a given model version. A missing pointer, an
   * unreadable or mismatched meta file, or a journal that references vectors not
   * on disk all discard the index and start a fresh generation — a stale vector
   * is worse than no vector, because it produces confidently wrong matches.
   */
  async load(
    modelVersion: number,
    phase2: Phase2Versions = { ocr: OCR_MODEL_VERSION, face: FACE_MODEL_VERSION },
    people: PeopleVersions = { model: FACE_EMBED_MODEL_VERSION, index: PEOPLE_INDEX_VERSION }
  ): Promise<{ rebuilt: boolean; droppedLines: number }> {
    this.phase2Versions = phase2
    this.peopleVersions = people
    return this.runExclusive(() => this.loadInner(modelVersion))
  }

  private async loadInner(modelVersion: number): Promise<{ rebuilt: boolean; droppedLines: number }> {
    await mkdir(this.directory, { recursive: true })

    const activeName = await this.readPointer()
    if (activeName) {
      this.activeDir = join(this.directory, activeName)
      this.generation = generationNumber(activeName) ?? this.generation
    }

    const meta = activeName ? await this.readMeta() : undefined
    const metaOk =
      meta !== undefined &&
      meta.formatVersion === INDEX_FORMAT_VERSION &&
      meta.modelVersion === modelVersion &&
      (typeof meta.generation !== 'number' || meta.generation === this.generation)

    if (!activeName || !metaOk) {
      await this.resetInner(modelVersion)
      await this.cleanupStaleGenerations()
      return { rebuilt: true, droppedLines: 0 }
    }

    this.modelVersion = modelVersion

    let journal: string
    try {
      journal = await readFile(this.journalPath(), 'utf8')
    } catch {
      // No journal yet is normal for a freshly created generation. rowCount comes
      // from the actual vector file so a later append lands at the true end.
      this.records = new Map()
      this.rowCount = Math.floor((await fileBytes(this.vectorPath())) / VECTOR_ROW_BYTES)
      this.loaded = true
      await this.cleanupStaleGenerations()
      return { rebuilt: false, droppedLines: 0 }
    }

    const records = new Map<string, PhotoIndexRecord>()
    let droppedLines = 0
    let highestRow = -1

    for (const line of journal.split('\n')) {
      if (line.trim().length === 0) {
        continue
      }
      const record = parseRecordLine(line, modelVersion, this.phase2Versions, this.peopleVersions)
      if (!record) {
        // A torn final line from an interrupted write, or a corrupt entry.
        droppedLines += 1
        continue
      }
      records.set(record.imageId, record)
      if (record.vectorRow !== undefined && record.vectorRow > highestRow) {
        highestRow = record.vectorRow
      }
    }

    const availableRows = Math.floor((await fileBytes(this.vectorPath())) / VECTOR_ROW_BYTES)
    if (highestRow >= availableRows) {
      // The journal references vectors that are not on disk. Quarantining the
      // whole generation is the only safe reading of that.
      await this.resetInner(modelVersion)
      await this.cleanupStaleGenerations()
      return { rebuilt: true, droppedLines }
    }

    this.records = records
    this.rowCount = Math.max(availableRows, 0)
    this.loaded = true
    await this.cleanupStaleGenerations()
    return { rebuilt: false, droppedLines }
  }

  isLoaded(): boolean {
    return this.loaded
  }

  /** The active generation directory. Exposed for diagnostics and tests. */
  activeDirectory(): string {
    return this.activeDir
  }

  get(imageId: string): PhotoIndexRecord | undefined {
    return this.records.get(imageId)
  }

  all(): PhotoIndexRecord[] {
    return [...this.records.values()]
  }

  /** Live, searchable records only. */
  indexed(): PhotoIndexRecord[] {
    return this.all().filter((record) => record.status === 'indexed' && record.vectorRow !== undefined)
  }

  counts(): IndexCounts {
    const counts: IndexCounts = { total: 0, indexed: 0, pending: 0, failed: 0, skipped: 0 }
    for (const record of this.records.values()) {
      if (record.status === 'deleted') {
        continue
      }
      counts.total += 1
      if (record.status === 'indexed') counts.indexed += 1
      else if (record.status === 'pending') counts.pending += 1
      else if (record.status === 'failed') counts.failed += 1
      else if (record.status === 'skipped') counts.skipped += 1
    }
    return counts
  }

  /**
   * Phase-2 coverage, reported separately from `counts()` so the existing
   * Phase-1 status contract keeps its exact shape.
   *
   * "Done" counts only records stamped with the *current* signal version, so a
   * model upgrade honestly shows coverage falling back rather than claiming
   * work that was done by a model no longer in use.
   */
  phase2Counts(): Phase2Counts {
    const counts: Phase2Counts = {
      total: 0,
      ocrDone: 0,
      ocrFailed: 0,
      ocrSkipped: 0,
      faceDone: 0,
      faceFailed: 0,
      faceSkipped: 0
    }
    for (const record of this.records.values()) {
      if (record.status === 'deleted') {
        continue
      }
      counts.total += 1
      if (record.ocrStatus === 'done') counts.ocrDone += 1
      else if (record.ocrStatus === 'failed') counts.ocrFailed += 1
      else if (record.ocrStatus === 'skipped') counts.ocrSkipped += 1

      if (record.faceStatus === 'done') counts.faceDone += 1
      else if (record.faceStatus === 'failed') counts.faceFailed += 1
      else if (record.faceStatus === 'skipped') counts.faceSkipped += 1
    }
    return counts
  }

  /**
   * Merges an OCR outcome into an existing record.
   *
   * A merge rather than a `put` on purpose: a Phase-2 writer must not be able to
   * clear the vector row, the status, or the other Phase-2 signal by
   * round-tripping a stale copy of the record. If the record has since been
   * deleted or re-scanned, the result is dropped rather than resurrecting it.
   */
  async recordOcr(
    imageId: string,
    outcome: {
      status: Phase2Status
      text?: string
      tokens?: readonly string[]
      failureCode?: OcrFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    return this.runExclusive(() => this.recordOcrInner(imageId, outcome, nowMs))
  }

  private async recordOcrInner(
    imageId: string,
    outcome: {
      status: Phase2Status
      text?: string
      tokens?: readonly string[]
      failureCode?: OcrFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    const existing = this.records.get(imageId)
    if (!existing || existing.status === 'deleted') {
      return
    }

    const stored: PhotoIndexRecord = {
      ...existing,
      ocrStatus: outcome.status,
      ocrVersion: this.phase2Versions.ocr,
      ocrAttempts: outcome.attempts ?? (existing.ocrAttempts ?? 0) + 1,
      ocrText: outcome.status === 'done' ? boundedOcrText(outcome.text) : undefined,
      ocrTokens: outcome.status === 'done' ? boundedOcrTokens(outcome.tokens) : undefined,
      ocrFailureCode: outcome.failureCode,
      updatedAtMs: nowMs
    }
    this.records.set(imageId, stored)
    await this.appendLine(stored)
  }

  /** As `recordOcr`, for the visible-face count. Stores counts, never geometry. */
  async recordFaces(
    imageId: string,
    outcome: {
      status: Phase2Status
      visibleFaceCount?: number
      uncertainFaceCount?: number
      failureCode?: FaceFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    return this.runExclusive(() => this.recordFacesInner(imageId, outcome, nowMs))
  }

  private async recordFacesInner(
    imageId: string,
    outcome: {
      status: Phase2Status
      visibleFaceCount?: number
      uncertainFaceCount?: number
      failureCode?: FaceFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    const existing = this.records.get(imageId)
    if (!existing || existing.status === 'deleted') {
      return
    }

    const stored: PhotoIndexRecord = {
      ...existing,
      faceStatus: outcome.status,
      faceVersion: this.phase2Versions.face,
      faceAttempts: outcome.attempts ?? (existing.faceAttempts ?? 0) + 1,
      visibleFaceCount: outcome.status === 'done' ? boundedFaceCount(outcome.visibleFaceCount) : undefined,
      uncertainFaceCount: outcome.status === 'done' ? boundedFaceCount(outcome.uncertainFaceCount) : undefined,
      faceFailureCode: outcome.failureCode,
      updatedAtMs: nowMs
    }
    this.records.set(imageId, stored)
    await this.appendLine(stored)
  }

  /**
   * Merges a labelled-person scan outcome into an existing record.
   *
   * A merge, like the Phase-2 writers, so a Phase-3 result cannot round-trip a
   * stale copy of the record and clear the vector row, the OCR text, or the face
   * count. The matches supplied replace the previous set wholesale: a rescan
   * that no longer finds a profile must not leave that profile's old answer
   * sitting beside the new ones.
   */
  async recordPeople(
    imageId: string,
    outcome: {
      status: Phase2Status
      matches?: readonly PeopleMatchRecord[]
      failureCode?: PeopleFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    return this.runExclusive(() => this.recordPeopleInner(imageId, outcome, nowMs))
  }

  private async recordPeopleInner(
    imageId: string,
    outcome: {
      status: Phase2Status
      matches?: readonly PeopleMatchRecord[]
      failureCode?: PeopleFailureCode
      attempts?: number
    },
    nowMs: number
  ): Promise<void> {
    const existing = this.records.get(imageId)
    if (!existing || existing.status === 'deleted') {
      return
    }

    const stored: PhotoIndexRecord = {
      ...existing,
      peopleStatus: outcome.status,
      peopleModelVersion: this.peopleVersions.model,
      peopleIndexVersion: this.peopleVersions.index,
      peopleAttempts: outcome.attempts ?? (existing.peopleAttempts ?? 0) + 1,
      peopleMatches: outcome.status === 'done' ? boundedPeopleMatches(outcome.matches) : undefined,
      peopleFailureCode: outcome.failureCode,
      updatedAtMs: nowMs
    }
    this.records.set(imageId, stored)
    await this.appendLine(stored)
  }

  /**
   * Removes one profile's outcomes from every photo.
   *
   * Compacts rather than appending a tombstone line per photo. That is not an
   * optimization: an append-only journal would keep the deleted profile's rows
   * readable in superseded lines until some later compaction happened to run,
   * and "delete this person" has to mean the bytes are gone. Rewriting the
   * generation and flipping the pointer leaves no line that ever named them.
   */
  async removeProfileRecords(profileId: string, nowMs: number): Promise<number> {
    return this.runExclusive(() => this.removeProfileRecordsInner(profileId, nowMs))
  }

  private async removeProfileRecordsInner(profileId: string, nowMs: number): Promise<number> {
    let touched = 0
    for (const [imageId, record] of this.records) {
      const matches = record.peopleMatches
      if (!matches?.some((match) => match.profileId === profileId)) {
        continue
      }
      const remaining = matches.filter((match) => match.profileId !== profileId)
      this.records.set(imageId, {
        ...record,
        peopleMatches: remaining.length > 0 ? remaining : undefined,
        updatedAtMs: nowMs
      })
      touched += 1
    }
    if (touched > 0) {
      await this.compactInner()
    }
    return touched
  }

  /**
   * Strips every Phase-3 field from every photo, for "delete all people data".
   *
   * Phase-1 and Phase-2 fields are untouched by construction: this rewrites the
   * records it already holds, dropping named fields, rather than resetting the
   * index. A user removing their face data does not expect to lose the photo
   * search that took an hour to build.
   */
  async clearPeopleRecords(nowMs: number): Promise<number> {
    return this.runExclusive(() => this.clearPeopleRecordsInner(nowMs))
  }

  private async clearPeopleRecordsInner(nowMs: number): Promise<number> {
    let touched = 0
    for (const [imageId, record] of this.records) {
      if (record.peopleStatus === undefined && record.peopleMatches === undefined) {
        continue
      }
      const stripped: PhotoIndexRecord = { ...record, updatedAtMs: nowMs }
      delete stripped.peopleStatus
      delete stripped.peopleModelVersion
      delete stripped.peopleIndexVersion
      delete stripped.peopleFailureCode
      delete stripped.peopleAttempts
      delete stripped.peopleMatches
      this.records.set(imageId, stripped)
      touched += 1
    }
    if (touched > 0) {
      await this.compactInner()
    }
    return touched
  }

  /**
   * Appends one vector and returns its row. Serialized, so two overlapping calls
   * receive distinct rows. Flushed before the caller writes a record that claims
   * the row, so a record can never outlive its vector.
   */
  async appendVector(vector: Float32Array): Promise<number> {
    return this.runExclusive(() => this.appendVectorInner(vector))
  }

  private async appendVectorInner(vector: Float32Array): Promise<number> {
    if (vector.length !== CLIP_EMBEDDING_LENGTH) {
      throw new Error('A stored vector must match the model embedding width.')
    }

    const row = this.rowCount
    const handle = await open(this.vectorPath(), 'a')
    try {
      await handle.write(Buffer.from(vector.buffer, vector.byteOffset, vector.byteLength))
      await handle.sync().catch(() => undefined)
    } finally {
      await handle.close()
    }

    this.rowCount = row + 1
    return row
  }

  async readVector(row: number): Promise<Float32Array | undefined> {
    if (!Number.isInteger(row) || row < 0 || row >= this.rowCount) {
      return undefined
    }
    const vectorPath = this.vectorPath()

    try {
      const handle = await open(vectorPath, 'r')
      try {
        const buffer = Buffer.alloc(VECTOR_ROW_BYTES)
        const { bytesRead } = await handle.read(buffer, 0, VECTOR_ROW_BYTES, row * VECTOR_ROW_BYTES)
        if (bytesRead !== VECTOR_ROW_BYTES) {
          return undefined
        }
        return new Float32Array(
          buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + VECTOR_ROW_BYTES)
        )
      } finally {
        await handle.close()
      }
    } catch {
      return undefined
    }
  }

  /** Loads every live vector once, for a full search pass. */
  async readAllVectors(): Promise<Map<string, Float32Array>> {
    // Snapshot the record set and the directory together, before any await, so a
    // compaction that activates mid-read cannot pair new rows with the old file.
    const live = this.indexed()
    const vectorPath = this.vectorPath()
    const vectors = new Map<string, Float32Array>()
    if (live.length === 0) {
      return vectors
    }

    try {
      const handle = await open(vectorPath, 'r')
      try {
        const buffer = Buffer.alloc(VECTOR_ROW_BYTES)
        for (const record of live) {
          const { bytesRead } = await handle.read(buffer, 0, VECTOR_ROW_BYTES, record.vectorRow! * VECTOR_ROW_BYTES)
          if (bytesRead !== VECTOR_ROW_BYTES) {
            continue
          }
          vectors.set(
            record.imageId,
            new Float32Array(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + VECTOR_ROW_BYTES))
          )
        }
      } finally {
        await handle.close()
      }
    } catch {
      return new Map()
    }

    return vectors
  }

  async put(record: PhotoIndexRecord): Promise<void> {
    return this.runExclusive(() => this.putInner(record))
  }

  private async putInner(record: PhotoIndexRecord): Promise<void> {
    // Reject an unsafe path at write time, not only on the next load, so a buggy
    // caller cannot persist a record that would later be silently dropped.
    assertSafeRelativePath(record.relativePath)
    const normalized: PhotoIndexRecord = { ...record, modelVersion: this.modelVersion }
    this.records.set(normalized.imageId, normalized)
    await this.appendLine(normalized)
  }

  async markDeleted(imageId: string, nowMs: number): Promise<void> {
    return this.runExclusive(() => this.markDeletedInner(imageId, nowMs))
  }

  private async markDeletedInner(imageId: string, nowMs: number): Promise<void> {
    const existing = this.records.get(imageId)
    if (!existing || existing.status === 'deleted') {
      return
    }
    // A tombstone rather than a removal: the journal is append-only, and the
    // dead vector row is reclaimed at the next compaction.
    await this.putInner({ ...existing, status: 'deleted', vectorRow: undefined, updatedAtMs: nowMs })
  }

  /** Revoking a folder must take its records out of every future search. */
  async purgeRoot(rootId: string, nowMs: number): Promise<number> {
    return this.runExclusive(() => this.purgeRootInner(rootId, nowMs))
  }

  private async purgeRootInner(rootId: string, nowMs: number): Promise<number> {
    const victims = this.all().filter((record) => record.rootId === rootId && record.status !== 'deleted')
    for (const record of victims) {
      await this.markDeletedInner(record.imageId, nowMs)
    }
    if (victims.length > 0) {
      await this.compactInner()
    }
    return victims.length
  }

  /** Keeps only the roots given; anything else is purged. */
  async retainRoots(rootIds: readonly string[], nowMs: number): Promise<number> {
    return this.runExclusive(() => this.retainRootsInner(rootIds, nowMs))
  }

  private async retainRootsInner(rootIds: readonly string[], nowMs: number): Promise<number> {
    const keep = new Set(rootIds)
    const stale = [...new Set(this.all().map((record) => record.rootId))].filter((rootId) => !keep.has(rootId))
    let removed = 0
    for (const rootId of stale) {
      removed += await this.purgeRootInner(rootId, nowMs)
    }
    return removed
  }

  shouldCompact(): boolean {
    const liveRows = this.indexed().length
    const deadRows = this.rowCount - liveRows
    return this.rowCount >= COMPACTION_MIN_ROWS && deadRows >= this.rowCount * COMPACTION_DEAD_FRACTION
  }

  async compact(): Promise<void> {
    return this.runExclusive(() => this.compactInner())
  }

  /**
   * Rewrites a fresh generation directory holding only live records with
   * renumbered vector rows, then flips the CURRENT pointer atomically. In-memory
   * state is updated only after the flip succeeds, so a failed rename leaves the
   * active store, rowCount, and previous generation exactly as they were.
   */
  private async compactInner(): Promise<void> {
    if (!this.loaded) {
      return
    }

    const live = this.all().filter((record) => record.status !== 'deleted')
    const nextGeneration = this.generation + 1
    const newDir = this.generationDir(nextGeneration)
    const newVectorPath = join(newDir, INDEX_VECTOR_FILE)
    const newJournalPath = join(newDir, INDEX_JOURNAL_FILE)
    const newMetaPath = join(newDir, INDEX_META_FILE)

    // A directory left by a previously interrupted attempt is not trustworthy.
    await rm(newDir, { recursive: true, force: true }).catch(() => undefined)
    await mkdir(newDir, { recursive: true })

    const rewritten: PhotoIndexRecord[] = []
    let nextRow = 0
    const source = await open(this.vectorPath(), 'r').catch(() => undefined)
    const sink = await open(newVectorPath, 'w')
    try {
      const buffer = Buffer.alloc(VECTOR_ROW_BYTES)
      for (const record of live) {
        const hasVector = record.status === 'indexed' && record.vectorRow !== undefined
        if (!hasVector) {
          rewritten.push({ ...record, vectorRow: undefined })
          continue
        }
        let bytesRead = 0
        if (source) {
          ;({ bytesRead } = await source.read(buffer, 0, VECTOR_ROW_BYTES, record.vectorRow! * VECTOR_ROW_BYTES))
        }
        if (bytesRead !== VECTOR_ROW_BYTES) {
          // The vector is gone; demote rather than keep a dangling row.
          rewritten.push({ ...record, status: 'pending', vectorRow: undefined })
          continue
        }
        await sink.write(buffer.subarray(0, VECTOR_ROW_BYTES))
        rewritten.push({ ...record, vectorRow: nextRow })
        nextRow += 1
      }
      await sink.sync().catch(() => undefined)
    } finally {
      await sink.close()
      await source?.close()
    }

    await writeFile(newJournalPath, rewritten.map((record) => `${JSON.stringify(record)}\n`).join(''), 'utf8')
    await this.writeMetaTo(newMetaPath, nextGeneration, nextRow)

    // Validate the freshly written generation before anything can point at it.
    const writtenBytes = await fileBytes(newVectorPath)
    if (writtenBytes !== nextRow * VECTOR_ROW_BYTES) {
      await rm(newDir, { recursive: true, force: true }).catch(() => undefined)
      throw new Error('The compacted vector file failed its size check.')
    }

    const previousDir = this.activeDir
    // Atomic activation. Everything above is discardable until this rename lands.
    await this.activateGeneration(nextGeneration)

    // Synchronous swap: a read that snapshotted before this tick stays internally
    // consistent, and a read after it sees the whole new generation.
    this.activeDir = newDir
    this.generation = nextGeneration
    this.records = new Map(rewritten.map((record) => [record.imageId, record]))
    this.rowCount = nextRow
    this.appendedLines = 0

    if (previousDir && previousDir !== newDir) {
      // Best effort: a still-open reader keeps the old bytes until the next
      // startup, which is harmless — nothing points at them as the new generation.
      await rm(previousDir, { recursive: true, force: true }).catch(() => undefined)
    }
  }

  /** Drops everything. Used by "Clear and rebuild" and by version invalidation. */
  async reset(modelVersion: number): Promise<void> {
    return this.runExclusive(() => this.resetInner(modelVersion))
  }

  private async resetInner(modelVersion: number): Promise<void> {
    const nextGeneration = this.generation + 1
    const newDir = this.generationDir(nextGeneration)
    await rm(newDir, { recursive: true, force: true }).catch(() => undefined)
    await mkdir(newDir, { recursive: true })

    this.modelVersion = modelVersion
    await this.writeMetaTo(join(newDir, INDEX_META_FILE), nextGeneration, 0)

    const previousDir = this.activeDir
    await this.activateGeneration(nextGeneration)

    this.activeDir = newDir
    this.generation = nextGeneration
    this.records = new Map()
    this.rowCount = 0
    this.appendedLines = 0
    this.loaded = true

    if (previousDir && previousDir !== newDir) {
      await rm(previousDir, { recursive: true, force: true }).catch(() => undefined)
    }
  }

  private async appendLine(record: PhotoIndexRecord): Promise<void> {
    await appendFile(this.journalPath(), `${JSON.stringify(record)}\n`, 'utf8')
    this.appendedLines += 1
    if (this.appendedLines % 50 === 0) {
      await this.writeMeta()
    }
  }

  /** Called at pause and shutdown so the row count on disk is current. */
  async flush(): Promise<void> {
    return this.runExclusive(async () => {
      if (this.loaded) {
        await this.writeMeta()
      }
    })
  }

  // --- serialization -------------------------------------------------------

  /**
   * Runs one mutation at a time. The next runs only after the previous settles,
   * success or failure, so a rejection releases the lock. Public mutators call
   * this; the *Inner methods they delegate to never re-acquire it, so a
   * high-level operation invoking another (purge → compact) cannot deadlock.
   */
  private runExclusive<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.mutation.then(operation, operation)
    this.mutation = result.then(
      () => undefined,
      () => undefined
    )
    return result
  }

  // --- generation plumbing -------------------------------------------------

  private journalPath(): string {
    return join(this.activeDir, INDEX_JOURNAL_FILE)
  }

  private vectorPath(): string {
    return join(this.activeDir, INDEX_VECTOR_FILE)
  }

  private metaPath(): string {
    return join(this.activeDir, INDEX_META_FILE)
  }

  private generationDir(generation: number): string {
    return join(this.directory, generationName(generation))
  }

  private async readPointer(): Promise<string | undefined> {
    try {
      const raw = (await readFile(this.pointerPath, 'utf8')).trim()
      return GENERATION_PATTERN.test(raw) ? raw : undefined
    } catch {
      return undefined
    }
  }

  private async activateGeneration(generation: number): Promise<void> {
    const temporaryPath = `${this.pointerPath}.tmp`
    await writeFile(temporaryPath, generationName(generation), 'utf8')
    await flushToDisk(temporaryPath)
    await rename(temporaryPath, this.pointerPath)
  }

  private async cleanupStaleGenerations(): Promise<void> {
    const activeName = generationName(this.generation)
    try {
      const entries = await readdir(this.directory, { withFileTypes: true })
      for (const entry of entries) {
        if (entry.isDirectory() && GENERATION_PATTERN.test(entry.name) && entry.name !== activeName) {
          await rm(join(this.directory, entry.name), { recursive: true, force: true }).catch(() => undefined)
        }
      }
    } catch {
      // Best effort; a leftover directory is inert until it is cleaned.
    }
  }

  private async readMeta(): Promise<IndexMeta | undefined> {
    try {
      const parsed: unknown = JSON.parse(await readFile(this.metaPath(), 'utf8'))
      if (
        typeof parsed !== 'object' ||
        parsed === null ||
        typeof (parsed as IndexMeta).formatVersion !== 'number' ||
        typeof (parsed as IndexMeta).modelVersion !== 'number'
      ) {
        return undefined
      }
      return parsed as IndexMeta
    } catch {
      return undefined
    }
  }

  private async writeMeta(): Promise<void> {
    await this.writeMetaTo(this.metaPath(), this.generation, this.rowCount)
  }

  private async writeMetaTo(metaPath: string, generation: number, rowCount: number): Promise<void> {
    const meta: IndexMeta = {
      formatVersion: INDEX_FORMAT_VERSION,
      modelVersion: this.modelVersion,
      generation,
      rowCount
    }
    const temporaryPath = `${metaPath}.tmp`
    await writeFile(temporaryPath, JSON.stringify(meta), 'utf8')
    await flushToDisk(temporaryPath)
    await rename(temporaryPath, metaPath)
  }
}

const GENERATION_PREFIX = 'gen-'

function generationName(generation: number): string {
  return `${GENERATION_PREFIX}${String(generation).padStart(6, '0')}`
}

function generationNumber(name: string): number | undefined {
  const match = /^gen-(\d+)$/.exec(name)
  return match ? Number(match[1]) : undefined
}

async function fileBytes(path: string): Promise<number> {
  try {
    const details = await stat(path)
    return details.isFile() ? details.size : 0
  } catch {
    return 0
  }
}

async function flushToDisk(path: string): Promise<void> {
  try {
    const handle = await open(path, 'r+')
    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  } catch {
    // A filesystem that will not fsync is still usable; the validation on the
    // next load is what actually protects us.
  }
}

/**
 * Relative paths only: anything drive-qualified, rooted, backslash-bearing, or
 * climbing out is a corrupt or hostile entry that must never gain filesystem
 * authority.
 */
function isSafeRelativePath(relativePath: string): boolean {
  return !(
    relativePath.length === 0 ||
    relativePath.length > 1_024 ||
    /^[a-zA-Z]:/.test(relativePath) ||
    relativePath.startsWith('/') ||
    relativePath.startsWith('\\') ||
    relativePath.includes('\\') ||
    relativePath.split('/').some((segment) => segment === '..')
  )
}

/**
 * Every bound is applied on the way in *and* on the way out (see the parser),
 * so a journal that was hand-edited or written by a future build still cannot
 * load an unbounded string into memory.
 */
function boundedOcrText(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined
  }
  const trimmed = value.slice(0, MAX_OCR_TEXT_CHARS)
  return trimmed.length > 0 ? trimmed : undefined
}

function boundedOcrTokens(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined
  }
  const tokens: string[] = []
  for (const candidate of value) {
    if (typeof candidate !== 'string' || candidate.length === 0) {
      continue
    }
    tokens.push(candidate.slice(0, MAX_OCR_TOKEN_LENGTH))
    if (tokens.length >= MAX_OCR_TOKENS) {
      break
    }
  }
  return tokens.length > 0 ? tokens : undefined
}

function boundedFaceCount(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    return undefined
  }
  return Math.min(value, MAX_STORED_FACE_COUNT)
}

function assertSafeRelativePath(relativePath: string): void {
  if (typeof relativePath !== 'string' || !isSafeRelativePath(relativePath)) {
    throw new Error('A photo index record must carry a safe root-relative path.')
  }
}

/**
 * Closed-schema parse. A line that does not match exactly is dropped rather
 * than coerced, so a hand-edited or corrupt journal cannot inject a record with
 * an absolute path or an out-of-range row.
 */
function parseRecordLine(
  line: string,
  modelVersion: number,
  phase2: Phase2Versions,
  people: PeopleVersions
): PhotoIndexRecord | undefined {
  let parsed: unknown
  try {
    parsed = JSON.parse(line)
  } catch {
    return undefined
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return undefined
  }
  const value = parsed as Record<string, unknown>

  if (
    typeof value.imageId !== 'string' ||
    typeof value.rootId !== 'string' ||
    typeof value.relativePath !== 'string' ||
    typeof value.name !== 'string' ||
    typeof value.mtimeMs !== 'number' ||
    typeof value.sizeBytes !== 'number' ||
    typeof value.attempts !== 'number' ||
    typeof value.updatedAtMs !== 'number' ||
    typeof value.modelVersion !== 'number' ||
    !(INDEX_STATUSES as readonly unknown[]).includes(value.status)
  ) {
    return undefined
  }

  // A record written under a different embedding space is not comparable.
  if (value.modelVersion !== modelVersion) {
    return undefined
  }

  const relativePath = value.relativePath
  if (!isSafeRelativePath(relativePath)) {
    return undefined
  }

  const vectorRow = value.vectorRow
  if (vectorRow !== undefined && (!Number.isInteger(vectorRow) || (vectorRow as number) < 0)) {
    return undefined
  }

  const failureCode = value.failureCode
  if (failureCode !== undefined && !(INDEX_FAILURE_CODES as readonly unknown[]).includes(failureCode)) {
    return undefined
  }

  return {
    imageId: value.imageId,
    rootId: value.rootId,
    relativePath,
    name: value.name,
    mtimeMs: value.mtimeMs,
    sizeBytes: value.sizeBytes,
    width: typeof value.width === 'number' ? value.width : undefined,
    height: typeof value.height === 'number' ? value.height : undefined,
    vectorRow: vectorRow as number | undefined,
    modelVersion,
    status: value.status as IndexStatus,
    failureCode: failureCode as IndexFailureCode | undefined,
    attempts: value.attempts,
    updatedAtMs: value.updatedAtMs,
    ...parseOcrFields(value, phase2.ocr),
    ...parseFaceFields(value, phase2.face),
    ...parsePeopleFields(value, people)
  }
}

/**
 * Phase-2 fields are parsed independently of the record, and independently of
 * each other. Three properties matter here:
 *
 *  - A Phase-1 journal has none of these fields. It loads cleanly, every image
 *    reading as "never checked", and keeps its vector. That is what lets an
 *    existing index upgrade in place instead of being rebuilt.
 *  - A result written by a superseded OCR or face model is dropped back to
 *    "never checked" rather than trusted, but the record and its vector row
 *    survive untouched.
 *  - A malformed or out-of-range field is dropped rather than coerced, so a
 *    corrupt journal cannot inject an unbounded string or a negative count.
 */
function parseOcrFields(value: Record<string, unknown>, ocrVersion: number): Partial<PhotoIndexRecord> {
  if (
    !(PHASE2_STATUSES as readonly unknown[]).includes(value.ocrStatus) ||
    value.ocrVersion !== ocrVersion
  ) {
    return {}
  }
  const failureCode = value.ocrFailureCode
  return {
    ocrStatus: value.ocrStatus as Phase2Status,
    ocrVersion,
    ocrText: boundedOcrText(value.ocrText),
    ocrTokens: boundedOcrTokens(value.ocrTokens),
    ocrFailureCode: (OCR_FAILURE_CODES as readonly unknown[]).includes(failureCode)
      ? (failureCode as OcrFailureCode)
      : undefined,
    ocrAttempts: typeof value.ocrAttempts === 'number' && value.ocrAttempts >= 0 ? value.ocrAttempts : 0
  }
}

/**
 * Phase-3 fields, parsed independently of Phase 1 and Phase 2 and gated on
 * *both* people versions.
 *
 * A mismatch on either drops the outcomes back to "never checked" and leaves the
 * vector, the OCR text and the face count exactly where they were. That is the
 * whole point of keeping these versions out of `index-meta.json`: shipping a new
 * face-embedding model must cost a face rescan, not a re-embed of the library.
 *
 * A record whose fields survive but whose profile has since gained a reference
 * is *not* filtered here — the store does not know about profiles. It is caught
 * at read time by comparing `profileRevision`, which is why that field is stored
 * rather than inferred.
 */
function parsePeopleFields(
  value: Record<string, unknown>,
  people: PeopleVersions
): Partial<PhotoIndexRecord> {
  if (
    !(PHASE2_STATUSES as readonly unknown[]).includes(value.peopleStatus) ||
    value.peopleModelVersion !== people.model ||
    value.peopleIndexVersion !== people.index
  ) {
    return {}
  }
  const failureCode = value.peopleFailureCode
  return {
    peopleStatus: value.peopleStatus as Phase2Status,
    peopleModelVersion: people.model,
    peopleIndexVersion: people.index,
    peopleMatches: boundedPeopleMatches(value.peopleMatches),
    peopleFailureCode: (PEOPLE_FAILURE_CODES as readonly unknown[]).includes(failureCode)
      ? (failureCode as PeopleFailureCode)
      : undefined,
    peopleAttempts: typeof value.peopleAttempts === 'number' && value.peopleAttempts >= 0 ? value.peopleAttempts : 0
  }
}

/**
 * Applied on the way in and on the way out, like the OCR bounds above, so a
 * hand-edited or future-written journal cannot load an unbounded array or a
 * status this build does not implement.
 *
 * Duplicate profile ids are dropped rather than merged: two disagreeing answers
 * for one person is a corrupt record, and picking one of them would be guessing.
 */
function boundedPeopleMatches(value: unknown): PeopleMatchRecord[] | undefined {
  if (!Array.isArray(value)) {
    return undefined
  }
  const matches: PeopleMatchRecord[] = []
  const seen = new Set<string>()

  for (const candidate of value) {
    if (typeof candidate !== 'object' || candidate === null) {
      continue
    }
    const raw = candidate as Record<string, unknown>
    if (
      typeof raw.profileId !== 'string' ||
      raw.profileId.length === 0 ||
      raw.profileId.length > 64 ||
      !PERSISTED_PEOPLE_MATCH_STATUSES.includes(raw.status as PeopleMatchStatus) ||
      typeof raw.matchingFaces !== 'number' ||
      !Number.isInteger(raw.matchingFaces) ||
      raw.matchingFaces < 0 ||
      typeof raw.profileRevision !== 'number' ||
      !Number.isInteger(raw.profileRevision) ||
      raw.profileRevision < 0
    ) {
      continue
    }
    if (seen.has(raw.profileId)) {
      continue
    }
    seen.add(raw.profileId)
    matches.push({
      profileId: raw.profileId,
      status: raw.status as PeopleMatchStatus,
      matchingFaces: Math.min(raw.matchingFaces, MAX_MATCHING_FACES),
      profileRevision: raw.profileRevision
    })
    if (matches.length >= MAX_PEOPLE_MATCH_RECORDS) {
      break
    }
  }

  return matches.length > 0 ? matches : undefined
}

function parseFaceFields(value: Record<string, unknown>, faceVersion: number): Partial<PhotoIndexRecord> {
  if (
    !(PHASE2_STATUSES as readonly unknown[]).includes(value.faceStatus) ||
    value.faceVersion !== faceVersion
  ) {
    return {}
  }
  const failureCode = value.faceFailureCode
  return {
    faceStatus: value.faceStatus as Phase2Status,
    faceVersion,
    visibleFaceCount: boundedFaceCount(value.visibleFaceCount),
    uncertainFaceCount: boundedFaceCount(value.uncertainFaceCount),
    faceFailureCode: (FACE_FAILURE_CODES as readonly unknown[]).includes(failureCode)
      ? (failureCode as FaceFailureCode)
      : undefined,
    faceAttempts: typeof value.faceAttempts === 'number' && value.faceAttempts >= 0 ? value.faceAttempts : 0
  }
}
