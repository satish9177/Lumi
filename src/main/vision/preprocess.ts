/**
 * CLIP input preparation and output validation.
 *
 * Pure arithmetic on a bounded buffer: no filesystem, no Electron, no ONNX
 * Runtime. Main hands the worker a fixed-size BGRA bitmap and the worker turns
 * it into the exact tensor layout CLIP expects, so a malformed buffer is
 * rejected before it ever reaches native code.
 */

import {
  ALLOWED_EMBEDDING_LENGTHS,
  EMBEDDING_SAMPLE_COUNT,
  VISION_BITMAP_BYTES,
  VISION_BITMAP_HEIGHT,
  VISION_BITMAP_WIDTH,
  VisionProtocolError
} from './protocol'

/** OpenAI CLIP image preprocessing constants (RGB order). */
export const CLIP_PIXEL_MEAN = [0.481_454_66, 0.457_827_5, 0.408_210_73] as const
export const CLIP_PIXEL_STD = [0.268_629_54, 0.261_302_58, 0.275_777_11] as const

export const CLIP_TENSOR_DIMS = [1, 3, VISION_BITMAP_HEIGHT, VISION_BITMAP_WIDTH] as const
const PLANE = VISION_BITMAP_WIDTH * VISION_BITMAP_HEIGHT

/**
 * Converts a 224x224 BGRA bitmap into a normalized float32 NCHW RGB tensor.
 * Alpha is dropped: CLIP was trained on opaque RGB.
 */
export function bgraToClipTensor(bitmap: Uint8Array): Float32Array {
  if (bitmap.length !== VISION_BITMAP_BYTES) {
    throw new VisionProtocolError('invalid_bitmap')
  }

  const tensor = new Float32Array(3 * PLANE)
  for (let pixel = 0; pixel < PLANE; pixel += 1) {
    const offset = pixel * 4
    const blue = bitmap[offset] / 255
    const green = bitmap[offset + 1] / 255
    const red = bitmap[offset + 2] / 255
    tensor[pixel] = (red - CLIP_PIXEL_MEAN[0]) / CLIP_PIXEL_STD[0]
    tensor[PLANE + pixel] = (green - CLIP_PIXEL_MEAN[1]) / CLIP_PIXEL_STD[1]
    tensor[2 * PLANE + pixel] = (blue - CLIP_PIXEL_MEAN[2]) / CLIP_PIXEL_STD[2]
  }
  return tensor
}

export interface ValidatedEmbedding {
  embeddingLength: number
  sampleValues: number[]
}

/**
 * Accepts a model output only when it is a plain finite vector of a size CLIP
 * ViT-B/32 can actually produce. Anything else is a bounded failure rather than
 * a value the caller could mistake for a usable embedding.
 */
export function validateEmbedding(output: unknown): ValidatedEmbedding {
  const values = asNumericVector(output)
  if (!ALLOWED_EMBEDDING_LENGTHS.includes(values.length)) {
    throw new VisionProtocolError('unexpected_embedding_length')
  }

  let allZero = true
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new VisionProtocolError('non_finite_embedding')
    }
    if (value !== 0) {
      allZero = false
    }
  }
  if (allZero) {
    throw new VisionProtocolError('invalid_output')
  }

  const sampleValues: number[] = []
  for (let index = 0; index < Math.min(EMBEDDING_SAMPLE_COUNT, values.length); index += 1) {
    sampleValues.push(roundSample(values[index]))
  }
  return { embeddingLength: values.length, sampleValues }
}

/**
 * Validates a model output and returns it as a unit-length Float32Array.
 *
 * Every vector that reaches storage or ranking passes through here, so cosine
 * similarity later reduces to a plain dot product and a zero-magnitude vector
 * (which has no direction, and would silently score 0 against everything) is
 * rejected rather than stored.
 */
export function normalizedEmbedding(output: unknown): Float32Array {
  const values = asNumericVector(output)
  if (!ALLOWED_EMBEDDING_LENGTHS.includes(values.length)) {
    throw new VisionProtocolError('unexpected_embedding_length')
  }

  const vector = new Float32Array(values.length)
  let sumOfSquares = 0
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new VisionProtocolError('non_finite_embedding')
    }
    vector[index] = value
    sumOfSquares += value * value
  }

  const magnitude = Math.sqrt(sumOfSquares)
  if (!Number.isFinite(magnitude) || magnitude === 0) {
    throw new VisionProtocolError('invalid_output')
  }

  for (let index = 0; index < vector.length; index += 1) {
    vector[index] = vector[index]! / magnitude
  }
  return vector
}

function asNumericVector(output: unknown): ArrayLike<number> {
  if (output instanceof Float32Array || output instanceof Float64Array) {
    return output
  }
  if (Array.isArray(output)) {
    return output
  }
  throw new VisionProtocolError('invalid_output')
}

function roundSample(value: number): number {
  return Math.round(value * 10_000) / 10_000
}
