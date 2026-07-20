import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { decodeForFaceDetection, letterbox } from './face-image'
import {
  FACE_BITMAP_BYTES,
  FACE_BITMAP_HEIGHT,
  FACE_BITMAP_WIDTH,
  MAX_FACE_SCORES,
  parseVisionCommand,
  parseVisionEvent,
  VisionProtocolError
} from './protocol'
import { PhotoDecodeError, type BoundedNativeImage, type PhotoFileSnapshot } from './scanner'

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-face-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

function fakeImage(width: number, height: number, fill = 0x7f): BoundedNativeImage {
  const bitmap = Buffer.alloc(width * height * 4, fill)
  return {
    isEmpty: () => width === 0 || height === 0,
    getSize: () => ({ width, height }),
    crop: () => fakeImage(width, height, fill),
    resize: () => fakeImage(width, height, fill),
    toBitmap: () => bitmap
  }
}

async function snapshotFor(width: number, height: number): Promise<PhotoFileSnapshot> {
  const absolutePath = join(directory, 'photo.jpg')
  await writeFile(absolutePath, 'metadata only')
  const { statSync } = await import('node:fs')
  const details = statSync(absolutePath)
  return {
    rootId: 'root-a',
    rootPath: directory,
    absolutePath,
    relativePath: 'photo.jpg',
    name: 'photo.jpg',
    mtimeMs: details.mtimeMs,
    sizeBytes: details.size,
    width,
    height
  }
}

describe('letterboxing preserves everyone in the frame', () => {
  it('always produces exactly the bitmap the model accepts', async () => {
    const snapshot = await snapshotFor(1920, 1080)
    const { bitmap } = await decodeForFaceDetection(snapshot, async (_path, size) =>
      fakeImage(size.width, size.height)
    )
    expect(bitmap.byteLength).toBe(FACE_BITMAP_BYTES)
  })

  it('scales to fit rather than cropping, so edge faces are not cut away', async () => {
    // A wide group photo centre-cropped to a square loses the people at both
    // ends, turning "five people" into "three" with no sign anything was lost.
    const snapshot = await snapshotFor(2000, 500)
    let requested: { width: number; height: number } | undefined
    await decodeForFaceDetection(snapshot, async (_path, size) => {
      requested = size
      return fakeImage(size.width, size.height)
    })

    expect(requested!.width).toBe(FACE_BITMAP_WIDTH)
    // Aspect preserved: 2000x500 is 4:1, so 640 wide gives 160 tall.
    expect(requested!.height).toBe(160)
  })

  it('never upscales a small photo', async () => {
    const snapshot = await snapshotFor(200, 150)
    const { scale } = await decodeForFaceDetection(snapshot, async (_path, size) =>
      fakeImage(size.width, size.height)
    )
    expect(scale).toBe(1)
  })

  it('pads with opaque zeroes rather than uninitialised memory', () => {
    const source = new Uint8Array(4 * 4 * 4).fill(0xab)
    const canvas = new Uint8Array(letterbox(source, 4, 4))

    // The first row of the image is present.
    expect(canvas[0]).toBe(0xab)
    // Everything beyond the image is zero, including the tail of row 0.
    expect(canvas[4 * 4]).toBe(0)
    expect(canvas[canvas.length - 1]).toBe(0)
  })

  it('places the image at the origin, so mapping back is a single scale', () => {
    const source = new Uint8Array(2 * 2 * 4).fill(0x11)
    const canvas = new Uint8Array(letterbox(source, 2, 2))
    expect(canvas[0]).toBe(0x11)
    expect(canvas[FACE_BITMAP_WIDTH * 4]).toBe(0x11) // start of row 1
    expect(canvas[FACE_BITMAP_WIDTH * 4 * 2]).toBe(0) // row 2 is padding
  })

  it('refuses a source larger than the canvas rather than overflowing it', () => {
    expect(() => letterbox(new Uint8Array(16), FACE_BITMAP_WIDTH + 1, 1)).toThrow(PhotoDecodeError)
  })
})

describe('face decoding fails safely', () => {
  it('refuses a file that changed since it was queued', async () => {
    const snapshot = await snapshotFor(800, 600)
    const stale = { ...snapshot, mtimeMs: snapshot.mtimeMs + 1_000 }
    await expect(decodeForFaceDetection(stale, async () => fakeImage(800, 600))).rejects.toMatchObject({
      code: 'not_a_real_file'
    })
  })

  it('refuses a deleted file', async () => {
    const snapshot = await snapshotFor(800, 600)
    await rm(snapshot.absolutePath)
    await expect(decodeForFaceDetection(snapshot, async () => fakeImage(640, 480))).rejects.toMatchObject({
      code: 'not_a_real_file'
    })
  })

  it('maps a locked file to its own bounded code', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForFaceDetection(snapshot, async () => {
        throw Object.assign(new Error('EPERM'), { code: 'EPERM' })
      })
    ).rejects.toMatchObject({ code: 'file_locked' })
  })

  it('rejects a decoder that returned a different size than requested', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForFaceDetection(snapshot, async () => fakeImage(FACE_BITMAP_WIDTH + 10, 10))
    ).rejects.toMatchObject({ code: 'decode_failed' })
  })

  it('rejects a bitmap inconsistent with its reported size', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForFaceDetection(snapshot, async () => ({ ...fakeImage(640, 480), toBitmap: () => Buffer.alloc(7) }))
    ).rejects.toMatchObject({ code: 'decode_failed' })
  })
})

describe('the detect_faces command is validated like every other', () => {
  const valid = {
    type: 'detect_faces',
    requestId: 'r1',
    width: FACE_BITMAP_WIDTH,
    height: FACE_BITMAP_HEIGHT,
    format: 'bgra',
    bitmap: new ArrayBuffer(FACE_BITMAP_BYTES)
  }

  it('accepts an exact, bounded bitmap', () => {
    expect(parseVisionCommand(valid)).toMatchObject({ type: 'detect_faces', requestId: 'r1' })
  })

  it.each([
    ['a wrong width', { width: 320 }],
    ['a wrong height', { height: 320 }],
    ['a wrong format', { format: 'rgba' }],
    ['a short bitmap', { bitmap: new ArrayBuffer(16) }],
    ['a long bitmap', { bitmap: new ArrayBuffer(FACE_BITMAP_BYTES + 4) }],
    ['a non-buffer bitmap', { bitmap: 'not a buffer' }],
    ['a missing request id', { requestId: '' }]
  ])('rejects %s', (_label, overrides) => {
    expect(() => parseVisionCommand({ ...valid, ...overrides })).toThrow(VisionProtocolError)
  })

  it('rejects an unexpected extra key', () => {
    expect(() => parseVisionCommand({ ...valid, modelPath: 'C:\\evil.onnx' })).toThrow(VisionProtocolError)
  })
})

describe('the face result carries scores and nothing else', () => {
  function resultWith(scores: number[]): Record<string, unknown> {
    return {
      type: 'face_result',
      requestId: 'r1',
      scores: Float32Array.from(scores).buffer,
      elapsedMs: 12,
      workerRssBytes: 1_000
    }
  }

  it('accepts a bounded list of probabilities', () => {
    const event = parseVisionEvent(resultWith([0.99, 0.7]))
    expect(event).toMatchObject({ type: 'face_result', requestId: 'r1' })
  })

  it('has no field capable of carrying a box or a landmark', () => {
    const event = parseVisionEvent(resultWith([0.95])) as unknown as Record<string, unknown>
    expect(Object.keys(event).sort()).toEqual(
      ['elapsedMs', 'requestId', 'scores', 'type', 'workerRssBytes'].sort()
    )
  })

  it('rejects an attempt to smuggle geometry alongside the scores', () => {
    expect(() =>
      parseVisionEvent({ ...resultWith([0.9]), boxes: [[1, 2, 3, 4]] })
    ).toThrow(VisionProtocolError)
  })

  it('rejects more scores than the ceiling allows', () => {
    const tooMany = Array.from({ length: MAX_FACE_SCORES + 1 }, () => 0.95)
    expect(() => parseVisionEvent(resultWith(tooMany))).toThrow(VisionProtocolError)
  })

  it.each([
    ['a score above one', [1.5]],
    ['a negative score', [-0.1]],
    ['a non-finite score', [Number.NaN]]
  ])('rejects %s rather than clamping it', (_label, scores) => {
    expect(() => parseVisionEvent(resultWith(scores))).toThrow(VisionProtocolError)
  })

  it('rejects a misaligned score buffer', () => {
    expect(() =>
      parseVisionEvent({ ...resultWith([0.9]), scores: new ArrayBuffer(7) })
    ).toThrow(VisionProtocolError)
  })
})
