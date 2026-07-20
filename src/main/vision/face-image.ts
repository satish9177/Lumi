/**
 * Preparing a bounded image for the local face detector.
 *
 * The pinned YuNet export has a fixed 640x640 input, so every photo has to be
 * fitted into that square. It is *letterboxed* — scaled to fit and padded —
 * rather than centre-cropped the way the CLIP path is. A crop would silently
 * cut people out of the edges of a group photo, and undercounting faces is the
 * failure this feature can least afford: it turns "five people" into "three"
 * with no indication that anything was lost.
 *
 * As with the CLIP path, main owns image preparation and the worker receives
 * only an exact, bounded bitmap.
 */

import { FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH } from './protocol'
import {
  PhotoDecodeError,
  revalidateSnapshot,
  type BoundedNativeImage,
  type PhotoFileSnapshot,
  type ThumbnailDecoder
} from './scanner'

export interface FaceBitmap {
  /** Exactly FACE_BITMAP_WIDTH x FACE_BITMAP_HEIGHT BGRA bytes. */
  bitmap: ArrayBuffer
  /** The scale applied to the source, retained for tests and diagnostics. */
  scale: number
}

/**
 * Decodes a photo into the exact square bitmap the detector accepts.
 *
 * Revalidates first: face scanning runs long after the file was queued, so the
 * image may have been replaced or deleted in the meantime.
 */
export async function decodeForFaceDetection(
  snapshot: PhotoFileSnapshot,
  decode: ThumbnailDecoder
): Promise<FaceBitmap> {
  if (!(await revalidateSnapshot(snapshot))) {
    throw new PhotoDecodeError('not_a_real_file')
  }
  if (snapshot.width <= 0 || snapshot.height <= 0) {
    throw new PhotoDecodeError('unsupported_format')
  }

  // Never upscale. Enlarging a thumbnail does not add detectable faces, it only
  // costs work and invites false positives on interpolation artefacts.
  const scale = Math.min(1, FACE_BITMAP_WIDTH / snapshot.width, FACE_BITMAP_HEIGHT / snapshot.height)
  const targetWidth = Math.max(1, Math.min(FACE_BITMAP_WIDTH, Math.round(snapshot.width * scale)))
  const targetHeight = Math.max(1, Math.min(FACE_BITMAP_HEIGHT, Math.round(snapshot.height * scale)))

  let image: BoundedNativeImage
  try {
    image = await decode(snapshot.absolutePath, { width: targetWidth, height: targetHeight })
  } catch (error) {
    if (isNodeError(error, 'EACCES') || isNodeError(error, 'EBUSY') || isNodeError(error, 'EPERM')) {
      throw new PhotoDecodeError('file_locked')
    }
    throw new PhotoDecodeError('decode_failed')
  }
  if (!image || image.isEmpty()) {
    throw new PhotoDecodeError('decode_failed')
  }

  const size = image.getSize()
  if (size.width <= 0 || size.height <= 0 || size.width > FACE_BITMAP_WIDTH || size.height > FACE_BITMAP_HEIGHT) {
    // The decoder returned something other than what was asked for. Padding it
    // blind would misplace every box, so this fails rather than guesses.
    throw new PhotoDecodeError('decode_failed')
  }

  const source = image.toBitmap()
  if (source.byteLength !== size.width * size.height * 4) {
    throw new PhotoDecodeError('decode_failed')
  }

  return { bitmap: letterbox(source, size.width, size.height), scale }
}

/**
 * Copies a smaller BGRA image into the top-left of a zeroed 640x640 canvas.
 *
 * Top-left rather than centred so the mapping from detector space back to the
 * source is a single scale factor with no offset — should a future local-only
 * UI ever need it, and so this function stays trivially checkable.
 */
export function letterbox(source: Uint8Array, width: number, height: number): ArrayBuffer {
  if (width > FACE_BITMAP_WIDTH || height > FACE_BITMAP_HEIGHT) {
    throw new PhotoDecodeError('too_many_pixels')
  }

  // Zero-filled, so padding is opaque black rather than uninitialised memory —
  // which would otherwise leak whatever the allocator last held into the model.
  const canvas = new Uint8Array(FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT * 4)
  const sourceStride = width * 4
  const canvasStride = FACE_BITMAP_WIDTH * 4

  for (let row = 0; row < height; row += 1) {
    canvas.set(source.subarray(row * sourceStride, (row + 1) * sourceStride), row * canvasStride)
  }

  return canvas.buffer as ArrayBuffer
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code
}
