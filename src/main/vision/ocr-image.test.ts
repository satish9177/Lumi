import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { inflateSync } from 'node:zlib'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bgraToGreyscale, decodeForOcr, encodeGreyscalePng, OCR_MAX_EDGE } from './ocr-image'
import { PhotoDecodeError, type BoundedNativeImage, type PhotoFileSnapshot } from './scanner'

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-ocr-image-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

/** A decoder that honours the requested size and reports a solid BGRA image. */
function fakeImage(width: number, height: number, fill = 0x40): BoundedNativeImage {
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
  const absolutePath = join(directory, 'photo.png')
  await writeFile(absolutePath, 'not really a png, only the metadata is read here')
  const { statSync } = await import('node:fs')
  const details = statSync(absolutePath)
  return {
    rootId: 'root-a',
    rootPath: directory,
    absolutePath,
    relativePath: 'photo.png',
    name: 'photo.png',
    mtimeMs: details.mtimeMs,
    sizeBytes: details.size,
    width,
    height
  }
}

/** Minimal PNG reader, so the encoder is checked against a real parse. */
function readPng(png: Buffer): { width: number; height: number; bitDepth: number; colourType: number; pixels: Buffer } {
  expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

  let offset = 8
  let header: { width: number; height: number; bitDepth: number; colourType: number } | undefined
  const idat: Buffer[] = []
  let sawEnd = false

  while (offset < png.length) {
    const length = png.readUInt32BE(offset)
    const type = png.toString('ascii', offset + 4, offset + 8)
    const data = png.subarray(offset + 8, offset + 8 + length)
    if (type === 'IHDR') {
      header = {
        width: data.readUInt32BE(0),
        height: data.readUInt32BE(4),
        bitDepth: data[8]!,
        colourType: data[9]!
      }
    } else if (type === 'IDAT') {
      idat.push(data)
    } else if (type === 'IEND') {
      sawEnd = true
    }
    offset += 12 + length
  }

  expect(sawEnd).toBe(true)
  expect(header).toBeDefined()

  const raw = inflateSync(Buffer.concat(idat))
  const { width, height } = header!
  const pixels = Buffer.alloc(width * height)
  for (let y = 0; y < height; y += 1) {
    // Every row must use filter type 0, or the pixel copy below is wrong.
    expect(raw[y * (width + 1)]).toBe(0)
    raw.copy(pixels, y * width, y * (width + 1) + 1, (y + 1) * (width + 1))
  }
  return { ...header!, pixels }
}

describe('greyscale conversion', () => {
  it('applies Rec. 601 luma weights to BGRA input', () => {
    // Pure red, green, blue, and white, in BGRA order.
    const bitmap = new Uint8Array([
      0, 0, 255, 255,
      0, 255, 0, 255,
      255, 0, 0, 255,
      255, 255, 255, 255
    ])
    const grey = bgraToGreyscale(bitmap, 4, 1)
    expect(grey[0]).toBeCloseTo(76, -1) // red
    expect(grey[1]).toBeCloseTo(150, -1) // green
    expect(grey[2]).toBeCloseTo(29, -1) // blue
    expect(grey[3]).toBe(255) // white
  })

  it('produces exactly one byte per pixel', () => {
    expect(bgraToGreyscale(new Uint8Array(16 * 9 * 4), 16, 9).length).toBe(16 * 9)
  })
})

describe('PNG encoding', () => {
  it('produces a PNG a parser can read back byte for byte', () => {
    const width = 17
    const height = 5
    const pixels = new Uint8Array(width * height)
    for (let index = 0; index < pixels.length; index += 1) {
      pixels[index] = (index * 7) % 256
    }

    const decoded = readPng(encodeGreyscalePng(pixels, width, height))
    expect(decoded.width).toBe(width)
    expect(decoded.height).toBe(height)
    expect(decoded.bitDepth).toBe(8)
    expect(decoded.colourType).toBe(0) // greyscale
    expect([...decoded.pixels]).toEqual([...pixels])
  })

  it('is deterministic, so the same image always encodes identically', () => {
    const pixels = new Uint8Array(64).fill(9)
    expect(encodeGreyscalePng(pixels, 8, 8).equals(encodeGreyscalePng(pixels, 8, 8))).toBe(true)
  })

  it('handles a single-pixel image', () => {
    expect(readPng(encodeGreyscalePng(new Uint8Array([200]), 1, 1)).pixels[0]).toBe(200)
  })
})

describe('decoding an image for OCR', () => {
  it('keeps a screenshot near native resolution', async () => {
    const snapshot = await snapshotFor(1920, 1080)
    let requested: { width: number; height: number } | undefined
    const image = await decodeForOcr(snapshot, async (_path, size) => {
      requested = size
      return fakeImage(size.width, size.height)
    })

    expect(requested).toEqual({ width: 1600, height: 900 })
    expect(image.width).toBe(1600)
    expect(readPng(image.png).width).toBe(1600)
  })

  it('never upscales a small image, which would invent detail to misread', async () => {
    const snapshot = await snapshotFor(300, 200)
    const image = await decodeForOcr(snapshot, async (_path, size) => fakeImage(size.width, size.height))
    expect(image.width).toBe(300)
    expect(image.height).toBe(200)
  })

  it('preserves aspect ratio when bounding a very wide image', async () => {
    const snapshot = await snapshotFor(4000, 1000)
    const image = await decodeForOcr(snapshot, async (_path, size) => fakeImage(size.width, size.height))
    expect(Math.max(image.width, image.height)).toBe(OCR_MAX_EDGE)
    expect(image.width / image.height).toBeCloseTo(4, 1)
  })

  it('rejects an image too small to hold readable text', async () => {
    const snapshot = await snapshotFor(8, 8)
    await expect(decodeForOcr(snapshot, async () => fakeImage(8, 8))).rejects.toBeInstanceOf(PhotoDecodeError)
  })

  it('fails safely when the decoder returns an empty image', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForOcr(snapshot, async () => ({ ...fakeImage(0, 0), isEmpty: () => true }))
    ).rejects.toMatchObject({ code: 'decode_failed' })
  })

  it('fails safely when the bitmap does not match the reported size', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForOcr(snapshot, async () => ({ ...fakeImage(800, 600), toBitmap: () => Buffer.alloc(10) }))
    ).rejects.toMatchObject({ code: 'decode_failed' })
  })

  it('maps a locked file to its own bounded code', async () => {
    const snapshot = await snapshotFor(800, 600)
    await expect(
      decodeForOcr(snapshot, async () => {
        throw Object.assign(new Error('EBUSY'), { code: 'EBUSY' })
      })
    ).rejects.toMatchObject({ code: 'file_locked' })
  })

  it('refuses a file that changed since it was queued', async () => {
    const snapshot = await snapshotFor(800, 600)
    const stale = { ...snapshot, sizeBytes: snapshot.sizeBytes + 1 }
    await expect(decodeForOcr(stale, async () => fakeImage(800, 600))).rejects.toMatchObject({
      code: 'not_a_real_file'
    })
  })

  it('refuses a file that has been deleted', async () => {
    const snapshot = await snapshotFor(800, 600)
    await rm(snapshot.absolutePath)
    await expect(decodeForOcr(snapshot, async () => fakeImage(800, 600))).rejects.toMatchObject({
      code: 'not_a_real_file'
    })
  })
})
