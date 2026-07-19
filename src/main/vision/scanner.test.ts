import { mkdtemp, mkdir, rm, symlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'
import { decodePhotoSnapshot, MAX_PHOTO_PIXELS, parseImageDimensions, scanApprovedPhotos, type BoundedNativeImage } from './scanner'

const temporary: string[] = []
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))) })

describe('safe photo scanner', () => {
  it('indexes only supported regular images inside the approved root', async () => {
    const root = await tempRoot()
    await writeFile(join(root, 'one.png'), pngHeader(10, 20))
    await writeFile(join(root, 'two.jpg'), jpegHeader(20, 10))
    await writeFile(join(root, 'three.webp'), webpHeader(12, 9))
    await writeFile(join(root, 'ignore.gif'), Buffer.from('GIF89a'))
    await writeFile(join(root, 'ignore.txt'), 'not a photo')
    const result = await scanApprovedPhotos([{ id: 'root', canonicalPath: root, label: 'Photos' }])
    expect(result.files.map((file) => file.name)).toEqual(['one.png', 'three.webp', 'two.jpg'])
    expect(result.files.every((file) => !file.relativePath.includes(root))).toBe(true)
  })

  it('rejects malformed and over-50-megapixel headers before decode', async () => {
    expect(() => parseImageDimensions(Buffer.from('not an image'))).toThrow()
    const root = await tempRoot()
    await writeFile(join(root, 'bomb.png'), pngHeader(10_000, Math.floor(MAX_PHOTO_PIXELS / 10_000) + 1))
    const result = await scanApprovedPhotos([{ id: 'root', canonicalPath: root, label: 'Photos' }])
    expect(result.files).toHaveLength(0)
    expect(result.failures[0]?.code).toBe('too_many_pixels')
  })

  it('does not follow symlinked files or directories', async () => {
    const root = await tempRoot()
    const outside = await tempRoot()
    await writeFile(join(outside, 'outside.png'), pngHeader(10, 10))
    try {
      await symlink(join(outside, 'outside.png'), join(root, 'linked.png'), 'file')
      await symlink(outside, join(root, 'linked-dir'), 'junction')
    } catch {
      return // Host policy may prohibit creating links; production check remains covered by code review/Windows tests.
    }
    const result = await scanApprovedPhotos([{ id: 'root', canonicalPath: root, label: 'Photos' }])
    expect(result.files).toHaveLength(0)
  })

  it('uses resize-shortest-side then an exact centered 224 crop', async () => {
    const root = await tempRoot()
    const path = join(root, 'wide.jpg')
    await writeFile(path, jpegHeader(400, 200))
    const stats = await import('node:fs/promises').then(({ stat }) => stat(path))
    let requested: { width: number; height: number } | undefined
    let crop: { x: number; y: number; width: number; height: number } | undefined
    const image = fakeImage(448, 224, (rect) => { crop = rect })
    const decoded = await decodePhotoSnapshot({
      rootId: 'root', rootPath: root, absolutePath: path, relativePath: 'wide.jpg', name: 'wide.jpg',
      mtimeMs: stats.mtimeMs, sizeBytes: stats.size, width: 400, height: 200
    }, async (_path, size) => { requested = size; return image })
    expect(requested).toEqual({ width: 448, height: 224 })
    expect(crop).toEqual({ x: 112, y: 0, width: 224, height: 224 })
    expect(decoded.bitmap.byteLength).toBe(224 * 224 * 4)
  })
})

async function tempRoot(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), 'lifelens-scanner-'))
  temporary.push(path)
  await mkdir(path, { recursive: true })
  return path
}

function pngHeader(width: number, height: number): Buffer {
  const bytes = Buffer.alloc(24)
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(bytes)
  bytes.write('IHDR', 12, 'ascii')
  bytes.writeUInt32BE(width, 16)
  bytes.writeUInt32BE(height, 20)
  return bytes
}

function jpegHeader(width: number, height: number): Buffer {
  return Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x11, 0x08, height >> 8, height & 0xff, width >> 8, width & 0xff, 3, 1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0, 0xff, 0xd9])
}

function webpHeader(width: number, height: number): Buffer {
  const bytes = Buffer.alloc(30)
  bytes.write('RIFF', 0, 'ascii')
  bytes.writeUInt32LE(22, 4)
  bytes.write('WEBPVP8X', 8, 'ascii')
  bytes.writeUIntLE(width - 1, 24, 3)
  bytes.writeUIntLE(height - 1, 27, 3)
  return bytes
}

function fakeImage(width: number, height: number, onCrop: (rect: { x: number; y: number; width: number; height: number }) => void): BoundedNativeImage {
  const image: BoundedNativeImage = {
    isEmpty: () => false,
    getSize: () => ({ width, height }),
    crop: (rect) => { onCrop(rect); return fakeImage(rect.width, rect.height, () => undefined) },
    resize: ({ width: nextWidth }) => fakeImage(nextWidth, Math.round(height * nextWidth / width), onCrop),
    toBitmap: () => Buffer.alloc(width * height * 4)
  }
  return image
}
