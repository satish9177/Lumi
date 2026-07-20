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

/** One file at a time. A second drop replaces the first. */
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
    // Capacity one: the previous entry is dropped, not queued.
    this.entry = snapshot
    this.expiresAt = this.now() + DROPPED_FILE_TTL_MS
    return describe(snapshot)
  }

  /** The renderer-safe view, or nothing when there is no live entry. */
  current(): DroppedFileDescriptor | undefined {
    const snapshot = this.peek()
    return snapshot ? describe(snapshot) : undefined
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

    // A confirmed use keeps the entry alive.
    this.expiresAt = this.now() + DROPPED_FILE_TTL_MS
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
    this.entry = undefined
    this.expiresAt = 0
  }

  private peek(): DroppedFileSnapshot | undefined {
    if (this.entry && this.now() >= this.expiresAt) {
      this.clear()
    }
    return this.entry
  }
}

/** Everything the renderer may know. Notably: no path of any kind. */
function describe(snapshot: DroppedFileSnapshot): DroppedFileDescriptor {
  return {
    droppedId: snapshot.droppedId,
    fileName: snapshot.fileName,
    fileTypeLabel: snapshot.fileTypeLabel,
    sizeBytes: snapshot.sizeBytes,
    mediaKind: snapshot.mediaKind
  }
}
