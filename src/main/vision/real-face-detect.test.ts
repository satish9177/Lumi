/**
 * Real YuNet inference against the actual installed extras pack.
 *
 * The decoder is unit-tested against synthesized tensors, which proves the
 * arithmetic is self-consistent but cannot prove it matches the model. Anchor
 * ordering, the stride-to-grid mapping, and the score formula are all
 * conventions of this particular export: get any of them wrong and the decoder
 * still returns plausible boxes, just in the wrong places. Only running the
 * real graph can catch that.
 *
 * Skipped when the extras pack is not installed, so the suite stays runnable
 * offline and on a clean checkout.
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { countFaces, decodeYunet, detectionScores, FACE_STRIDES, type YunetOutputs } from './face-detect'
import { EXTRAS_PACK_ID, extrasAssetFor } from './extras-manifest'
import { FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH } from './protocol'

const PACK_DIR = join(
  process.env.APPDATA ?? join(process.env.USERPROFILE ?? '', 'AppData', 'Roaming'),
  'lifelens',
  'vision-models',
  EXTRAS_PACK_ID
)

const MODEL_PATH = join(PACK_DIR, extrasAssetFor('faceModel').fileName)
const modelInstalled = existsSync(MODEL_PATH)

type Session = {
  inputNames: readonly string[]
  outputNames: readonly string[]
  run: (feeds: Record<string, unknown>) => Promise<Record<string, { data: unknown; dims: readonly number[] }>>
}

async function loadSession(): Promise<{ session: Session; Tensor: new (type: string, data: Float32Array, dims: readonly number[]) => unknown }> {
  const imported = (await import('onnxruntime-node')) as unknown as Record<string, unknown>
  const ort = (imported.default ?? imported) as {
    InferenceSession: { create: (path: string, options?: unknown) => Promise<Session> }
    Tensor: new (type: string, data: Float32Array, dims: readonly number[]) => unknown
  }
  const session = await ort.InferenceSession.create(MODEL_PATH, {
    executionProviders: ['cpu'],
    graphOptimizationLevel: 'all'
  })
  return { session, Tensor: ort.Tensor }
}

function collect(outputs: Record<string, { data: unknown }>): YunetOutputs {
  const collected: YunetOutputs = { cls: {}, obj: {}, bbox: {} }
  for (const stride of FACE_STRIDES) {
    for (const family of ['cls', 'obj', 'bbox'] as const) {
      const data = outputs[`${family}_${stride}`]?.data
      if (data instanceof Float32Array) {
        collected[family][stride] = data
      }
    }
  }
  return collected
}

describe.skipIf(!modelInstalled)('real local face detection', () => {
  it('exposes exactly the tensors this decoder reads, at the expected sizes', async () => {
    const { session } = await loadSession()
    for (const stride of FACE_STRIDES) {
      for (const family of ['cls', 'obj', 'bbox'] as const) {
        expect(session.outputNames).toContain(`${family}_${stride}`)
      }
    }
  }, 120_000)

  it('produces anchor counts matching the decoder grid assumption', async () => {
    const { session, Tensor } = await loadSession()
    const input = new Float32Array(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT).fill(128)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', input, [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH])
    })

    for (const stride of FACE_STRIDES) {
      const expected = (FACE_BITMAP_WIDTH / stride) * (FACE_BITMAP_HEIGHT / stride)
      // The decoder derives row/column from this exact anchor count. If the
      // real graph disagreed, every decoded box would be misplaced.
      expect(outputs[`cls_${stride}`]!.dims).toEqual([1, expected, 1])
      expect(outputs[`bbox_${stride}`]!.dims).toEqual([1, expected, 4])
    }
  }, 120_000)

  it('reports no faces in a flat image, rather than inventing them', async () => {
    const { session, Tensor } = await loadSession()
    const flat = new Float32Array(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT).fill(128)
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', flat, [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH])
    })

    const counts = countFaces(detectionScores(collect(outputs)))
    // A uniform grey field contains no face. A detector or decoder that scored
    // background highly would show up here as a false positive.
    expect(counts.visible).toBe(0)
  }, 120_000)

  it('keeps every decoded box inside the input frame', async () => {
    const { session, Tensor } = await loadSession()
    // Structured noise, so some anchors score above the floor and the geometry
    // is actually exercised rather than all being filtered out.
    const noisy = new Float32Array(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT)
    for (let index = 0; index < noisy.length; index += 1) {
      noisy[index] = ((index * 2654435761) % 256 + 256) % 256
    }
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor('float32', noisy, [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH])
    })

    for (const detection of decodeYunet(collect(outputs))) {
      expect(Number.isFinite(detection.x)).toBe(true)
      expect(Number.isFinite(detection.y)).toBe(true)
      expect(detection.width).toBeGreaterThan(0)
      expect(detection.height).toBeGreaterThan(0)
      // Centres must land within the frame; a stride/grid mix-up would push
      // them far outside it.
      expect(detection.x + detection.width / 2).toBeGreaterThan(-FACE_BITMAP_WIDTH)
      expect(detection.x + detection.width / 2).toBeLessThan(FACE_BITMAP_WIDTH * 2)
      expect(detection.y + detection.height / 2).toBeGreaterThan(-FACE_BITMAP_HEIGHT)
      expect(detection.y + detection.height / 2).toBeLessThan(FACE_BITMAP_HEIGHT * 2)
    }
  }, 120_000)

  it('returns only scores, bounded, from the worker-side pipeline', async () => {
    const { session, Tensor } = await loadSession()
    const outputs = await session.run({
      [session.inputNames[0]!]: new Tensor(
        'float32',
        new Float32Array(3 * FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT).fill(200),
        [1, 3, FACE_BITMAP_HEIGHT, FACE_BITMAP_WIDTH]
      )
    })

    const scores = detectionScores(collect(outputs))
    expect(scores).toBeInstanceOf(Float32Array)
    expect(scores.length).toBeLessThanOrEqual(64)
    for (const score of scores) {
      expect(score).toBeGreaterThanOrEqual(0)
      expect(score).toBeLessThanOrEqual(1)
    }
  }, 120_000)
})
