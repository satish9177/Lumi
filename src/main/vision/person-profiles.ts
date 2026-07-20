/**
 * The main-process store for user-labelled people.
 *
 * This file owns the only biometric data Lumi keeps: a small set of face
 * embeddings the user deliberately enrolled, under a label the user chose. Some
 * deliberate properties:
 *
 *  - **It lives in its own directory and its own file.** Not in the photo index,
 *    and emphatically not in the Telegram session store. "Delete all people
 *    data" is then a single recursive removal that provably cannot take a CLIP
 *    vector, an OCR result, or a Telegram session with it.
 *  - **The whole file is encrypted at rest** through Electron's safeStorage,
 *    which on Windows is DPAPI keyed to the user account. If encryption is
 *    unavailable the store refuses to persist anything rather than falling back
 *    to plaintext — a face embedding is not something to write in the clear
 *    because a platform API was missing.
 *  - **Nothing here is derived from the renderer.** Labels arrive as text and
 *    are normalized and bounded; embeddings arrive only from the local enrolment
 *    pipeline. There is no path by which a caller supplies a profile id, and ids
 *    are opaque so one cannot be guessed from a label.
 *  - **Embeddings are never logged.** Not at any level, not on error. The catch
 *    blocks below discard the underlying error rather than reporting it, because
 *    a JSON parse failure can quote the document it failed on.
 *
 * A reference embedding is a 128-float vector. It is not reversible into a
 * photograph, but it is still biometric data about a real person, and it is
 * treated that way throughout.
 */

import { randomUUID } from 'node:crypto'
import { mkdir, open, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { peopleDirectory, PEOPLE_PROFILE_FILE } from './model-location'
import { FACE_EMBED_DIMENSIONS, FACE_EMBED_MODEL_VERSION, PEOPLE_INDEX_VERSION } from './people-manifest'

/**
 * Structurally identical to the port the Telegram service takes, and
 * deliberately declared again rather than imported: the two stores share an
 * Electron API, and nothing else. Keeping the declarations apart means no future
 * refactor can quietly route face data through a Telegram code path.
 */
export interface SafeStoragePort {
  isEncryptionAvailable: () => boolean
  encryptString: (value: string) => Buffer
  decryptString: (value: Buffer) => string
}

/** A label the user typed. Bounded so it cannot become a payload. */
export const MAX_LABEL_LENGTH = 40
export const MIN_LABEL_LENGTH = 1

/**
 * Enrolment quality floors. Three is the documented minimum because a single
 * reference matches its own lighting and pose and little else; eight is the
 * ceiling because the gain flattens and every extra vector is more biometric
 * data retained for the same result.
 */
export const MIN_REFERENCES = 3
export const MAX_REFERENCES = 8

/** A ceiling on enrolled people, so the file cannot grow without bound. */
export const MAX_PROFILES = 20

export const PROFILE_STATUSES = ['ready', 'needs_rescan', 'needs_reenrolment'] as const
export type ProfileStatus = (typeof PROFILE_STATUSES)[number]

export const PROFILE_ERROR_CODES = [
  'label_empty',
  'label_too_long',
  'label_duplicate',
  'too_few_references',
  'too_many_references',
  'too_many_profiles',
  'unknown_profile',
  'storage_unavailable',
  'invalid_embedding'
] as const
export type ProfileErrorCode = (typeof PROFILE_ERROR_CODES)[number]

export class PersonProfileError extends Error {
  constructor(readonly code: ProfileErrorCode) {
    // The message is app-authored per code and carries no label or embedding,
    // so it is safe to surface and safe to log.
    super(PROFILE_ERROR_MESSAGES[code])
    this.name = 'PersonProfileError'
  }
}

export const PROFILE_ERROR_MESSAGES: Record<ProfileErrorCode, string> = {
  label_empty: 'Enter a name for this person.',
  label_too_long: `Use ${MAX_LABEL_LENGTH} characters or fewer.`,
  label_duplicate: 'You already have someone with that name.',
  too_few_references: `Choose at least ${MIN_REFERENCES} photos of this person.`,
  too_many_references: `Lumi keeps up to ${MAX_REFERENCES} reference photos per person.`,
  too_many_profiles: `Lumi keeps up to ${MAX_PROFILES} people.`,
  unknown_profile: 'That person is no longer saved.',
  storage_unavailable: 'This device cannot store face data securely, so Lumi did not save it.',
  invalid_embedding: 'Lumi could not read a face from that photo.'
}

/** Quality figures kept to explain a weak profile. Never shown as raw numbers. */
export interface ReferenceQuality {
  /** The detector's confidence in the face this embedding came from. */
  detectionScore: number
  /** The face's size in the source image, in pixels along its longer edge. */
  faceSizePx: number
}

export interface StoredReference {
  id: string
  /** L2-normalized, `FACE_EMBED_DIMENSIONS` wide. */
  embedding: number[]
  quality: ReferenceQuality
  addedAt: string
}

export interface StoredPersonProfile {
  id: string
  label: string
  /** Case-folded label, the key uniqueness is enforced on. */
  normalizedLabel: string
  /** The embedding model these references came from. */
  modelVersion: number
  /** The matching rules the stored per-photo outcomes were produced under. */
  indexVersion: number
  /**
   * Bumped every time the *evidence* changes — a reference added or removed —
   * and deliberately not when the label changes.
   *
   * Per-photo match records carry the revision they were computed against, so a
   * new reference invalidates exactly this profile's outcomes and nothing else:
   * not another person's, and not the CLIP, OCR or face-count fields sharing the
   * record. A rename leaves the revision alone, which is what makes "renaming
   * does not require a rescan" a structural fact rather than a promise.
   */
  revision: number
  references: StoredReference[]
  createdAt: string
  updatedAt: string
}

/**
 * What a caller outside this module is allowed to see. Note what is absent:
 * the embeddings. Nothing that reaches an IPC handler carries a vector, so the
 * renderer cannot receive one even by accident.
 */
export interface PersonProfileSummary {
  id: string
  label: string
  referenceCount: number
  status: ProfileStatus
  createdAt: string
  updatedAt: string
}

/** The starting revision for a freshly created profile. */
export const INITIAL_PROFILE_REVISION = 1

/**
 * Case-insensitive and whitespace-insensitive, so "father", "Father" and
 * " Father " are one person. Unicode-normalized first, so two visually identical
 * labels with different code points cannot both be enrolled.
 */
export function normalizeLabel(raw: string): string {
  return raw.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase('en-US')
}

/** Validates and bounds a label without deciding uniqueness. */
export function cleanLabel(raw: unknown): string {
  if (typeof raw !== 'string') {
    throw new PersonProfileError('label_empty')
  }
  const cleaned = raw.normalize('NFKC').trim().replace(/\s+/g, ' ')
  if (cleaned.length < MIN_LABEL_LENGTH) {
    throw new PersonProfileError('label_empty')
  }
  if (cleaned.length > MAX_LABEL_LENGTH) {
    throw new PersonProfileError('label_too_long')
  }
  return cleaned
}

/**
 * A vector is accepted only at the exact width the pinned model produces and
 * only if finite and non-degenerate. Anything else is refused rather than
 * stored, because a malformed reference would silently poison every comparison
 * against that profile.
 */
export function normalizeEmbedding(values: ArrayLike<number>): number[] {
  if (values.length !== FACE_EMBED_DIMENSIONS) {
    throw new PersonProfileError('invalid_embedding')
  }
  let sumOfSquares = 0
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]!
    if (!Number.isFinite(value)) {
      throw new PersonProfileError('invalid_embedding')
    }
    sumOfSquares += value * value
  }
  const norm = Math.sqrt(sumOfSquares)
  if (!(norm > 1e-6)) {
    throw new PersonProfileError('invalid_embedding')
  }
  const normalized = new Array<number>(values.length)
  for (let index = 0; index < values.length; index += 1) {
    normalized[index] = values[index]! / norm
  }
  return normalized
}

/**
 * Cosine similarity of two already-normalized vectors.
 *
 * Takes `ArrayLike` rather than `number[]` so a caller can pass a `Float32Array`
 * view straight from the worker's output. Copying 128 floats into a JS array per
 * face would be pure waste, and — more to the point — it would create a second
 * copy of a library-face embedding whose lifetime someone then has to reason
 * about. See people-scan.ts.
 */
export function cosineSimilarity(left: ArrayLike<number>, right: ArrayLike<number>): number {
  if (left.length !== right.length) {
    return 0
  }
  let total = 0
  for (let index = 0; index < left.length; index += 1) {
    total += left[index]! * right[index]!
  }
  return total
}

interface ProfileDocument {
  version: number
  profiles: StoredPersonProfile[]
}

const DOCUMENT_VERSION = 1

export interface PersonProfileStoreOptions {
  userDataDir: string
  secureStorage: SafeStoragePort
  now?: () => number
}

export class PersonProfileStore {
  private profiles: StoredPersonProfile[] = []
  private loaded = false
  /**
   * True when a stored file existed but could not be read back. Surfaced so the
   * UI can say so honestly instead of presenting "no people" as if the user had
   * never enrolled anyone.
   */
  private corrupted = false
  /** Serializes writes, so two concurrent edits cannot interleave a rename. */
  private writeChain: Promise<void> = Promise.resolve()

  constructor(private readonly options: PersonProfileStoreOptions) {}

  private now(): string {
    return new Date(this.options.now?.() ?? Date.now()).toISOString()
  }

  private filePath(): string {
    return join(peopleDirectory(this.options.userDataDir), PEOPLE_PROFILE_FILE)
  }

  /** True when this device can protect the data. Checked before every write. */
  storageAvailable(): boolean {
    try {
      return this.options.secureStorage.isEncryptionAvailable()
    } catch {
      return false
    }
  }

  recoveredFromCorruption(): boolean {
    return this.corrupted
  }

  async load(): Promise<void> {
    if (this.loaded) {
      return
    }
    this.loaded = true

    let encrypted: Buffer
    try {
      encrypted = await readFile(this.filePath())
    } catch {
      // No file yet is the ordinary first-run case, not a corruption.
      this.profiles = []
      return
    }

    try {
      const plain = this.options.secureStorage.decryptString(encrypted)
      const parsed: unknown = JSON.parse(plain)
      this.profiles = parseDocument(parsed)
    } catch {
      // Deliberately swallowed: the error text can quote the document, and the
      // document is biometric data. The file is left in place rather than
      // deleted, and is replaced by the next successful write.
      this.profiles = []
      this.corrupted = true
    }
  }

  /**
   * Marks profiles whose embeddings came from a different model. Their vectors
   * are kept — deleting the user's enrolment because we shipped a new model
   * would be its own kind of data loss — but they are excluded from matching
   * until re-enrolled, because a vector from one model means nothing to another.
   */
  statusOf(profile: StoredPersonProfile): ProfileStatus {
    if (profile.modelVersion !== FACE_EMBED_MODEL_VERSION) {
      return 'needs_reenrolment'
    }
    if (profile.indexVersion !== PEOPLE_INDEX_VERSION) {
      return 'needs_rescan'
    }
    return 'ready'
  }

  list(): PersonProfileSummary[] {
    return this.profiles.map((profile) => ({
      id: profile.id,
      label: profile.label,
      referenceCount: profile.references.length,
      status: this.statusOf(profile),
      createdAt: profile.createdAt,
      updatedAt: profile.updatedAt
    }))
  }

  /** Profiles usable for matching right now. */
  matchable(): StoredPersonProfile[] {
    return this.profiles.filter((profile) => this.statusOf(profile) !== 'needs_reenrolment')
  }

  /**
   * The only label-to-id resolution in the system, and it lives in main.
   * Returns undefined rather than creating anything: a search for a name that
   * was never enrolled must not enrol it.
   */
  resolveLabel(rawLabel: string): StoredPersonProfile | undefined {
    const normalized = normalizeLabel(rawLabel)
    if (normalized.length === 0) {
      return undefined
    }
    return this.profiles.find((profile) => profile.normalizedLabel === normalized)
  }

  byId(profileId: string): StoredPersonProfile | undefined {
    return this.profiles.find((profile) => profile.id === profileId)
  }

  async create(rawLabel: string, references: readonly StoredReference[]): Promise<PersonProfileSummary> {
    const label = cleanLabel(rawLabel)
    const normalizedLabel = normalizeLabel(label)

    if (references.length < MIN_REFERENCES) {
      throw new PersonProfileError('too_few_references')
    }
    if (references.length > MAX_REFERENCES) {
      throw new PersonProfileError('too_many_references')
    }
    if (this.profiles.length >= MAX_PROFILES) {
      throw new PersonProfileError('too_many_profiles')
    }
    if (this.profiles.some((profile) => profile.normalizedLabel === normalizedLabel)) {
      throw new PersonProfileError('label_duplicate')
    }

    const timestamp = this.now()
    const profile: StoredPersonProfile = {
      id: randomUUID(),
      label,
      normalizedLabel,
      modelVersion: FACE_EMBED_MODEL_VERSION,
      indexVersion: PEOPLE_INDEX_VERSION,
      revision: INITIAL_PROFILE_REVISION,
      references: references.map((reference) => ({
        id: reference.id,
        embedding: normalizeEmbedding(reference.embedding),
        quality: reference.quality,
        addedAt: reference.addedAt
      })),
      createdAt: timestamp,
      updatedAt: timestamp
    }

    this.profiles.push(profile)
    await this.persist()
    return this.list().find((summary) => summary.id === profile.id)!
  }

  async rename(profileId: string, rawLabel: string): Promise<PersonProfileSummary> {
    const profile = this.byId(profileId)
    if (!profile) {
      throw new PersonProfileError('unknown_profile')
    }
    const label = cleanLabel(rawLabel)
    const normalizedLabel = normalizeLabel(label)
    if (this.profiles.some((other) => other.id !== profileId && other.normalizedLabel === normalizedLabel)) {
      throw new PersonProfileError('label_duplicate')
    }

    profile.label = label
    profile.normalizedLabel = normalizedLabel
    profile.updatedAt = this.now()
    // A rename does not touch the embeddings, so stored match outcomes stay
    // valid. Only the word the user reads has changed.
    await this.persist()
    return this.list().find((summary) => summary.id === profileId)!
  }

  async addReference(profileId: string, reference: StoredReference): Promise<PersonProfileSummary> {
    const profile = this.byId(profileId)
    if (!profile) {
      throw new PersonProfileError('unknown_profile')
    }
    if (profile.references.length >= MAX_REFERENCES) {
      throw new PersonProfileError('too_many_references')
    }
    profile.references.push({
      id: reference.id,
      embedding: normalizeEmbedding(reference.embedding),
      quality: reference.quality,
      addedAt: reference.addedAt
    })
    profile.updatedAt = this.now()
    // New evidence changes what this profile will match, so previously computed
    // outcomes are no longer trustworthy. Bumping the revision is what makes
    // every stored match record for *this* profile read as "not checked".
    profile.revision += 1
    profile.indexVersion = -1
    await this.persist()
    return this.list().find((summary) => summary.id === profileId)!
  }

  async removeReference(profileId: string, referenceId: string): Promise<PersonProfileSummary> {
    const profile = this.byId(profileId)
    if (!profile) {
      throw new PersonProfileError('unknown_profile')
    }
    const remaining = profile.references.filter((reference) => reference.id !== referenceId)
    if (remaining.length < MIN_REFERENCES) {
      throw new PersonProfileError('too_few_references')
    }
    profile.references = remaining
    profile.updatedAt = this.now()
    profile.revision += 1
    profile.indexVersion = -1
    await this.persist()
    return this.list().find((summary) => summary.id === profileId)!
  }

  /** Marks a profile as freshly scanned under the current matching rules. */
  async markScanned(profileId: string): Promise<void> {
    const profile = this.byId(profileId)
    if (!profile || profile.indexVersion === PEOPLE_INDEX_VERSION) {
      return
    }
    profile.indexVersion = PEOPLE_INDEX_VERSION
    await this.persist()
  }

  /**
   * Forces a rescan of one profile without disturbing its enrolment.
   *
   * The revision moves too, so the records already on disk stop being read as
   * answers. Leaving them readable while claiming a rescan was queued would show
   * the user stale matches under a progress bar that said otherwise.
   */
  async invalidateScan(profileId: string): Promise<void> {
    const profile = this.byId(profileId)
    if (!profile) {
      throw new PersonProfileError('unknown_profile')
    }
    profile.revision += 1
    profile.indexVersion = -1
    await this.persist()
  }

  /**
   * Removes the profile and its embeddings. The caller is responsible for the
   * matching records keyed by this id; see the coordinator, which deletes those
   * in the same operation.
   */
  async remove(profileId: string): Promise<boolean> {
    const before = this.profiles.length
    this.profiles = this.profiles.filter((profile) => profile.id !== profileId)
    if (this.profiles.length === before) {
      return false
    }
    await this.persist()
    return true
  }

  /**
   * Deletes every profile and the file itself, so nothing recoverable is left
   * behind — an empty encrypted document would still be a file that once held
   * face data, and users asking for this are asking for it to be gone.
   */
  async removeAll(): Promise<void> {
    this.profiles = []
    this.corrupted = false
    await this.enqueue(async () => {
      await rm(peopleDirectory(this.options.userDataDir), { recursive: true, force: true }).catch(() => undefined)
    })
  }

  private persist(): Promise<void> {
    if (!this.storageAvailable()) {
      // Refused rather than degraded. Writing embeddings in plaintext because
      // DPAPI was unavailable would be the wrong trade to make silently.
      throw new PersonProfileError('storage_unavailable')
    }
    const document: ProfileDocument = { version: DOCUMENT_VERSION, profiles: this.profiles }
    const plain = JSON.stringify(document)
    const encrypted = this.options.secureStorage.encryptString(plain)

    return this.enqueue(async () => {
      const directory = peopleDirectory(this.options.userDataDir)
      await mkdir(directory, { recursive: true })
      const target = this.filePath()
      const temporary = `${target}.tmp`
      await writeFile(temporary, encrypted)
      await flushToDisk(temporary)
      // Atomic replace on the same volume: a reader sees either the whole old
      // document or the whole new one, never a truncated file of half-vectors.
      await rename(temporary, target)
      this.corrupted = false
    })
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.writeChain.then(operation, operation)
    // Kept un-rejected so one failed write does not poison every later one.
    this.writeChain = next.catch(() => undefined)
    return next
  }
}

/**
 * Parses a decrypted document defensively. Anything that does not match is
 * dropped rather than coerced: a half-valid profile would match unpredictably,
 * and silently matching the wrong person is the worst outcome this feature has.
 */
function parseDocument(value: unknown): StoredPersonProfile[] {
  if (typeof value !== 'object' || value === null) {
    throw new Error('unreadable')
  }
  const document = value as Partial<ProfileDocument>
  if (!Array.isArray(document.profiles)) {
    throw new Error('unreadable')
  }

  const profiles: StoredPersonProfile[] = []
  const seen = new Set<string>()

  for (const raw of document.profiles.slice(0, MAX_PROFILES)) {
    const profile = parseProfile(raw)
    // A duplicate normalized label in a hand-edited file would make label
    // resolution ambiguous; the first one wins and the rest are dropped.
    if (!profile || seen.has(profile.normalizedLabel)) {
      continue
    }
    seen.add(profile.normalizedLabel)
    profiles.push(profile)
  }

  return profiles
}

function parseProfile(value: unknown): StoredPersonProfile | undefined {
  if (typeof value !== 'object' || value === null) {
    return undefined
  }
  const raw = value as Record<string, unknown>
  if (
    typeof raw.id !== 'string' ||
    typeof raw.label !== 'string' ||
    typeof raw.normalizedLabel !== 'string' ||
    typeof raw.modelVersion !== 'number' ||
    typeof raw.indexVersion !== 'number' ||
    typeof raw.createdAt !== 'string' ||
    typeof raw.updatedAt !== 'string' ||
    !Array.isArray(raw.references)
  ) {
    return undefined
  }

  const references: StoredReference[] = []
  for (const candidate of raw.references.slice(0, MAX_REFERENCES)) {
    const reference = parseReference(candidate)
    if (reference) {
      references.push(reference)
    }
  }
  if (references.length === 0) {
    return undefined
  }

  // A document written before revisions existed reads as the initial revision.
  // That is the conservative direction: stored match records carrying no
  // revision are already dropped by the index parser, so the worst case is a
  // rescan, never a stale match presented as current.
  const revision =
    typeof raw.revision === 'number' && Number.isInteger(raw.revision) && raw.revision >= INITIAL_PROFILE_REVISION
      ? raw.revision
      : INITIAL_PROFILE_REVISION

  return {
    id: raw.id,
    label: raw.label.slice(0, MAX_LABEL_LENGTH),
    normalizedLabel: raw.normalizedLabel,
    modelVersion: raw.modelVersion,
    indexVersion: raw.indexVersion,
    revision,
    references,
    createdAt: raw.createdAt,
    updatedAt: raw.updatedAt
  }
}

function parseReference(value: unknown): StoredReference | undefined {
  if (typeof value !== 'object' || value === null) {
    return undefined
  }
  const raw = value as Record<string, unknown>
  if (typeof raw.id !== 'string' || typeof raw.addedAt !== 'string' || !Array.isArray(raw.embedding)) {
    return undefined
  }
  if (raw.embedding.length !== FACE_EMBED_DIMENSIONS) {
    return undefined
  }
  const embedding: number[] = []
  for (const entry of raw.embedding) {
    if (typeof entry !== 'number' || !Number.isFinite(entry)) {
      return undefined
    }
    embedding.push(entry)
  }

  const quality = raw.quality as Partial<ReferenceQuality> | undefined
  return {
    id: raw.id,
    embedding,
    quality: {
      detectionScore: typeof quality?.detectionScore === 'number' ? quality.detectionScore : 0,
      faceSizePx: typeof quality?.faceSizePx === 'number' ? quality.faceSizePx : 0
    },
    addedAt: raw.addedAt
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
    // A filesystem that will not fsync is still usable; the atomic rename is
    // what actually protects against a torn document.
  }
}
