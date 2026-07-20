import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PhotoIndexCoordinator, type PhotoIndexCoordinatorDependencies } from './coordinator'
import { extrasLanguageDirectory } from './model-pack'
import { computeImageId } from './index-store'
import type { BoundedNativeImage } from './scanner'

let userDataDir: string
let photosDir: string

/**
 * Every coordinator a test builds, so teardown can stop it.
 *
 * The coordinator starts reconciliation as a fire-and-forget promise. Deleting
 * the temp directory while that is still writing the index meta file fails the
 * whole run with an unhandled ENOENT — and only under load, which is what makes
 * it a flake rather than an honest failure. Shutting each coordinator down
 * first is the fix; suppressing the rejection would only hide it.
 */
const running: PhotoIndexCoordinator[] = []

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-coord2-'))
  photosDir = await mkdtemp(join(tmpdir(), 'lumi-photos-'))
})

afterEach(async () => {
  for (const coordinator of running.splice(0)) {
    await coordinator.shutdown().catch(() => undefined)
  }
  await rm(userDataDir, { recursive: true, force: true })
  await rm(photosDir, { recursive: true, force: true })
})

/**
 * A fake image that actually honours crop and resize. Returning `this` from
 * both would make `toBitmap()` disagree with `getSize()`, which the decode path
 * correctly rejects — so the fake has to be faithful about geometry.
 */
function image(width = 640, height = 480): BoundedNativeImage {
  return {
    isEmpty: () => width === 0 || height === 0,
    getSize: () => ({ width, height }),
    crop: (rect) => image(rect.width, rect.height),
    resize: (options) =>
      image(options.width, options.height ?? Math.max(1, Math.round((options.width / width) * height))),
    toBitmap: () => Buffer.alloc(width * height * 4, 0x60)
  }
}

interface Harness {
  coordinator: PhotoIndexCoordinator
  ocrCalls: () => number
  faceCalls: () => number
  /** Simulates the user removing the folder from the approved-root store. */
  removeRoot: () => void
}

/**
 * A coordinator wired to fake detectors. Nothing here loads a real model, so
 * the scheduling, cancellation, and persistence behaviour is exercised
 * deterministically and offline.
 */
async function harness(
  options: {
    files?: string[]
    recognize?: (image: Buffer) => Promise<{ text: string }>
    detect?: () => Promise<Float32Array>
    extrasInstalled?: boolean
  } = {}
): Promise<Harness> {
  const files = options.files ?? ['screenshot-invoice.png']
  for (const name of files) {
    await writeFile(join(photosDir, name), 'x')
  }

  // The OCR engine deliberately fails closed when its verified training data is
  // absent, rather than fetching one. Place a stand-in so that real check is
  // satisfied honestly instead of being stubbed out.
  const languageDir = extrasLanguageDirectory(userDataDir)
  await mkdir(languageDir, { recursive: true })
  await writeFile(join(languageDir, 'eng.traineddata'), 'stand-in for the verified file')

  const state = { ocr: 0, face: 0 }
  const roots = [{ id: 'root-a', path: photosDir, label: 'Photos', createdAt: '2026-01-01T00:00:00.000Z' }]

  const dependencies: PhotoIndexCoordinatorDependencies = {
    userDataDir,
    listRoots: async () => [...roots],
    createEngine: () =>
      ({
        embedImage: async () => new Float32Array(512).fill(0.1),
        embedText: async () => new Float32Array(512).fill(0.1),
        detectFaces: async () => {
          state.face += 1
          return options.detect ? await options.detect() : Float32Array.from([0.97, 0.95])
        },
        releaseImageModel: () => undefined,
        releaseFaceModel: () => undefined,
        dispose: () => undefined,
        isRunning: () => true,
        loadedModels: () => []
      }) as never,
    decodeThumbnail: async () => image(),
    modelRuntime: { fetch: (() => { throw new Error('no network in tests') }) as unknown as typeof fetch },
    isModelInstalled: async () => true,
    isExtrasInstalled: async () => options.extrasInstalled ?? true,
    downloadExtras: async () => undefined,
    createOcrWorker: async () => ({
      recognize: async (buffer: Buffer) => {
        state.ocr += 1
        return options.recognize ? await options.recognize(buffer) : { text: 'INVOICE 1234' }
      },
      terminate: async () => undefined
    }),
    // Real stat values: the coordinator revalidates mtime and size before and
    // after every expensive step, so a fabricated snapshot is correctly refused.
    scan: async () => ({
      files: await Promise.all(
        files.map(async (name) => {
          const absolutePath = join(photosDir, name)
          const details = await (await import('node:fs/promises')).stat(absolutePath)
          return {
            rootId: 'root-a',
            rootPath: photosDir,
            absolutePath,
            relativePath: name,
            name,
            mtimeMs: details.mtimeMs,
            sizeBytes: details.size,
            width: 640,
            height: 480
          }
        })
      ),
      failures: [],
      truncated: false
    }),
    delay: async () => undefined,
    now: () => 1_800_000_000_000
  }

  const coordinator = new PhotoIndexCoordinator(dependencies)
  running.push(coordinator)
  await coordinator.initialize()
  return {
    coordinator,
    ocrCalls: () => state.ocr,
    faceCalls: () => state.face,
    removeRoot: () => {
      roots.length = 0
    }
  }
}

/** Lets the coordinator's fire-and-forget processing settle. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 60; turn += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

describe('Phase 2 is opt-in and off by default', () => {
  it('does no OCR or face work until it is turned on', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await settle()

    expect(h.ocrCalls()).toBe(0)
    expect(h.faceCalls()).toBe(0)
    // Semantic indexing still happened.
    expect(h.coordinator.status().indexed).toBeGreaterThan(0)
  })

  it('reports both capabilities as off in its status', async () => {
    const h = await harness()
    const status = h.coordinator.status()
    expect(status.textSearchEnabled).toBe(false)
    expect(status.faceCountEnabled).toBe(false)
  })
})

describe('turning on text search', () => {
  it('reads text and stores it against the record', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await settle()

    expect(h.ocrCalls()).toBeGreaterThan(0)
    const status = h.coordinator.status()
    expect(status.textSearchEnabled).toBe(true)
    expect(status.textIndexed).toBeGreaterThan(0)
  })

  it('finds a photo by the text inside it', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await settle()

    const result = await h.coordinator.search(
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      (await import('../../shared/search-query')).normalizeSearchQuery({
        queryTerms: 'invoice',
        containsText: 'invoice 1234'
      })
    )
    expect(result.available).toBe(true)
    expect(result.candidates.map((candidate) => candidate.name)).toContain('screenshot-invoice.png')
    expect(result.candidates[0]!.reason).toBe('Contains the text you searched for')
  })

  it('does not run OCR when the extras pack is not installed', async () => {
    const h = await harness({ extrasInstalled: false })
    await h.coordinator.enable()
    await settle()
    expect(h.ocrCalls()).toBe(0)
  })
})

describe('turning on visible-face counting', () => {
  it('counts faces and stores only counts', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setFaceCountEnabled(true)
    await settle()

    expect(h.faceCalls()).toBeGreaterThan(0)
    expect(h.coordinator.status().faceScanned).toBeGreaterThan(0)
  })

  it('answers a two-people search from the stored count', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setFaceCountEnabled(true)
    await settle()

    const { normalizeSearchQuery } = await import('../../shared/search-query')
    const result = await h.coordinator.search(
      normalizeSearchQuery({ queryTerms: 'photos', people: { op: 'eq', n: 2 } })
    )
    expect(result.candidates).toHaveLength(1)
    expect(result.candidates[0]!.reason).toBe('2 visible faces detected')
  })

  it('excludes a photo with the wrong count', async () => {
    const h = await harness({ detect: async () => Float32Array.from([0.97]) })
    await h.coordinator.enable()
    await h.coordinator.setFaceCountEnabled(true)
    await settle()

    const { normalizeSearchQuery } = await import('../../shared/search-query')
    const result = await h.coordinator.search(
      normalizeSearchQuery({ queryTerms: 'photos', people: { op: 'eq', n: 2 } })
    )
    expect(result.candidates).toHaveLength(0)
  })

  it('never reports an unscanned photo as having no people', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await settle()

    const { normalizeSearchQuery } = await import('../../shared/search-query')
    const result = await h.coordinator.search(
      normalizeSearchQuery({ queryTerms: 'photos', people: { op: 'none' } })
    )
    expect(result.candidates).toHaveLength(0)
    expect(result.message).toMatch(/not been checked for visible faces/)
  })
})

describe('rebuilding one Phase-2 index leaves the others alone', () => {
  it('re-reads text without discarding the CLIP vectors', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await settle()

    const indexedBefore = h.coordinator.status().indexed
    const callsBefore = h.ocrCalls()

    await h.coordinator.rebuildTextIndex()
    await settle()

    // Every photo is still embedded, and the text was read again.
    expect(h.coordinator.status().indexed).toBe(indexedBefore)
    expect(h.ocrCalls()).toBeGreaterThan(callsBefore)
  })

  it('re-counts faces without re-reading text', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await h.coordinator.setFaceCountEnabled(true)
    await settle()

    const ocrBefore = h.ocrCalls()
    const faceBefore = h.faceCalls()

    await h.coordinator.rebuildFaceIndex()
    await settle()

    expect(h.faceCalls()).toBeGreaterThan(faceBefore)
    expect(h.ocrCalls()).toBe(ocrBefore)
  })
})

describe('Phase-2 work stops when authority is withdrawn', () => {
  it('stops immediately when the root is revoked', async () => {
    const h = await harness({ files: ['a.png', 'b.png', 'c.png'] })
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    h.removeRoot()
    await h.coordinator.revokeRoot('root-a')
    await settle()

    // Whatever had been recorded is purged with the root.
    expect(h.coordinator.status().total).toBe(0)
  })

  it('stops when indexing is paused', async () => {
    const h = await harness({ files: ['a.png', 'b.png', 'c.png'] })
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await h.coordinator.pause()
    const afterPause = h.ocrCalls()
    await settle()

    // At most the job already in flight completes; no new work is started.
    expect(h.ocrCalls()).toBeLessThanOrEqual(afterPause + 1)
  })

  it('survives an OCR engine that always fails, without stopping Phase 1', async () => {
    const h = await harness({
      recognize: async () => {
        throw new Error('engine exploded')
      }
    })
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await settle()

    // Semantic search is unaffected by a broken text reader.
    expect(h.coordinator.status().indexed).toBeGreaterThan(0)
  })
})

describe('no recognized text reaches a log', () => {
  it('writes nothing to the console while indexing text', async () => {
    const spies = [
      vi.spyOn(console, 'log').mockImplementation(() => {}),
      vi.spyOn(console, 'info').mockImplementation(() => {}),
      vi.spyOn(console, 'warn').mockImplementation(() => {}),
      vi.spyOn(console, 'error').mockImplementation(() => {})
    ]
    try {
      const h = await harness({ recognize: async () => ({ text: 'SALARY 120000 CONFIDENTIAL' }) })
      await h.coordinator.enable()
      await h.coordinator.setTextSearchEnabled(true)
      await settle()

      for (const spy of spies) {
        for (const call of spy.mock.calls) {
          expect(JSON.stringify(call)).not.toMatch(/SALARY|120000|CONFIDENTIAL/i)
        }
      }
    } finally {
      for (const spy of spies) spy.mockRestore()
    }
  })
})

describe('the search result carries no image-derived data', () => {
  it('exposes only trusted candidate fields, never text or counts', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await h.coordinator.setTextSearchEnabled(true)
    await settle()

    const { normalizeSearchQuery } = await import('../../shared/search-query')
    const result = await h.coordinator.search(
      normalizeSearchQuery({ queryTerms: 'invoice', containsText: 'invoice' })
    )

    for (const candidate of result.candidates) {
      expect(Object.keys(candidate).sort()).toEqual(
        ['absolutePath', 'modifiedAtMs', 'name', 'reason', 'relativePath', 'rootId', 'sizeBytes'].sort()
      )
      // The reason is app-authored; the recognized text itself never travels.
      expect(candidate.reason).not.toMatch(/1234/)
    }
  })

  it('keeps the image id out of the candidate entirely', async () => {
    const h = await harness()
    await h.coordinator.enable()
    await settle()
    const { normalizeSearchQuery } = await import('../../shared/search-query')
    const result = await h.coordinator.search(
      normalizeSearchQuery({ queryTerms: 'invoice', concepts: ['invoice'] })
    )
    const forbidden = computeImageId('root-a', 'screenshot-invoice.png')
    expect(JSON.stringify(result.candidates)).not.toContain(forbidden)
  })
})
