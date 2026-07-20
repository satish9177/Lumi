import { nativeImage } from 'electron'
import { extname } from 'node:path'
import {
  canonicalizeApprovedRoots,
  resolveApprovedDocumentPath
} from '../../features/document-tools/search'
import type { ResultThumbnail } from '../../shared/contracts'
import { isImageExtension } from '../../shared/search-query'
import type { CaptureImage } from './capture'
import type { DroppedFileLookup } from './dropped-files'
import type { LocalStore } from './store'

/** Bounds chosen so a full grid stays small enough for the renderer. */
export const MAX_THUMBNAILS = 12
export const THUMBNAIL_MAX_WIDTH = 240
export const THUMBNAIL_MAX_HEIGHT = 180
export const MAX_THUMBNAIL_BYTES = 25_000
export const MAX_THUMBNAIL_SET_BYTES = 12 * MAX_THUMBNAIL_BYTES
const MIN_THUMBNAIL_WIDTH = 64
const QUALITY_LADDER = [70, 60, 50, 40]
const MAX_SHRINK_ATTEMPTS = 8

export type ImageLoader = (path: string) => CaptureImage | undefined

/**
 * Builds local previews for results the user can already see. Nothing here is
 * ever sent to the model: thumbnails exist only so the renderer can show a
 * photo grid without the renderer ever learning a filesystem path.
 *
 * A caller may only name opaque result identifiers from a previous approved
 * search; every path is resolved from the trusted store and revalidated against
 * its approved root immediately before the file is read.
 */
export async function createResultThumbnails(
  store: LocalStore,
  resultIds: readonly string[],
  loadImage: ImageLoader = loadNativeImage,
  droppedFiles?: DroppedFileLookup
): Promise<ResultThumbnail[]> {
  if (!Array.isArray(resultIds)) {
    return []
  }

  const requested = resultIds.slice(0, MAX_THUMBNAILS)
  const thumbnails: ResultThumbnail[] = []
  let setBytes = 0

  for (const resultId of requested) {
    if (typeof resultId !== 'string' || resultId.length === 0 || resultId.length > 250) {
      continue
    }

    const thumbnail = await createOneThumbnail(store, resultId, loadImage, MAX_THUMBNAIL_SET_BYTES - setBytes, droppedFiles)
    setBytes += byteLengthOf(thumbnail.dataUrl)
    thumbnails.push(thumbnail)
  }

  return thumbnails
}

async function createOneThumbnail(
  store: LocalStore,
  resultId: string,
  loadImage: ImageLoader,
  remainingBytes: number,
  droppedFiles?: DroppedFileLookup
): Promise<ResultThumbnail> {
  // Resolving revalidates a dropped record before any byte is read.
  const safePath = await resolveTrustedPath(store, droppedFiles, resultId)
  if (!safePath) {
    return { resultId, status: 'unavailable' }
  }

  if (!isImageExtension(extname(safePath))) {
    return { resultId, status: 'unsupported' }
  }

  let image: CaptureImage | undefined
  try {
    image = loadImage(safePath)
  } catch {
    // A corrupt or unreadable image must degrade to a placeholder, never throw.
    return { resultId, status: 'unsupported' }
  }

  if (!image || image.isEmpty?.()) {
    return { resultId, status: 'unsupported' }
  }

  const budget = Math.min(MAX_THUMBNAIL_BYTES, Math.max(0, remainingBytes))
  if (budget === 0) {
    return { resultId, status: 'too_large' }
  }

  try {
    const encoded = encodeThumbnail(image, budget)
    return encoded
      ? { resultId, status: 'ok', ...encoded }
      : { resultId, status: 'too_large' }
  } catch {
    return { resultId, status: 'unsupported' }
  }
}

/**
 * Resolves a stored result to a canonical path that is still inside its
 * approved root. Returns undefined for an unknown result, a revoked folder, a
 * deleted file, or any path that escapes.
 */
/**
 * Resolves any identifier the renderer may legitimately name to a path main
 * trusts.
 *
 * Two kinds of trust meet here, and only here: a dropped file, which the user
 * handed Lumi directly and which is revalidated on every use, and an
 * approved-folder search result, which must still prove membership in an
 * approved root. Both are UUIDs, so no action contract changes shape.
 *
 * A dropped identifier never gains approved-root trust, and an unknown
 * identifier resolves to nothing in both branches.
 */
export async function resolveTrustedPath(
  store: LocalStore,
  droppedFiles: { resolve(id: string): Promise<string | undefined> } | undefined,
  id: string
): Promise<string | undefined> {
  return (await droppedFiles?.resolve(id)) ?? (await resolveTrustedResultPath(store, id))
}

export async function resolveTrustedResultPath(store: LocalStore, resultId: string): Promise<string | undefined> {
  const storedResult = await store.getSearchResult(resultId)
  if (!storedResult) {
    return undefined
  }

  const root = await store.getDocumentRoot(storedResult.rootId)
  if (!root) {
    return undefined
  }

  try {
    const approvedRoots = await canonicalizeApprovedRoots([root.path])
    return await resolveApprovedDocumentPath(storedResult.absolutePath, approvedRoots)
  } catch {
    return undefined
  }
}

/** Fits the image inside the thumbnail box, then compresses within the cap. */
export function encodeThumbnail(
  sourceImage: CaptureImage,
  maxBytes = MAX_THUMBNAIL_BYTES
): { dataUrl: string; width: number; height: number } | undefined {
  const size = sourceImage.getSize()
  if (size.width <= 0 || size.height <= 0) {
    return undefined
  }

  const scale = Math.min(THUMBNAIL_MAX_WIDTH / size.width, THUMBNAIL_MAX_HEIGHT / size.height, 1)
  // Supplying only width keeps the source aspect ratio intact.
  let image = scale < 1 ? sourceImage.resize({ width: Math.max(1, Math.round(size.width * scale)) }) : sourceImage

  // Bounded by attempts, not just by width: an image whose resize does not
  // actually shrink must fail cleanly rather than spin the main process.
  for (let attempt = 0; attempt <= MAX_SHRINK_ATTEMPTS; attempt += 1) {
    for (const quality of QUALITY_LADDER) {
      const jpeg = image.toJPEG(quality)
      if (jpeg.byteLength <= maxBytes) {
        const encodedSize = image.getSize()
        return {
          dataUrl: `data:image/jpeg;base64,${jpeg.toString('base64')}`,
          width: encodedSize.width,
          height: encodedSize.height
        }
      }
    }

    const currentWidth = image.getSize().width
    if (currentWidth <= MIN_THUMBNAIL_WIDTH) {
      return undefined
    }

    image = image.resize({ width: Math.max(MIN_THUMBNAIL_WIDTH, Math.round(currentWidth * 0.7)) })
    if (image.getSize().width >= currentWidth) {
      return undefined
    }
  }

  return undefined
}

function loadNativeImage(path: string): CaptureImage | undefined {
  const image = nativeImage.createFromPath(path)
  return image.isEmpty() ? undefined : (image as unknown as CaptureImage)
}

function byteLengthOf(dataUrl: string | undefined): number {
  if (!dataUrl) {
    return 0
  }
  return Buffer.from(dataUrl.split(',')[1] ?? '', 'base64').byteLength
}
