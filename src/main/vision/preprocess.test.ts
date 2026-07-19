import { describe, expect, it } from 'vitest'
import {
  bgraToClipTensor,
  CLIP_PIXEL_MEAN,
  CLIP_PIXEL_STD,
  CLIP_TENSOR_DIMS,
  normalizedEmbedding
} from './preprocess'
import { VISION_BITMAP_BYTES } from './protocol'

const PLANE = 224 * 224

describe('bgraToClipTensor', () => {
  it('produces a CHW RGB tensor of the shape CLIP expects', () => {
    const tensor = bgraToClipTensor(new Uint8Array(VISION_BITMAP_BYTES).fill(255))

    expect(tensor).toBeInstanceOf(Float32Array)
    expect(tensor.length).toBe(3 * PLANE)
    expect(CLIP_TENSOR_DIMS).toEqual([1, 3, 224, 224])
  })

  it('reorders BGRA source bytes into RGB planes and applies CLIP normalization', () => {
    const bitmap = new Uint8Array(VISION_BITMAP_BYTES)
    // First pixel: blue=10, green=20, red=30, alpha=255.
    bitmap[0] = 10
    bitmap[1] = 20
    bitmap[2] = 30
    bitmap[3] = 255

    const tensor = bgraToClipTensor(bitmap)

    expect(tensor[0]).toBeCloseTo((30 / 255 - CLIP_PIXEL_MEAN[0]) / CLIP_PIXEL_STD[0], 5)
    expect(tensor[PLANE]).toBeCloseTo((20 / 255 - CLIP_PIXEL_MEAN[1]) / CLIP_PIXEL_STD[1], 5)
    expect(tensor[2 * PLANE]).toBeCloseTo((10 / 255 - CLIP_PIXEL_MEAN[2]) / CLIP_PIXEL_STD[2], 5)
  })

  it('never emits a non-finite value for any byte in range', () => {
    const bitmap = new Uint8Array(VISION_BITMAP_BYTES)
    for (let index = 0; index < bitmap.length; index += 1) {
      bitmap[index] = index % 256
    }

    expect(bgraToClipTensor(bitmap).every((value) => Number.isFinite(value))).toBe(true)
  })

  it('rejects a bitmap whose byte count is wrong', () => {
    for (const size of [0, VISION_BITMAP_BYTES - 1, VISION_BITMAP_BYTES + 1]) {
      expect(() => bgraToClipTensor(new Uint8Array(size))).toThrow(
        expect.objectContaining({ code: 'invalid_bitmap' })
      )
    }
  })
})

describe('normalizedEmbedding', () => {
  function vector(length: number, fill = 0.25): Float32Array {
    return new Float32Array(length).fill(fill)
  }

  function magnitude(values: Float32Array): number {
    return Math.sqrt(values.reduce((total, value) => total + value * value, 0))
  }

  it('accepts the one CLIP ViT-B/32 projected output width', () => {
    expect(normalizedEmbedding(vector(512))).toHaveLength(512)
  })

  it('returns a unit-length vector so ranking can use a plain dot product', () => {
    expect(magnitude(normalizedEmbedding(vector(512, 0.25)))).toBeCloseTo(1, 5)

    const uneven = new Float32Array(512)
    uneven[0] = 3
    uneven[1] = 4
    const normalized = normalizedEmbedding(uneven)
    expect(magnitude(normalized)).toBeCloseTo(1, 5)
    expect(normalized[0]).toBeCloseTo(0.6, 5)
    expect(normalized[1]).toBeCloseTo(0.8, 5)
  })

  it('preserves direction, so two scalings of one vector normalize identically', () => {
    const base = Float32Array.from({ length: 512 }, (_value, index) => (index % 5) - 2)
    const scaled = base.map((value) => value * 7.5)
    const first = normalizedEmbedding(base)
    const second = normalizedEmbedding(scaled)
    for (let index = 0; index < first.length; index += 1) {
      expect(second[index]).toBeCloseTo(first[index]!, 5)
    }
  })

  it('rejects an unexpected embedding length', () => {
    for (const length of [1, 256, 511, 513, 768, 1_024, 2_400]) {
      expect(() => normalizedEmbedding(vector(length))).toThrow(
        expect.objectContaining({ code: 'unexpected_embedding_length' })
      )
    }
  })

  it('rejects NaN and Infinity anywhere in the vector', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      const values = vector(512)
      values[500] = bad
      expect(() => normalizedEmbedding(values)).toThrow(
        expect.objectContaining({ code: 'non_finite_embedding' })
      )
    }
  })

  it('rejects an all-zero vector, which has no direction to compare', () => {
    expect(() => normalizedEmbedding(vector(512, 0))).toThrow(
      expect.objectContaining({ code: 'invalid_output' })
    )
  })

  it('rejects output shapes that are not numeric vectors', () => {
    for (const output of [undefined, null, 'embedding', { data: [1, 2] }, new BigInt64Array(512)]) {
      expect(() => normalizedEmbedding(output)).toThrow(
        expect.objectContaining({ code: 'invalid_output' })
      )
    }
  })

  it('accepts a plain number array of a valid length', () => {
    expect(normalizedEmbedding(new Array(512).fill(0.5))).toHaveLength(512)
  })
})
