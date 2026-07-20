import { nativeImage } from 'electron'
import { open, stat } from 'node:fs/promises'
import { basename, extname } from 'node:path'
import type { AttachmentMediaKind } from '../../shared/contracts'
import type { LocalStore } from './store'
import { resolveTrustedResultPath } from './thumbnails'

export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
export const MAX_PHOTO_BYTES = 10 * 1024 * 1024
export const MAX_TEXT_BYTES = 2 * 1024 * 1024
export const MAX_PHOTO_DIMENSION = 10_000
export const MAX_PHOTO_ASPECT_RATIO = 20
const SNIFF_BYTES = 8 * 1024

export type AttachmentType = 'jpeg' | 'png' | 'webp' | 'pdf' | 'doc' | 'docx' | 'txt'

export interface TrustedAttachmentSnapshot {
  fileResultId: string
  canonicalPath: string
  fileName: string
  mediaKind: AttachmentMediaKind
  sizeBytes: number
  mtimeMs: number
  sniffedType: AttachmentType
  fileTypeLabel: string
}

export type ImageDimensionsProbe = (path: string) => { width: number; height: number } | undefined

export async function validateTrustedAttachment(
  store: LocalStore,
  fileResultId: string,
  probeImage: ImageDimensionsProbe = probeNativeImage
): Promise<TrustedAttachmentSnapshot> {
  const stored = await store.getSearchResult(fileResultId)
  if (!stored) {
    throw new Error('That file is not a result from an approved search. Search again first.')
  }

  const canonicalPath = await resolveTrustedResultPath(store, fileResultId)
  if (!canonicalPath) {
    throw new Error('That file is no longer available inside its approved folder.')
  }

  let metadata: Awaited<ReturnType<typeof stat>>
  try {
    metadata = await stat(canonicalPath)
  } catch {
    throw new Error('That file is no longer available inside its approved folder.')
  }
  if (!metadata.isFile()) {
    throw new Error('The selected result is no longer a regular file.')
  }
  if (metadata.size > MAX_ATTACHMENT_BYTES) {
    throw new Error('Telegram attachments must be 50 MB or smaller.')
  }

  const extension = extname(canonicalPath).toLocaleLowerCase('en-US')
  let header: Buffer
  try {
    header = await readPrefix(canonicalPath)
  } catch {
    throw new Error('That file could not be read safely. Nothing was sent.')
  }
  const sniffedType = sniffAttachmentType(extension, header)
  const mediaKind: AttachmentMediaKind = sniffedType === 'jpeg' || sniffedType === 'png' || sniffedType === 'webp'
    ? 'photo'
    : 'document'

  if (mediaKind === 'photo') {
    if (metadata.size > MAX_PHOTO_BYTES) {
      throw new Error('Telegram photos must be 10 MB or smaller. Nothing was sent.')
    }
    let dimensions: { width: number; height: number } | undefined
    try {
      dimensions = probeImage(canonicalPath)
    } catch {
      dimensions = undefined
    }
    if (!dimensions || !isTelegramSafeDimensions(dimensions.width, dimensions.height)) {
      throw new Error('That image cannot be sent safely as a Telegram photo. Nothing was sent.')
    }
  }

  if (sniffedType === 'txt' && metadata.size > MAX_TEXT_BYTES) {
    throw new Error('Text attachments must be 2 MB or smaller.')
  }

  return Object.freeze({
    fileResultId,
    canonicalPath,
    fileName: basename(canonicalPath),
    mediaKind,
    sizeBytes: metadata.size,
    mtimeMs: metadata.mtimeMs,
    sniffedType,
    fileTypeLabel: typeLabel(sniffedType)
  })
}

export async function revalidateTrustedAttachment(
  store: LocalStore,
  reviewed: TrustedAttachmentSnapshot,
  probeImage: ImageDimensionsProbe = probeNativeImage
): Promise<TrustedAttachmentSnapshot> {
  let current: TrustedAttachmentSnapshot
  try {
    current = await validateTrustedAttachment(store, reviewed.fileResultId, probeImage)
  } catch {
    throw changedFileError()
  }

  if (
    current.canonicalPath !== reviewed.canonicalPath ||
    current.fileName !== reviewed.fileName ||
    current.sizeBytes !== reviewed.sizeBytes ||
    current.mtimeMs !== reviewed.mtimeMs ||
    current.sniffedType !== reviewed.sniffedType ||
    current.mediaKind !== reviewed.mediaKind
  ) {
    throw changedFileError()
  }
  return current
}

export function sniffAttachmentType(extension: string, bytes: Buffer): AttachmentType {
  const normalized = extension.toLocaleLowerCase('en-US')
  if ((normalized === '.jpg' || normalized === '.jpeg') && startsWith(bytes, [0xff, 0xd8, 0xff])) return 'jpeg'
  if (normalized === '.png' && startsWith(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return 'png'
  if (normalized === '.webp' && bytes.length >= 12 && bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP') return 'webp'
  if (normalized === '.pdf' && bytes.subarray(0, 5).toString('ascii') === '%PDF-') return 'pdf'
  if (normalized === '.docx' && startsWith(bytes, [0x50, 0x4b, 0x03, 0x04])) return 'docx'
  if (normalized === '.doc' && startsWith(bytes, [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])) return 'doc'
  if (normalized === '.txt' && !bytes.includes(0)) return 'txt'

  const supported = new Set(['.jpg', '.jpeg', '.png', '.webp', '.pdf', '.doc', '.docx', '.txt'])
  if (supported.has(normalized)) {
    throw new Error('The file contents do not match its trusted filename extension.')
  }
  throw new Error('That file type is not supported for Telegram attachments.')
}

export function isTelegramSafeDimensions(width: number, height: number): boolean {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) return false
  const ratio = Math.max(width / height, height / width)
  return width <= MAX_PHOTO_DIMENSION && height <= MAX_PHOTO_DIMENSION && ratio <= MAX_PHOTO_ASPECT_RATIO
}

/** Exported so dropped-file validation reuses the same labels. */
export function attachmentTypeLabel(type: AttachmentType): string {
  return typeLabel(type)
}

function typeLabel(type: AttachmentType): string {
  switch (type) {
    case 'jpeg': return 'JPEG image'
    case 'png': return 'PNG image'
    case 'webp': return 'WebP image'
    case 'pdf': return 'PDF document'
    case 'doc': return 'Word document'
    case 'docx': return 'Word document'
    case 'txt': return 'Text document'
  }
}

/** Exported so dropped-file validation sniffs with the identical prefix read. */
export function readAttachmentPrefix(path: string): Promise<Buffer> {
  return readPrefix(path)
}

async function readPrefix(path: string): Promise<Buffer> {
  const handle = await open(path, 'r')
  try {
    const buffer = Buffer.alloc(SNIFF_BYTES)
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0)
    return buffer.subarray(0, bytesRead)
  } finally {
    await handle.close()
  }
}

function startsWith(buffer: Buffer, signature: readonly number[]): boolean {
  return buffer.length >= signature.length && signature.every((byte, index) => buffer[index] === byte)
}

function probeNativeImage(path: string): { width: number; height: number } | undefined {
  const image = nativeImage.createFromPath(path)
  if (image.isEmpty()) return undefined
  return image.getSize()
}

function changedFileError(): Error {
  return new Error('That file changed since you reviewed it. Nothing was sent. Please confirm it again.')
}
