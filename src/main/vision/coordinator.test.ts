import { mkdtemp, rm, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'
import type { StoredDocumentRoot } from '../services/store'
import { PhotoIndexCoordinator } from './coordinator'
import type { VisionEngine } from './engine'
import type { BoundedNativeImage } from './scanner'

const temporary: string[] = []
afterEach(async () => { await Promise.all(temporary.splice(0).map((path) => rm(path, { recursive: true, force: true }))) })

describe('PhotoIndexCoordinator', () => {
  it('reconciles new, changed, deleted, paused, and revoked photos with one inference at a time', async () => {
    const userDataDir = await tempRoot()
    const photoRoot = await tempRoot()
    const first = join(photoRoot, 'first.png')
    await writeFile(first, pngHeader(10, 10))
    let roots: StoredDocumentRoot[] = [{ id: 'root', path: photoRoot, label: 'Photos', createdAt: new Date().toISOString() }]
    let active = 0
    let maximumActive = 0
    let calls = 0
    const coordinator = createCoordinator(userDataDir, () => roots, {
      embedImage: async () => {
        active += 1
        maximumActive = Math.max(maximumActive, active)
        await Promise.resolve()
        calls += 1
        active -= 1
        return unit()
      }
    })
    await coordinator.initialize()
    await coordinator.enable()
    await waitUntil(() => coordinator.status().state === 'ready')
    expect(coordinator.status()).toMatchObject({ indexed: 1, total: 1 })

    await writeFile(first, Buffer.concat([pngHeader(10, 10), Buffer.from([1])]))
    await coordinator.reconcile()
    await waitUntil(() => coordinator.status().state === 'ready' && calls >= 2)
    expect(coordinator.status().indexed).toBe(1)

    await coordinator.pause()
    await writeFile(join(photoRoot, 'second.png'), pngHeader(10, 10))
    await coordinator.reconcile()
    expect(coordinator.status().total).toBe(1)
    await coordinator.resume()
    await waitUntil(() => coordinator.status().state === 'ready' && coordinator.status().indexed === 2)

    await unlink(first)
    await coordinator.reconcile()
    await waitUntil(() => coordinator.status().total === 1)
    roots = []
    await coordinator.reconcile()
    expect(coordinator.status()).toMatchObject({ indexed: 0, total: 0 })
    expect(maximumActive).toBe(1)
    await coordinator.shutdown()
  })

  it('disposes inference before clearing model files', async () => {
    const userDataDir = await tempRoot()
    const events: string[] = []
    const coordinator = createCoordinator(userDataDir, async () => [], {}, events, async () => { events.push('clear') })
    await coordinator.initialize()
    await coordinator.enable()
    // Force construction as a semantic query would, while keeping the test pack-free.
    ;(coordinator as unknown as { getEngine: () => VisionEngine }).getEngine()
    await coordinator.rebuild()
    expect(events.slice(-2)).toEqual(['dispose', 'clear'])
    await coordinator.shutdown()
  })
})

function createCoordinator(
  userDataDir: string,
  listRoots: () => Promise<StoredDocumentRoot[]> | StoredDocumentRoot[],
  engineOverrides: Partial<VisionEngine> = {},
  events: string[] = [],
  clearModel = async (): Promise<void> => undefined
): PhotoIndexCoordinator {
  return new PhotoIndexCoordinator({
    userDataDir,
    listRoots: async () => listRoots(),
    isModelInstalled: async () => true,
    clearModel,
    modelRuntime: { fetch },
    delay: async () => undefined,
    createEngine: () => ({
      embedImage: engineOverrides.embedImage ?? (async () => unit()),
      embedText: engineOverrides.embedText ?? (async () => unit()),
      dispose: () => { events.push('dispose') }
    }) as unknown as VisionEngine,
    decodeThumbnail: async () => fakeImage()
  })
}

function fakeImage(): BoundedNativeImage {
  const image: BoundedNativeImage = {
    isEmpty: () => false,
    getSize: () => ({ width: 224, height: 224 }),
    crop: () => image,
    resize: () => image,
    toBitmap: () => Buffer.alloc(224 * 224 * 4)
  }
  return image
}

function unit(): Float32Array {
  const vector = new Float32Array(512)
  vector[0] = 1
  return vector
}

function pngHeader(width: number, height: number): Buffer {
  const bytes = Buffer.alloc(24)
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(bytes)
  bytes.write('IHDR', 12, 'ascii')
  bytes.writeUInt32BE(width, 16)
  bytes.writeUInt32BE(height, 20)
  return bytes
}

async function tempRoot(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), 'lifelens-coordinator-'))
  temporary.push(path)
  return path
}

async function waitUntil(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('Timed out waiting for coordinator state.')
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
}
