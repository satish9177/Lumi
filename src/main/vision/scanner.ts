import type { Dirent, Stats } from 'node:fs'
import { lstat, open, readdir, realpath } from 'node:fs/promises'
import { basename, extname, join, relative, resolve, sep } from 'node:path'
import { isPathWithinRoot } from '../../features/document-tools/search'
import type { IndexFailureCode } from './index-store'
import { VISION_BITMAP_HEIGHT, VISION_BITMAP_WIDTH } from './protocol'

export const PHOTO_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.webp'])
export const MAX_PHOTO_FILE_BYTES = 100 * 1024 * 1024
export const MAX_PHOTO_PIXELS = 50_000_000
const MAX_HEADER_BYTES = 1024 * 1024
const MAX_SCAN_ENTRIES = 200_000
const MAX_SCAN_DEPTH = 8
const MAX_INTERMEDIATE_EDGE = 4096

export interface PhotoScanRoot {
  id: string
  canonicalPath: string
  label: string
}

export interface PhotoFileSnapshot {
  rootId: string
  rootPath: string
  absolutePath: string
  relativePath: string
  name: string
  mtimeMs: number
  sizeBytes: number
  width: number
  height: number
}

export interface ScanFailure {
  rootId: string
  relativePath: string
  name: string
  mtimeMs: number
  sizeBytes: number
  code: IndexFailureCode
}

export interface PhotoScanResult {
  files: PhotoFileSnapshot[]
  failures: ScanFailure[]
  truncated: boolean
}

export interface BoundedNativeImage {
  isEmpty: () => boolean
  getSize: () => { width: number; height: number }
  crop: (rect: { x: number; y: number; width: number; height: number }) => BoundedNativeImage
  resize: (options: { width: number; height?: number }) => BoundedNativeImage
  toBitmap: () => Buffer
}

export type ThumbnailDecoder = (
  path: string,
  size: { width: number; height: number }
) => Promise<BoundedNativeImage>

export interface DecodedPhoto {
  bitmap: ArrayBuffer
  width: typeof VISION_BITMAP_WIDTH
  height: typeof VISION_BITMAP_HEIGHT
}

export class PhotoDecodeError extends Error {
  constructor(readonly code: IndexFailureCode) {
    super(code)
    this.name = 'PhotoDecodeError'
  }
}

/** Enumerates only regular, non-link JPEG/PNG/WebP files under live roots. */
export async function scanApprovedPhotos(roots: readonly PhotoScanRoot[]): Promise<PhotoScanResult> {
  const result: PhotoScanResult = { files: [], failures: [], truncated: false }
  const budget = { visited: 0 }
  for (const root of roots) {
    await walk(root.canonicalPath, root, 0, budget, result)
    if (budget.visited >= MAX_SCAN_ENTRIES) {
      result.truncated = true
      break
    }
  }
  result.files.sort((a, b) => comparePath(a.relativePath, b.relativePath))
  return result
}

async function walk(
  directory: string,
  root: PhotoScanRoot,
  depth: number,
  budget: { visited: number },
  result: PhotoScanResult
): Promise<void> {
  if (budget.visited >= MAX_SCAN_ENTRIES || depth > MAX_SCAN_DEPTH) return
  if (!(await isDirectPath(directory)) || !isPathWithinRoot(directory, root.canonicalPath)) return

  let entries: Dirent[]
  try {
    entries = await readdir(directory, { withFileTypes: true })
  } catch {
    return
  }

  for (const entry of [...entries].sort((a, b) => comparePath(a.name, b.name))) {
    if (budget.visited++ >= MAX_SCAN_ENTRIES) {
      result.truncated = true
      return
    }
    if (entry.isSymbolicLink()) continue
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      if (depth < MAX_SCAN_DEPTH && !entry.name.startsWith('.')) await walk(path, root, depth + 1, budget, result)
      continue
    }
    if (!entry.isFile() || !PHOTO_EXTENSIONS.has(extname(entry.name).toLocaleLowerCase('en-US'))) continue
    const described = await describePhoto(path, root)
    if (described && 'code' in described) result.failures.push(described)
    else if (described) result.files.push(described)
  }
}

async function describePhoto(path: string, root: PhotoScanRoot): Promise<PhotoFileSnapshot | ScanFailure | undefined> {
  let details: Stats
  try {
    details = await lstat(path)
  } catch {
    return undefined
  }
  if (!details.isFile() || !(await isDirectPath(path))) return undefined
  const relativePath = stableRelative(root.canonicalPath, path)
  if (!relativePath || !isPathWithinRoot(path, root.canonicalPath)) return undefined
  const base = {
    rootId: root.id,
    relativePath,
    name: basename(path),
    mtimeMs: details.mtimeMs,
    sizeBytes: details.size
  }
  if (details.size <= 0 || details.size > MAX_PHOTO_FILE_BYTES) return { ...base, code: 'too_large' }
  try {
    const dimensions = await readImageDimensions(path, details.size)
    if (dimensions.width <= 0 || dimensions.height <= 0) return { ...base, code: 'unsupported_format' }
    if (dimensions.width * dimensions.height > MAX_PHOTO_PIXELS) return { ...base, code: 'too_many_pixels' }
    return { ...base, ...dimensions, rootPath: root.canonicalPath, absolutePath: path }
  } catch (error) {
    return { ...base, code: error instanceof PhotoDecodeError ? error.code : 'decode_failed' }
  }
}

/** Revalidates the queued snapshot, decodes a bounded thumbnail, then center-crops. */
export async function decodePhotoSnapshot(snapshot: PhotoFileSnapshot, decode: ThumbnailDecoder): Promise<DecodedPhoto> {
  const current = await revalidateSnapshot(snapshot)
  if (!current) throw new PhotoDecodeError('not_a_real_file')

  const scale = Math.max(VISION_BITMAP_WIDTH / snapshot.width, VISION_BITMAP_HEIGHT / snapshot.height)
  const targetWidth = Math.max(VISION_BITMAP_WIDTH, Math.round(snapshot.width * scale))
  const targetHeight = Math.max(VISION_BITMAP_HEIGHT, Math.round(snapshot.height * scale))
  if (targetWidth > MAX_INTERMEDIATE_EDGE || targetHeight > MAX_INTERMEDIATE_EDGE) {
    throw new PhotoDecodeError('too_many_pixels')
  }

  let image: BoundedNativeImage
  try {
    image = await decode(snapshot.absolutePath, { width: targetWidth, height: targetHeight })
  } catch (error) {
    if (isNodeError(error, 'EACCES') || isNodeError(error, 'EBUSY') || isNodeError(error, 'EPERM')) {
      throw new PhotoDecodeError('file_locked')
    }
    throw new PhotoDecodeError('decode_failed')
  }
  if (!image || image.isEmpty()) throw new PhotoDecodeError('decode_failed')

  let size = image.getSize()
  if (size.width < VISION_BITMAP_WIDTH || size.height < VISION_BITMAP_HEIGHT) {
    const rescale = Math.max(VISION_BITMAP_WIDTH / size.width, VISION_BITMAP_HEIGHT / size.height)
    image = image.resize({ width: Math.round(size.width * rescale) })
    size = image.getSize()
  }
  const cropped = image.crop({
    x: Math.max(0, Math.floor((size.width - VISION_BITMAP_WIDTH) / 2)),
    y: Math.max(0, Math.floor((size.height - VISION_BITMAP_HEIGHT) / 2)),
    width: VISION_BITMAP_WIDTH,
    height: VISION_BITMAP_HEIGHT
  })
  const bitmap = cropped.toBitmap()
  const expected = VISION_BITMAP_WIDTH * VISION_BITMAP_HEIGHT * 4
  if (bitmap.byteLength !== expected) throw new PhotoDecodeError('decode_failed')
  return {
    bitmap: bitmap.buffer.slice(bitmap.byteOffset, bitmap.byteOffset + bitmap.byteLength) as ArrayBuffer,
    width: VISION_BITMAP_WIDTH,
    height: VISION_BITMAP_HEIGHT
  }
}

export async function revalidateSnapshot(snapshot: PhotoFileSnapshot): Promise<boolean> {
  try {
    const details = await lstat(snapshot.absolutePath)
    return details.isFile() &&
      await isDirectPath(snapshot.absolutePath) &&
      isPathWithinRoot(snapshot.absolutePath, snapshot.rootPath) &&
      details.size === snapshot.sizeBytes &&
      details.mtimeMs === snapshot.mtimeMs
  } catch {
    return false
  }
}

export async function readImageDimensions(path: string, sizeBytes: number): Promise<{ width: number; height: number }> {
  const length = Math.min(sizeBytes, MAX_HEADER_BYTES)
  const handle = await open(path, 'r')
  try {
    const buffer = Buffer.alloc(length)
    const { bytesRead } = await handle.read(buffer, 0, length, 0)
    return parseImageDimensions(buffer.subarray(0, bytesRead))
  } finally {
    await handle.close()
  }
}

export function parseImageDimensions(buffer: Uint8Array): { width: number; height: number } {
  const bytes = Buffer.from(buffer.buffer, buffer.byteOffset, buffer.byteLength)
  if (bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    if (bytes.toString('ascii', 12, 16) !== 'IHDR') throw new PhotoDecodeError('unsupported_format')
    return dimensions(bytes.readUInt32BE(16), bytes.readUInt32BE(20))
  }
  if (bytes.length >= 12 && bytes.toString('ascii', 0, 4) === 'RIFF' && bytes.toString('ascii', 8, 12) === 'WEBP') {
    return parseWebp(bytes)
  }
  if (bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8) return parseJpeg(bytes)
  throw new PhotoDecodeError('unsupported_format')
}

function parseJpeg(bytes: Buffer): { width: number; height: number } {
  let offset = 2
  while (offset + 4 <= bytes.length) {
    if (bytes[offset] !== 0xff) { offset += 1; continue }
    let marker = bytes[offset + 1]!
    while (marker === 0xff && offset + 2 < bytes.length) marker = bytes[++offset + 1]!
    if (marker === 0xd9 || marker === 0xda) break
    if (marker >= 0xd0 && marker <= 0xd7) { offset += 2; continue }
    const segmentLength = bytes.readUInt16BE(offset + 2)
    if (segmentLength < 2 || offset + 2 + segmentLength > bytes.length) break
    if ((marker >= 0xc0 && marker <= 0xc3) || (marker >= 0xc5 && marker <= 0xc7) || (marker >= 0xc9 && marker <= 0xcb) || (marker >= 0xcd && marker <= 0xcf)) {
      if (segmentLength < 7) break
      return dimensions(bytes.readUInt16BE(offset + 7), bytes.readUInt16BE(offset + 5))
    }
    offset += 2 + segmentLength
  }
  throw new PhotoDecodeError('unsupported_format')
}

function parseWebp(bytes: Buffer): { width: number; height: number } {
  if (bytes.length < 30) throw new PhotoDecodeError('unsupported_format')
  const kind = bytes.toString('ascii', 12, 16)
  if (kind === 'VP8X') {
    return dimensions(1 + bytes.readUIntLE(24, 3), 1 + bytes.readUIntLE(27, 3))
  }
  if (kind === 'VP8 ' && bytes.length >= 30 && bytes[23] === 0x9d && bytes[24] === 0x01 && bytes[25] === 0x2a) {
    return dimensions(bytes.readUInt16LE(26) & 0x3fff, bytes.readUInt16LE(28) & 0x3fff)
  }
  if (kind === 'VP8L' && bytes.length >= 25 && bytes[20] === 0x2f) {
    const bits = bytes.readUInt32LE(21)
    return dimensions((bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1)
  }
  throw new PhotoDecodeError('unsupported_format')
}

function dimensions(width: number, height: number): { width: number; height: number } {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new PhotoDecodeError('unsupported_format')
  }
  return { width, height }
}

async function isDirectPath(path: string): Promise<boolean> {
  try {
    const canonical = await realpath(path)
    return normalize(canonical) === normalize(resolve(path))
  } catch {
    return false
  }
}

function stableRelative(root: string, path: string): string {
  const value = relative(root, path)
  if (!value || value === '..' || value.startsWith(`..${sep}`)) return ''
  return value.split(sep).join('/')
}

function normalize(value: string): string {
  return process.platform === 'win32' ? value.toLocaleLowerCase('en-US') : value
}

function comparePath(a: string, b: string): number {
  return normalize(a) < normalize(b) ? -1 : normalize(a) > normalize(b) ? 1 : a < b ? -1 : a > b ? 1 : 0
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code
}
