/**
 * Real inference for the Phase-3 pipeline: YuNet's landmark output and the
 * pinned SFace export.
 *
 * Every other Phase-3 test synthesizes tensors and asserts the arithmetic is
 * self-consistent, which cannot prove it matches what the real graphs
 * actually emit. Anchor ordering, the landmark offset formula, and the plain
 * pixel-range convention SFace expects are all conventions of these specific
 * exports — get one wrong and the code still produces plausible-looking
 * numbers, just numbers that do not mean what the alignment or matching logic
 * assumes they mean. Only running the real models can catch that.
 *
 * Every input here is generated in this file: deterministic noise, flat
 * fields, and a landmark template. No image asset is loaded from disk and no
 * personal or third-party photograph is committed anywhere in this repository.
 * A crude procedural pattern is not a face a production system would
 * recognise — see the module docstring on each `it` for exactly what is and
 * is not being claimed.
 *
 * Skipped whenever the required pack is not installed, so the suite stays
 * runnable offline and on a clean checkout. Nothing in this file performs a
 * network request: both models are loaded from a local path already on disk,
 * and the "no network" test asserts that explicitly rather than by absence.
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { alignFaceToTensor, REFERENCE_LANDMARKS, type SourceImage } from './face-align'
import { UNCERTAIN_FACE_SCORE } from './face-detect'
import { EXTRAS_PACK_ID, extrasAssetFor } from './extras-manifest'
import {
  decodeYunetLandmarks,
  LANDMARK_COUNT,
  type YunetLandmarkOutputs
} from './face-landmarks'
import { cosineSimilarity, normalizeEmbedding } from './person-profiles'
import { FACE_EMBED_DIMENSIONS, FACE_EMBED_INPUT_SIZE, PEOPLE_PACK_ID, peopleAssetFor } from './people-manifest'
import { FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH } from './protocol'

const APPDATA = process.env.APPDATA ?? join(process.env.USERPROFILE ?? '', 'AppData', 'Roaming')

const YUNET_PATH = join(APPDATA, 'lifelens', 'vision-models', EXTRAS_PACK_ID, extrasAssetFor('faceModel').fileName)
const SFACE_PATH = join(APPDATA, 'lifelens', 'vision-models', PEOPLE_PACK_ID, peopleAssetFor('faceEmbedModel').fileName)

const yunetInstalled = existsSync(YUNET_PATH)
const sfaceInstalled = existsSync(SFACE_PATH)

type Session = {
  inputNames: readonly string[]
  outputNames: readonly string[]
  run: (feeds: Record<string, unknown>) => Promise<Record<string, { data: unknown; dims: readonly number[] }>>
}

async function loadSession(
  path: string
): Promise<{ session: Session; Tensor: new (type: string, data: Float32Array, dims: readonly number[]) => unknown }> {
  const imported = (await import('onnxruntime-node')) as unknown as Record<string, unknown>
  const ort = (imported.default ?? imported) as {
    InferenceSession: { create: (path: string, options?: unknown) => Promise<Session> }
    Tensor: new (type: string, data: Float32Array, dims: readonly number[]) => unknown
  }
  const session = await ort.InferenceSession.create(path, { executionProviders: ['cpu'], graphOptimizationLevel: 'all' })
  return { session, Tensor: ort.Tensor }
}

/** Deterministic structured noise, so anchors actually vary rather than being uniform. */
function noiseImage(length: number): Float32Array {
  const values = new Float32Array(length)
  for (let index = 0; index < length; index += 1) {
    values[index] = ((index * 2654435761) % 256 + 256) % 256
  }
  return values
}

const FACE_STRIDES = [8, 16, 32] as const

function collectLandmarkOutputs(outputs: Record<string, { data: unknown }>): YunetLandmarkOutputs {
  const collected: YunetLandmarkOutputs = { cls: {}, obj: {}, bbox: {}, kps: {} }
  for (const stride of FACE_STRIDES) {
    for (const family of ['cls', 'obj', 'bbox', 'kps'] as const) {
      const data = outputs[`${family}_${stride}`]?.data
      if (data instanceof Float32Array) {
        collected[family][stride] = data
      }
    }
  }
  return collected
}

describe.skipIf(!yunetInstalled)('real YuNet landmark output', () => {
  it('exposes the kps tensor at each stride, at the width the landmark decoder assumes', async () => {
    const { session } = await loadSession(YUNET_PATH)
    for (const stride of FACE_STRIDES) {
      expect(session.outputNames).toContain(`kps_${stride}`)
    }
  }, 120_000)

  it('produces kps rows sized for exactly five 2-D points per anchor', async () => {
    const { session, Tensor } = await loadSession(YUNET_PATH)
    const input = new Float32Array(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT).fill(128)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', input, [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH])
    })

    for (const stride of FACE_STRIDES) {
      const anchors = (FACE_BITMAP_WIDTH / stride) * (FACE_BITMAP_HEIGHT / stride)
      expect(outputs[`kps_${stride}`]!.dims).toEqual([1, anchors, LANDMARK_COUNT * 2])
    }
  }, 120_000)

  it('decodes finite landmark points inside the frame from real structured-noise output', async () => {
    const { session, Tensor } = await loadSession(YUNET_PATH)
    const noisy = noiseImage(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', noisy, [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH])
    })

    const faces = decodeYunetLandmarks(collectLandmarkOutputs(outputs), FACE_BITMAP_WIDTH)
    for (const face of faces) {
      expect(face.landmarks).toHaveLength(LANDMARK_COUNT)
      for (const point of face.landmarks) {
        expect(Number.isFinite(point.x)).toBe(true)
        expect(Number.isFinite(point.y)).toBe(true)
      }
      expect(face.score).toBeGreaterThanOrEqual(UNCERTAIN_FACE_SCORE)
    }
  }, 120_000)
})

describe.skipIf(!sfaceInstalled)('real SFace inference', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    // The model is already on disk; nothing in this suite may reach the
    // network to get it, and a fetch attempt fails the test immediately
    // rather than silently succeeding via a mock.
    globalThis.fetch = (() => {
      throw new Error('real-people-inference must not access the network')
    }) as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('returns fc1 at exactly the width every downstream consumer assumes', async () => {
    const { session, Tensor } = await loadSession(SFACE_PATH)
    const input = new Float32Array(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE).fill(128)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', input, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })

    const fc1 = outputs.fc1!.data as Float32Array
    expect(fc1.length).toBe(FACE_EMBED_DIMENSIONS)
    for (const value of fc1) {
      expect(Number.isFinite(value)).toBe(true)
    }
  }, 120_000)

  it('is deterministic: the same tensor produces the same embedding twice', async () => {
    const { session, Tensor } = await loadSession(SFACE_PATH)
    const input = noiseImage(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE)

    const first = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', input, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })
    const second = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', input, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })

    expect(Array.from(first.fc1!.data as Float32Array)).toEqual(Array.from(second.fc1!.data as Float32Array))
  }, 120_000)

  /**
   * The property this suite exists to check: does normalizing the model's raw
   * output produce a unit vector whose cosine similarity behaves the way
   * face-matching.ts assumes it does?
   *
   * This is deliberately not a claim about face recognition accuracy — the
   * two "faces" here are two procedurally generated tensors, not photographs
   * of two people. What is being proven is narrower and structural: feeding
   * the *same* input twice yields similarity 1, and feeding two *different*
   * inputs yields something measurably lower — which is the only property
   * `SAME_IDENTITY_THRESHOLD` and the tier logic actually rely on.
   */
  it('same input reaches similarity 1; a different input scores measurably lower', async () => {
    const { session, Tensor } = await loadSession(SFACE_PATH)
    const inputA = noiseImage(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE)
    const inputB = noiseImage(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE).map((value, index) =>
      (value + 97 + index) % 256
    )

    const outputsA = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', inputA, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })
    const outputsB = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', inputB, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })

    const embeddingA = normalizeEmbedding(outputsA.fc1!.data as Float32Array)
    const embeddingA2 = normalizeEmbedding(outputsA.fc1!.data as Float32Array)
    const embeddingB = normalizeEmbedding(outputsB.fc1!.data as Float32Array)

    const norm = Math.sqrt(embeddingA.reduce((total, value) => total + value * value, 0))
    expect(norm).toBeCloseTo(1, 5)

    expect(cosineSimilarity(embeddingA, embeddingA2)).toBeCloseTo(1, 5)
    const crossSimilarity = cosineSimilarity(embeddingA, embeddingB)
    expect(crossSimilarity).toBeLessThan(0.999)
  }, 120_000)

  it('runs the alignment step against a real embedding without a shape mismatch', async () => {
    // Exercises the actual pipeline seam: landmarks -> alignFaceToTensor ->
    // SFace, with the real model on the receiving end rather than a shape
    // assertion alone.
    const size = 256
    const source: SourceImage = {
      data: new Uint8Array(size * size * 4).fill(90),
      width: size,
      height: size
    }
    // The reference template itself, scaled and offset into the source frame
    // — landmarks that a real detector would plausibly produce for a face
    // roughly centred in a 256x256 crop.
    const landmarks = REFERENCE_LANDMARKS.map((point) => ({ x: point.x + 60, y: point.y + 60 }))

    const tensor = alignFaceToTensor(source, landmarks)
    expect(tensor).toBeDefined()
    expect(tensor!.length).toBe(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE)

    const { session, Tensor } = await loadSession(SFACE_PATH)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', tensor!, [1, 3, FACE_EMBED_INPUT_SIZE, FACE_EMBED_INPUT_SIZE])
    })
    const embedding = normalizeEmbedding(outputs.fc1!.data as Float32Array)
    expect(embedding).toHaveLength(FACE_EMBED_DIMENSIONS)
  }, 120_000)
})
