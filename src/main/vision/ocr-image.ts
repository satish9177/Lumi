/**
 * Preparing a bounded image for the local OCR engine.
 *
 * The Phase-1 path hands CLIP a 224x224 crop, which is useless for reading
 * text. OCR needs real resolution, so this module decodes a separate, larger,
 * still strictly bounded rendition.
 *
 * Two deliberate choices:
 *
 *  - **Greyscale.** Tesseract binarizes internally, so colour is discarded
 *    work. Dropping it early cuts the buffer handed across the process boundary
 *    to a quarter and removes any question of colour profiles affecting the
 *    result.
 *  - **PNG encoded here, in pure Node.** The alternative was to widen the
 *    thumbnail-decoder interface with an Electron `toPNG()`, which would make
 *    this path untestable without a real Electron image. Encoding is lossless,
 *    deterministic, and about as cheap as the memcpy it replaces.
 *
 * The whole image is aspect-preserved and never upscaled: enlarging a small
 * photo invents detail and makes the engine confidently misread it.
 */

import { deflateSync } from 'node:zlib'
import { PhotoDecodeError, revalidateSnapshot, type BoundedNativeImage, type PhotoFileSnapshot, type ThumbnailDecoder } from './scanner'

/**
 * The longest edge handed to the OCR engine. 1600 keeps a 1080p screenshot
 * near its native resolution — the dominant case for the text people search
 * for — while capping the worst case at roughly 2.5 megapixels of greyscale.
 */
export const OCR_MAX_EDGE = 1600

/** Below this there is no text worth reading, and the engine wastes its budget. */
export const OCR_MIN_EDGE = 32

export interface OcrImage {
  /** PNG bytes. Greyscale, 8 bits per pixel. */
  png: Buffer
  width: number
  height: number
}

/**
 * Revalidates the file, then decodes it to a bounded greyscale PNG.
 *
 * Revalidation happens here as well as at dequeue because OCR runs long after
 * the file was queued; the image may have been replaced or deleted since.
 */
export async function decodeForOcr(
  snapshot: PhotoFileSnapshot,
  decode: ThumbnailDecoder
): Promise<OcrImage> {
  if (!(await revalidateSnapshot(snapshot))) {
    throw new PhotoDecodeError('not_a_real_file')
  }

  if (snapshot.width < OCR_MIN_EDGE && snapshot.height < OCR_MIN_EDGE) {
    throw new PhotoDecodeError('unsupported_format')
  }

  // Never upscale: a scale above 1 would invent detail the engine then reads as
  // characters that are not there.
  const scale = Math.min(1, OCR_MAX_EDGE / Math.max(snapshot.width, snapshot.height))
  const targetWidth = Math.max(1, Math.round(snapshot.width * scale))
  const targetHeight = Math.max(1, Math.round(snapshot.height * scale))

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
  if (size.width <= 0 || size.height <= 0 || size.width > OCR_MAX_EDGE * 2 || size.height > OCR_MAX_EDGE * 2) {
    throw new PhotoDecodeError('too_many_pixels')
  }

  const bitmap = image.toBitmap()
  if (bitmap.byteLength !== size.width * size.height * 4) {
    throw new PhotoDecodeError('decode_failed')
  }

  return {
    png: encodeGreyscalePng(bgraToGreyscale(bitmap, size.width, size.height), size.width, size.height),
    width: size.width,
    height: size.height
  }
}

/** Rec. 601 luma, the weighting Tesseract's own greyscale conversion assumes. */
export function bgraToGreyscale(bitmap: Uint8Array, width: number, height: number): Uint8Array {
  const out = new Uint8Array(width * height)
  for (let index = 0, pixel = 0; pixel < out.length; index += 4, pixel += 1) {
    const blue = bitmap[index]!
    const green = bitmap[index + 1]!
    const red = bitmap[index + 2]!
    out[pixel] = (red * 77 + green * 150 + blue * 29) >> 8
  }
  return out
}

/** Minimal 8-bit greyscale PNG. No ancillary chunks, so the output is stable. */
export function encodeGreyscalePng(pixels: Uint8Array, width: number, height: number): Buffer {
  const stride = width + 1
  const raw = Buffer.alloc(stride * height)
  for (let y = 0; y < height; y += 1) {
    raw[y * stride] = 0 // filter type 0 (None)
    raw.set(pixels.subarray(y * width, (y + 1) * width), y * stride + 1)
  }

  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 0 // colour type: greyscale
  ihdr[10] = 0 // deflate
  ihdr[11] = 0 // adaptive filtering
  ihdr[12] = 0 // no interlace

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw, { level: 6 })),
    pngChunk('IEND', Buffer.alloc(0))
  ])
}

function pngChunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([length, body, crc])
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    table[n] = c >>> 0
  }
  return table
})()

function crc32(buffer: Buffer): number {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc = CRC_TABLE[(crc ^ byte) & 0xff]! ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code
}
