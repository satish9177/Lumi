/**
 * Real ONNX Runtime inference against the actual model pack.
 *
 * Everything else in this directory is unit-tested with mocks. This file is the
 * one place that proves the pieces agree in reality: that our reimplemented CLIP
 * tokenizer, the text tower, and the image tower land in the *same* embedding
 * space. A tokenizer that is subtly wrong, or pooling at the wrong position,
 * still yields finite 512-d vectors — it just yields meaningless ones — so
 * shape checks alone cannot catch it. Only relative similarity can.
 *
 * Skipped when the pack is not installed, so the suite stays runnable offline
 * and on a clean checkout.
 */

import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { MODEL_ASSETS } from './manifest'
import { bgraToClipTensor, CLIP_TENSOR_DIMS, normalizedEmbedding } from './preprocess'
import { CLIP_CONTEXT_LENGTH, VISION_BITMAP_BYTES, VISION_BITMAP_HEIGHT, VISION_BITMAP_WIDTH } from './protocol'
import { createClipTokenizer } from './tokenizer'

/**
 * The developer's own userData pack. Resolved here rather than through
 * model-location so this file does not need an Electron app instance.
 */
const PACK_DIR = join(
  process.env.APPDATA ?? join(process.env.USERPROFILE ?? '', 'AppData', 'Roaming'),
  'lifelens',
  'vision-models',
  'clip-vit-base-patch32-q8'
)

const assetPath = (fileName: string): string => join(PACK_DIR, fileName)
const packInstalled = MODEL_ASSETS.every((asset) => existsSync(assetPath(asset.fileName)))

type Session = {
  inputNames: readonly string[]
  outputNames: readonly string[]
  run: (feeds: Record<string, unknown>) => Promise<Record<string, { data: unknown }>>
}

async function loadRuntime() {
  const imported = (await import('onnxruntime-node')) as unknown as Record<string, unknown>
  return (imported.default ?? imported) as {
    InferenceSession: { create: (path: string, options?: unknown) => Promise<Session> }
    Tensor: new (type: string, data: Float32Array | BigInt64Array, dims: readonly number[]) => unknown
  }
}

function solidBitmap(blue: number, green: number, red: number): ArrayBuffer {
  const bytes = new Uint8Array(VISION_BITMAP_BYTES)
  for (let offset = 0; offset < bytes.length; offset += 4) {
    bytes[offset] = blue
    bytes[offset + 1] = green
    bytes[offset + 2] = red
    bytes[offset + 3] = 255
  }
  return bytes.buffer
}

/** A crude two-band image: sky over ground. */
function bandedBitmap(top: [number, number, number], bottom: [number, number, number]): ArrayBuffer {
  const bytes = new Uint8Array(VISION_BITMAP_BYTES)
  const horizon = Math.floor(VISION_BITMAP_HEIGHT / 2)
  for (let y = 0; y < VISION_BITMAP_HEIGHT; y += 1) {
    const [blue, green, red] = y < horizon ? top : bottom
    for (let x = 0; x < VISION_BITMAP_WIDTH; x += 1) {
      const offset = (y * VISION_BITMAP_WIDTH + x) * 4
      bytes[offset] = blue
      bytes[offset + 1] = green
      bytes[offset + 2] = red
      bytes[offset + 3] = 255
    }
  }
  return bytes.buffer
}

function dot(left: Float32Array, right: Float32Array): number {
  let total = 0
  for (let index = 0; index < left.length; index += 1) {
    total += left[index]! * right[index]!
  }
  return total
}

describe.skipIf(!packInstalled)('real CLIP inference', () => {
  const tokenizer = createClipTokenizer(
    readFileSync(assetPath('vocab.json'), 'utf8'),
    readFileSync(assetPath('merges.txt'), 'utf8')
  )

  let textSession: Session | undefined
  let imageSession: Session | undefined
  let ort: Awaited<ReturnType<typeof loadRuntime>> | undefined

  async function embedText(prompt: string): Promise<Float32Array> {
    ort ??= await loadRuntime()
    textSession ??= await ort.InferenceSession.create(assetPath('text_model_quantized.onnx'), {
      executionProviders: ['cpu']
    })

    const { tokenIds, tokenCount } = tokenizer.encode(prompt)
    const ids = new BigInt64Array(CLIP_CONTEXT_LENGTH)
    const mask = new BigInt64Array(CLIP_CONTEXT_LENGTH)
    for (let index = 0; index < CLIP_CONTEXT_LENGTH; index += 1) {
      ids[index] = BigInt(tokenIds[index]!)
      mask[index] = index < tokenCount ? 1n : 0n
    }

    const dims = [1, CLIP_CONTEXT_LENGTH]
    const feeds: Record<string, unknown> = { input_ids: new ort.Tensor('int64', ids, dims) }
    if (textSession.inputNames.includes('attention_mask')) {
      feeds.attention_mask = new ort.Tensor('int64', mask, dims)
    }

    const outputs = await textSession.run(feeds)
    return normalizedEmbedding((outputs.text_embeds ?? outputs[textSession.outputNames[0]!])!.data)
  }

  async function embedImage(bitmap: ArrayBuffer): Promise<Float32Array> {
    ort ??= await loadRuntime()
    imageSession ??= await ort.InferenceSession.create(assetPath('vision_model_quantized.onnx'), {
      executionProviders: ['cpu']
    })

    const tensor = new ort.Tensor('float32', bgraToClipTensor(new Uint8Array(bitmap)), CLIP_TENSOR_DIMS)
    const outputs = await imageSession.run({ pixel_values: tensor })
    return normalizedEmbedding((outputs.image_embeds ?? outputs[imageSession.outputNames[0]!])!.data)
  }

  it('produces a unit-length 512-d vector from the text tower', async () => {
    const vector = await embedText('a photo of a cat')

    expect(vector).toHaveLength(512)
    expect(Math.sqrt(dot(vector, vector))).toBeCloseTo(1, 4)
  }, 120_000)

  it('produces a unit-length 512-d vector from the image tower', async () => {
    const vector = await embedImage(solidBitmap(200, 120, 60))

    expect(vector).toHaveLength(512)
    expect(Math.sqrt(dot(vector, vector))).toBeCloseTo(1, 4)
  }, 120_000)

  it('is deterministic for the same input', async () => {
    const [first, second] = await Promise.all([embedText('a photo of beach'), embedText('a photo of beach')])
    expect([...first]).toEqual([...second])
  }, 120_000)

  it('places related concepts closer than unrelated ones', async () => {
    // If the tokenizer or the pooling position were wrong, these vectors would
    // still be finite and unit length, but this ordering would not hold.
    const cat = await embedText('a photo of a cat')
    const kitten = await embedText('a photo of a kitten')
    const airplane = await embedText('a photo of an airplane')

    expect(dot(cat, kitten)).toBeGreaterThan(dot(cat, airplane))
  }, 120_000)

  it('separates distinct concepts rather than collapsing everything together', async () => {
    const beach = await embedText('a photo of beach')
    const document = await embedText('a photo of document')

    // Degenerate embeddings tend to be near-identical for every prompt.
    expect(dot(beach, document)).toBeLessThan(0.95)
  }, 120_000)

  it('aligns image and text embeddings in one shared space', async () => {
    // A blue-over-sand banded image should sit nearer "beach" than "grass",
    // and a green field nearer "grass" than "beach". This is the property the
    // whole feature rests on: that a text query can rank image vectors at all.
    const beachLike = await embedImage(bandedBitmap([220, 170, 90], [150, 210, 235]))
    const grassLike = await embedImage(solidBitmap(60, 160, 70))

    const beachText = await embedText('a photo of a beach')
    const grassText = await embedText('a photo of green grass')

    expect(dot(beachLike, beachText)).toBeGreaterThan(dot(beachLike, grassText))
    expect(dot(grassLike, grassText)).toBeGreaterThan(dot(grassLike, beachText))
  }, 180_000)

  it('keeps cross-modal similarities in CLIP\'s usual range, not pinned at an extreme', async () => {
    const image = await embedImage(solidBitmap(150, 150, 150))
    const text = await embedText('a photo of beach')
    const similarity = dot(image, text)

    // CLIP image/text cosines cluster loosely around 0.1-0.35; a value near 0
    // or near 1 would mean the two towers are not sharing a space.
    expect(similarity).toBeGreaterThan(0)
    expect(similarity).toBeLessThan(0.6)
  }, 180_000)
})

describe.skipIf(packInstalled)('real CLIP inference (skipped)', () => {
  it('reports that the model pack is not installed on this machine', () => {
    expect(packInstalled).toBe(false)
  })
})
