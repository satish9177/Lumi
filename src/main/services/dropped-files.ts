import { randomUUID } from 'node:crypto'
import { lstat, realpath } from 'node:fs/promises'
import { basename, extname } from 'node:path'
import type { AttachmentMediaKind, DroppedFileDescriptor } from '../../shared/contracts'
import {
  isTelegramSafeDimensions,
  readAttachmentPrefix,
  sniffAttachmentType,
  attachmentTypeLabel,
  MAX_ATTACHMENT_BYTES,
  MAX_PHOTO_BYTES,
  MAX_TEXT_BYTES,
  type AttachmentType,
  type ImageDimensionsProbe
} from './attachment-validation'

/**
 * Holds the one file the user has handed Lumi by dropping it.
 *
 * A drop is an explicit gesture that creates a temporary, main-owned trusted
 * item and causes nothing else — no upload, no analysis, no send, no open. The
 * absolute path never leaves main; the renderer sees an opaque identifier and
 * safe metadata only.
 *
 * A dropped file is deliberately *not* placed in the approved-root store:
 * dropping one file must never widen the folders Lumi may search, and reusing
 * the search-result store would let the next search silently evict it.
 */

/**
 * How long a dropped record lives, measured from registration and never
 * extended. A fixed expiry is chosen over an idle one so that rendering a
 * confirmation card — which revalidates, and so would touch an idle timer —
 * cannot silently prolong the user's temporary grant.
 */
export const DROPPED_FILE_TTL_MS = 30 * 60 * 1000

/** Shortcuts are rejected by extension before anything dereferences them. */
const SHORTCUT_EXTENSIONS = new Set(['.lnk', '.url'])

export interface DroppedFileSnapshot {
  readonly droppedId: string
  readonly canonicalPath: string
  readonly fileName: string
  readonly sizeBytes: number
  readonly mtimeMs: number
  readonly sniffedType: AttachmentType
  readonly mediaKind: AttachmentMediaKind
  readonly fileTypeLabel: string
}

export class DroppedFileError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DroppedFileError'
  }
}

/**
 * Validates a dropped path and freezes what was checked.
 *
 * Ordering matters: shortcuts are refused by extension first, then `lstat`
 * rejects links and directories without following them, then `realpath`
 * canonicalises, then the canonical path is `lstat`-ed again to narrow the
 * time-of-check/time-of-use gap — the pattern attachment validation already
 * uses.
 */
export async function validateDroppedFile(
  path: string,
  probeImage?: ImageDimensionsProbe
): Promise<Omit<DroppedFileSnapshot, 'droppedId'>> {
  if (typeof path !== 'string' || path.trim().length === 0) {
    // A virtual file — an Outlook attachment, a browser drag — has no local path.
    throw new DroppedFileError("This file isn't saved on your computer yet. Save it somewhere first, then drop it on Lumi.")
  }

  if (SHORTCUT_EXTENSIONS.has(extname(path).toLocaleLowerCase('en-US'))) {
    throw new DroppedFileError('Lumi works with files, not shortcuts. Drop the file itself.')
  }

  let initial: Awaited<ReturnType<typeof lstat>>
  try {
    initial = await lstat(path)
  } catch {
    throw new DroppedFileError("Lumi can't find that file anymore. It may have been moved or renamed.")
  }
  assertRegularFile(initial)

  let canonicalPath: string
  try {
    canonicalPath = await realpath(path)
  } catch {
    throw new DroppedFileError("Lumi can't find that file anymore. It may have been moved or renamed.")
  }

  // A junction or link could have been swapped in between the two checks.
  let metadata: Awaited<ReturnType<typeof lstat>>
  try {
    metadata = await lstat(canonicalPath)
  } catch {
    throw new DroppedFileError("Lumi can't find that file anymore. It may have been moved or renamed.")
  }
  assertRegularFile(metadata)

  if (metadata.size > MAX_ATTACHMENT_BYTES) {
    throw new DroppedFileError(
      `This file is ${formatMegabytes(metadata.size)} — Lumi handles files up to 50 MB, and photos up to 10 MB. Nothing was added.`
    )
  }

  let header: Buffer
  try {
    header = await readAttachmentPrefix(canonicalPath)
  } catch {
    throw new DroppedFileError('Lumi could not read that file. Nothing was added.')
  }

  let sniffedType: AttachmentType
  try {
    sniffedType = sniffAttachmentType(extname(canonicalPath), header)
  } catch {
    throw new DroppedFileError("Lumi can't take this file type yet. It works with JPEG, PNG, WebP, PDF, Word, and text files.")
  }

  const mediaKind: AttachmentMediaKind =
    sniffedType === 'jpeg' || sniffedType === 'png' || sniffedType === 'webp' ? 'photo' : 'document'

  if (mediaKind === 'photo') {
    if (metadata.size > MAX_PHOTO_BYTES) {
      throw new DroppedFileError(
        `This photo is ${formatMegabytes(metadata.size)} — Lumi handles photos up to 10 MB. Nothing was added.`
      )
    }
    let dimensions: { width: number; height: number } | undefined
    try {
      dimensions = probeImage?.(canonicalPath)
    } catch {
      dimensions = undefined
    }
    if (probeImage && (!dimensions || !isTelegramSafeDimensions(dimensions.width, dimensions.height))) {
      throw new DroppedFileError('Lumi cannot work with that image safely. Nothing was added.')
    }
  }

  if (sniffedType === 'txt' && metadata.size > MAX_TEXT_BYTES) {
    throw new DroppedFileError('This text file is larger than 2 MB. Nothing was added.')
  }

  return {
    canonicalPath,
    fileName: basename(canonicalPath),
    sizeBytes: metadata.size,
    mtimeMs: metadata.mtimeMs,
    sniffedType,
    mediaKind,
    fileTypeLabel: attachmentTypeLabel(sniffedType)
  }
}

/** What `lstat` tells us about a candidate. Narrowed so it can be faked in tests. */
export interface DroppableStats {
  isSymbolicLink(): boolean
  isDirectory(): boolean
  isFile(): boolean
}

/**
 * The rule that keeps a drop to a single real file.
 *
 * Exported so it can be tested directly: creating a real symbolic link needs
 * elevation on Windows, and a security control must not go unverified just
 * because the test host lacks a privilege.
 */
export function assertRegularFile(stats: DroppableStats): void {
  // lstat does not follow links, so a symlink or Windows junction lands here.
  if (stats.isSymbolicLink()) {
    throw new DroppedFileError('Lumi works with files, not shortcuts. Drop the file itself.')
  }
  if (stats.isDirectory()) {
    throw new DroppedFileError('Lumi takes one file, not a folder. To let Lumi search a folder, approve it in Settings.')
  }
  if (!stats.isFile()) {
    throw new DroppedFileError("Lumi can't take that. Drop a single file.")
  }
}

function formatMegabytes(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024))} MB`
}

export class DroppedFileStore {
  private entry: DroppedFileSnapshot | undefined
  private expiresAt = 0
  /**
   * Identifiers this store has let go of, so an action on one can say plainly
   * that the temporary file is gone instead of falling through to "not a result
   * from an approved search", which would be untrue and confusing.
   *
   * Identifiers only — no path, no metadata. Bounded to the recent few.
   */
  private readonly invalidated: string[] = []

  constructor(
    private readonly probeImage?: ImageDimensionsProbe,
    private readonly now: () => number = Date.now
  ) {}

  /**
   * Validates and retains one dropped file, replacing any previous one.
   * Returns only what the renderer is allowed to see.
   */
  async register(path: string): Promise<DroppedFileDescriptor> {
    const validated = await validateDroppedFile(path, this.probeImage)
    const snapshot: DroppedFileSnapshot = Object.freeze({ droppedId: randomUUID(), ...validated })
    // Capacity one: the previous entry is let go of, not queued, and its
    // identifier is remembered as invalidated so actions on it fail honestly.
    this.clear()
    this.entry = snapshot
    this.expiresAt = this.now() + DROPPED_FILE_TTL_MS
    return describe(snapshot, this.expiresAt)
  }

  /** The renderer-safe view, or nothing when there is no live entry. */
  current(): DroppedFileDescriptor | undefined {
    const snapshot = this.peek()
    return snapshot ? describe(snapshot, this.expiresAt) : undefined
  }

  /**
   * Re-checks the file and returns its canonical path.
   *
   * Called immediately before every open, analyse, and send. Any change to the
   * size, mtime, type, or media kind aborts and clears the entry — the same
   * rule approved-folder results are held to.
   */
  async resolve(droppedId: string): Promise<string | undefined> {
    const snapshot = this.peek()
    if (!snapshot || snapshot.droppedId !== droppedId) {
      return undefined
    }

    let current: Omit<DroppedFileSnapshot, 'droppedId'>
    try {
      current = await validateDroppedFile(snapshot.canonicalPath, this.probeImage)
    } catch {
      this.clear()
      return undefined
    }

    if (
      current.canonicalPath !== snapshot.canonicalPath ||
      current.fileName !== snapshot.fileName ||
      current.sizeBytes !== snapshot.sizeBytes ||
      current.mtimeMs !== snapshot.mtimeMs ||
      current.sniffedType !== snapshot.sniffedType ||
      current.mediaKind !== snapshot.mediaKind
    ) {
      this.clear()
      return undefined
    }

    // Deliberately no TTL refresh. `resolve` runs at proposal time as well as
    // at approval, so refreshing here would let merely rendering a confirmation
    // card extend the temporary record's life. The expiry is fixed at
    // registration; to keep working past it the user drops the file again.
    return snapshot.canonicalPath
  }

  /** The frozen snapshot, for building a confirmation preview. */
  snapshot(droppedId: string): DroppedFileSnapshot | undefined {
    const snapshot = this.peek()
    return snapshot && snapshot.droppedId === droppedId ? snapshot : undefined
  }

  has(droppedId: string): boolean {
    return this.snapshot(droppedId) !== undefined
  }

  remove(droppedId: string): boolean {
    if (this.entry?.droppedId !== droppedId) {
      return false
    }
    this.clear()
    return true
  }

  clear(): void {
    if (this.entry) {
      this.invalidated.push(this.entry.droppedId)
      while (this.invalidated.length > 8) {
        this.invalidated.shift()
      }
    }
    this.entry = undefined
    this.expiresAt = 0
  }

  /** True for an identifier this store held and has since let go of. */
  wasInvalidated(droppedId: string): boolean {
    // Expiry is lazy, so apply it before answering — otherwise an entry that
    // has just lapsed would not yet be recorded as invalidated.
    this.peek()
    return this.invalidated.includes(droppedId)
  }

  private peek(): DroppedFileSnapshot | undefined {
    if (this.entry && this.now() >= this.expiresAt) {
      this.clear()
    }
    return this.entry
  }
}

/** Everything the renderer may know. Notably: no path of any kind. */
function describe(snapshot: DroppedFileSnapshot, expiresAt: number): DroppedFileDescriptor {
  return {
    droppedId: snapshot.droppedId,
    fileName: snapshot.fileName,
    fileTypeLabel: snapshot.fileTypeLabel,
    sizeBytes: snapshot.sizeBytes,
    mediaKind: snapshot.mediaKind,
    expiresAt: new Date(expiresAt).toISOString()
  }
}

/**
 * The narrow view main's trusted-file consumers need.
 *
 * Kept minimal so a consumer can be handed the ability to resolve and describe
 * a dropped file without also being able to register or clear one.
 */
export interface DroppedFileLookup {
  resolve(droppedId: string): Promise<string | undefined>
  snapshot(droppedId: string): DroppedFileSnapshot | undefined
  wasInvalidated(droppedId: string): boolean
}
