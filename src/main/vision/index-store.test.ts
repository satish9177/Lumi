import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  computeImageId,
  imageIdPrefix,
  INDEX_FORMAT_VERSION,
  PhotoIndexStore,
  VECTOR_ROW_BYTES,
  type PhotoIndexRecord
} from './index-store'
import {
  INDEX_JOURNAL_FILE,
  INDEX_META_FILE,
  INDEX_POINTER_FILE,
  INDEX_VECTOR_FILE,
  photoIndexDirectory
} from './model-location'
import { CLIP_EMBEDDING_LENGTH } from './protocol'

const MODEL_VERSION = 1

let userDataDir: string

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-index-'))
})

afterEach(async () => {
  await rm(userDataDir, { recursive: true, force: true })
})

function vector(fill: number): Float32Array {
  return new Float32Array(CLIP_EMBEDDING_LENGTH).fill(fill)
}

function record(overrides: Partial<PhotoIndexRecord> = {}): PhotoIndexRecord {
  const rootId = overrides.rootId ?? 'root-a'
  const relativePath = overrides.relativePath ?? 'holiday/beach.jpg'
  return {
    imageId: computeImageId(rootId, relativePath),
    rootId,
    relativePath,
    name: relativePath.split('/').pop()!,
    mtimeMs: 1_700_000_000_000,
    sizeBytes: 2_048,
    modelVersion: MODEL_VERSION,
    status: 'pending',
    attempts: 0,
    updatedAtMs: 1_700_000_000_000,
    ...overrides
  }
}

async function loadedStore(): Promise<PhotoIndexStore> {
  const store = new PhotoIndexStore(userDataDir)
  await store.load(MODEL_VERSION)
  return store
}

function journalPath(store: PhotoIndexStore): string {
  return join(store.activeDirectory(), INDEX_JOURNAL_FILE)
}

function vectorPath(store: PhotoIndexStore): string {
  return join(store.activeDirectory(), INDEX_VECTOR_FILE)
}

describe('image identity', () => {
  it('is stable for the same root and path, and case-insensitive on Windows paths', () => {
    expect(computeImageId('root-a', 'a/b.jpg')).toBe(computeImageId('root-a', 'a/b.jpg'))
    expect(computeImageId('root-a', 'A/B.JPG')).toBe(computeImageId('root-a', 'a/b.jpg'))
  })

  it('differs across roots, so two folders never collide', () => {
    expect(computeImageId('root-a', 'a.jpg')).not.toBe(computeImageId('root-b', 'a.jpg'))
  })

  it('yields a short non-reversible prefix for logging', () => {
    const id = computeImageId('root-a', 'a.jpg')
    expect(imageIdPrefix(id)).toHaveLength(8)
    expect(id).not.toContain('a.jpg')
  })
})

describe('persistence and restart', () => {
  it('round-trips a record and its vector across a reload', async () => {
    const store = await loadedStore()
    const row = await store.appendVector(vector(0.25))
    await store.put(record({ status: 'indexed', vectorRow: row }))

    const reopened = await loadedStore()
    const reloaded = reopened.get(computeImageId('root-a', 'holiday/beach.jpg'))
    expect(reloaded?.status).toBe('indexed')
    expect(reloaded?.vectorRow).toBe(row)
    expect(await reopened.readVector(row)).toEqual(vector(0.25))
  })

  it('keeps the newest line for an imageId, so an update supersedes its predecessor', async () => {
    const store = await loadedStore()
    await store.put(record({ status: 'pending' }))
    await store.put(record({ status: 'failed', failureCode: 'decode_failed', attempts: 2 }))

    const reopened = await loadedStore()
    const reloaded = reopened.get(computeImageId('root-a', 'holiday/beach.jpg'))
    expect(reloaded?.status).toBe('failed')
    expect(reloaded?.attempts).toBe(2)
  })

  it('drops a torn final line left by an interrupted write', async () => {
    const store = await loadedStore()
    await store.put(record({ status: 'indexed', vectorRow: await store.appendVector(vector(0.5)) }))
    await store.put(record({ relativePath: 'b.jpg', status: 'pending' }))

    const journal = await readFile(journalPath(store), 'utf8')
    await writeFile(journalPath(store), `${journal}{"imageId":"partial","rootId":"root-a`, 'utf8')

    const reopened = new PhotoIndexStore(userDataDir)
    const outcome = await reopened.load(MODEL_VERSION)

    expect(outcome.rebuilt).toBe(false)
    expect(outcome.droppedLines).toBe(1)
    expect(reopened.counts().total).toBe(2)
  })

  it('survives a crash between the vector write and the record write', async () => {
    const store = await loadedStore()
    // The vector lands but no record claims it: an orphan row, not a corruption.
    await store.appendVector(vector(0.75))

    const reopened = new PhotoIndexStore(userDataDir)
    const outcome = await reopened.load(MODEL_VERSION)

    expect(outcome.rebuilt).toBe(false)
    expect(reopened.counts().total).toBe(0)
  })

  it('rebuilds when the journal references a vector that is not on disk', async () => {
    const store = await loadedStore()
    await store.put(record({ status: 'indexed', vectorRow: 99 }))

    const reopened = new PhotoIndexStore(userDataDir)
    expect((await reopened.load(MODEL_VERSION)).rebuilt).toBe(true)
    expect(reopened.counts().total).toBe(0)
  })

  it('rebuilds when the model version changes, discarding stale vectors', async () => {
    const store = await loadedStore()
    await store.put(record({ status: 'indexed', vectorRow: await store.appendVector(vector(0.25)) }))

    const reopened = new PhotoIndexStore(userDataDir)
    expect((await reopened.load(MODEL_VERSION + 1)).rebuilt).toBe(true)
    expect(reopened.counts().total).toBe(0)
  })
})

describe('journal validation', () => {
  async function loadWithLine(line: string): Promise<PhotoIndexStore> {
    const store = await loadedStore()
    await store.put(record())
    await writeFile(journalPath(store), `${line}\n`, 'utf8')
    const reopened = new PhotoIndexStore(userDataDir)
    await reopened.load(MODEL_VERSION)
    return reopened
  }

  const base = {
    imageId: 'abc',
    rootId: 'root-a',
    name: 'a.jpg',
    mtimeMs: 1,
    sizeBytes: 1,
    modelVersion: MODEL_VERSION,
    status: 'pending',
    attempts: 0,
    updatedAtMs: 1
  }

  it('rejects a record carrying an absolute path', async () => {
    for (const relativePath of ['C:\\photos\\a.jpg', '/etc/passwd', '\\\\server\\share\\a.jpg']) {
      const store = await loadWithLine(JSON.stringify({ ...base, relativePath }))
      expect(store.counts().total).toBe(0)
    }
  })

  it('rejects a record that climbs out of its root', async () => {
    const store = await loadWithLine(JSON.stringify({ ...base, relativePath: '../../secrets/a.jpg' }))
    expect(store.counts().total).toBe(0)
  })

  it('rejects an unknown status or failure code', async () => {
    expect((await loadWithLine(JSON.stringify({ ...base, relativePath: 'a.jpg', status: 'owned' }))).counts().total).toBe(0)
    expect(
      (await loadWithLine(JSON.stringify({ ...base, relativePath: 'a.jpg', failureCode: 'stack trace' }))).counts().total
    ).toBe(0)
  })

  it('rejects a negative or fractional vector row', async () => {
    for (const vectorRow of [-1, 1.5]) {
      const store = await loadWithLine(JSON.stringify({ ...base, relativePath: 'a.jpg', vectorRow }))
      expect(store.counts().total).toBe(0)
    }
  })

  it('accepts a well-formed record', async () => {
    const store = await loadWithLine(JSON.stringify({ ...base, relativePath: 'holiday/a.jpg' }))
    expect(store.counts().total).toBe(1)
  })
})

describe('reconciliation', () => {
  it('tombstones a deleted image and drops it from search', async () => {
    const store = await loadedStore()
    const imageId = computeImageId('root-a', 'holiday/beach.jpg')
    await store.put(record({ status: 'indexed', vectorRow: await store.appendVector(vector(0.25)) }))
    expect(store.indexed()).toHaveLength(1)

    await store.markDeleted(imageId, 2_000)

    expect(store.indexed()).toHaveLength(0)
    expect(store.counts().total).toBe(0)
    expect(store.get(imageId)?.status).toBe('deleted')
  })

  it('purges every record belonging to a revoked root', async () => {
    const store = await loadedStore()
    await store.put(record({ rootId: 'root-a', relativePath: 'a.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.1)) }))
    await store.put(record({ rootId: 'root-a', relativePath: 'b.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.2)) }))
    await store.put(record({ rootId: 'root-b', relativePath: 'c.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.3)) }))

    const removed = await store.purgeRoot('root-a', 3_000)

    expect(removed).toBe(2)
    expect(store.indexed().map((entry) => entry.rootId)).toEqual(['root-b'])
  })

  it('purges roots that are no longer approved at all', async () => {
    const store = await loadedStore()
    await store.put(record({ rootId: 'root-a', relativePath: 'a.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.1)) }))
    await store.put(record({ rootId: 'root-gone', relativePath: 'b.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.2)) }))

    const removed = await store.retainRoots(['root-a'], 4_000)

    expect(removed).toBe(1)
    expect(store.indexed().map((entry) => entry.rootId)).toEqual(['root-a'])
  })

  it('reports counts by status', async () => {
    const store = await loadedStore()
    await store.put(record({ relativePath: 'a.jpg', status: 'indexed', vectorRow: await store.appendVector(vector(0.1)) }))
    await store.put(record({ relativePath: 'b.jpg', status: 'pending' }))
    await store.put(record({ relativePath: 'c.jpg', status: 'failed', failureCode: 'decode_failed' }))
    await store.put(record({ relativePath: 'd.jpg', status: 'skipped', failureCode: 'too_many_pixels' }))

    expect(store.counts()).toEqual({ total: 4, indexed: 1, pending: 1, failed: 1, skipped: 1 })
  })
})

describe('compaction', () => {
  async function fill(store: PhotoIndexStore, count: number): Promise<void> {
    for (let index = 0; index < count; index += 1) {
      const row = await store.appendVector(vector(index / 1_000))
      await store.put(record({ relativePath: `photo-${index}.jpg`, status: 'indexed', vectorRow: row }))
    }
  }

  it('waits until a real fraction of the file is dead', async () => {
    const store = await loadedStore()
    await fill(store, 64)
    expect(store.shouldCompact()).toBe(false)

    for (let index = 0; index < 6; index += 1) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 5_000)
    }
    expect(store.shouldCompact()).toBe(false)

    for (let index = 6; index < 20; index += 1) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 5_000)
    }
    expect(store.shouldCompact()).toBe(true)
  })

  it('reclaims dead rows while preserving every surviving vector', async () => {
    const store = await loadedStore()
    await fill(store, 6)
    const survivorId = computeImageId('root-a', 'photo-4.jpg')
    const before = await store.readVector(store.get(survivorId)!.vectorRow!)

    for (const index of [0, 1, 2, 5]) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 6_000)
    }
    await store.compact()

    expect(store.indexed()).toHaveLength(2)
    const after = await store.readVector(store.get(survivorId)!.vectorRow!)
    expect(after).toEqual(before)
  })

  it('shrinks both files and survives a reload', async () => {
    const store = await loadedStore()
    await fill(store, 8)
    for (let index = 0; index < 6; index += 1) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 7_000)
    }
    await store.compact()
    await store.flush()

    const vectorBytes = (await readFile(vectorPath(store))).byteLength
    expect(vectorBytes).toBe(2 * CLIP_EMBEDDING_LENGTH * 4)

    const reopened = await loadedStore()
    expect(reopened.indexed()).toHaveLength(2)
    expect(await reopened.readVector(0)).not.toBeUndefined()
  })

  it('loads every live vector in one pass', async () => {
    const store = await loadedStore()
    await fill(store, 4)
    await store.markDeleted(computeImageId('root-a', 'photo-1.jpg'), 8_000)

    const vectors = await store.readAllVectors()

    expect(vectors.size).toBe(3)
    expect(vectors.get(computeImageId('root-a', 'photo-0.jpg'))).toEqual(vector(0))
    expect(vectors.has(computeImageId('root-a', 'photo-1.jpg'))).toBe(false)
  })
})

describe('reset', () => {
  it('clears every record and vector', async () => {
    const store = await loadedStore()
    await store.put(record({ status: 'indexed', vectorRow: await store.appendVector(vector(0.25)) }))

    await store.reset(MODEL_VERSION)

    expect(store.counts().total).toBe(0)
    expect(await store.readVector(0)).toBeUndefined()
  })
})

describe('generation crash-safety', () => {
  const indexDir = (): string => photoIndexDirectory(userDataDir)
  const genName = (n: number): string => `gen-${String(n).padStart(6, '0')}`

  async function genDirs(): Promise<string[]> {
    const entries = await readdir(indexDir(), { withFileTypes: true })
    return entries
      .filter((entry) => entry.isDirectory() && /^gen-\d+$/.test(entry.name))
      .map((entry) => entry.name)
      .sort()
  }

  async function pointer(): Promise<string> {
    return (await readFile(join(indexDir(), INDEX_POINTER_FILE), 'utf8')).trim()
  }

  /** Writes a self-consistent generation directory by hand. */
  async function writeGeneration(
    generation: number,
    entries: Array<{ record: PhotoIndexRecord; vector?: Float32Array }>,
    options: { withJournal?: boolean; withMeta?: boolean; modelVersion?: number } = {}
  ): Promise<void> {
    const dir = join(indexDir(), genName(generation))
    await mkdir(dir, { recursive: true })

    const vectors = entries.filter((entry) => entry.vector).map((entry) => entry.vector!)
    if (vectors.length > 0) {
      const buffer = Buffer.concat(
        vectors.map((v) => Buffer.from(v.buffer, v.byteOffset, v.byteLength))
      )
      await writeFile(join(dir, INDEX_VECTOR_FILE), buffer)
    }
    if (options.withJournal !== false) {
      await writeFile(
        join(dir, INDEX_JOURNAL_FILE),
        entries.map((entry) => `${JSON.stringify(entry.record)}\n`).join(''),
        'utf8'
      )
    }
    if (options.withMeta !== false) {
      await writeFile(
        join(dir, INDEX_META_FILE),
        JSON.stringify({
          formatVersion: INDEX_FORMAT_VERSION,
          modelVersion: options.modelVersion ?? MODEL_VERSION,
          generation,
          rowCount: vectors.length
        }),
        'utf8'
      )
    }
  }

  async function setPointer(generation: number): Promise<void> {
    await mkdir(indexDir(), { recursive: true })
    await writeFile(join(indexDir(), INDEX_POINTER_FILE), genName(generation), 'utf8')
  }

  function indexedEntry(relativePath: string, row: number, fill: number): { record: PhotoIndexRecord; vector: Float32Array } {
    return { record: record({ relativePath, status: 'indexed', vectorRow: row }), vector: vector(fill) }
  }

  it('recovers the old generation when compaction crashed before writing vectors', async () => {
    // Only an empty half-built new directory exists; the pointer still names the old.
    await writeGeneration(1, [indexedEntry('a.jpg', 0, 0.1), indexedEntry('b.jpg', 1, 0.2)])
    await mkdir(join(indexDir(), genName(2)), { recursive: true })
    await setPointer(1)

    const store = new PhotoIndexStore(userDataDir)
    const outcome = await store.load(MODEL_VERSION)

    expect(outcome.rebuilt).toBe(false)
    expect(store.indexed()).toHaveLength(2)
    expect(await store.readVector(0)).toEqual(vector(0.1))
    // The stale half-built generation is gone.
    expect(await genDirs()).toEqual([genName(1)])
  })

  it('recovers the old generation when compaction crashed after vectors but before records', async () => {
    await writeGeneration(1, [indexedEntry('a.jpg', 0, 0.1), indexedEntry('b.jpg', 1, 0.2)])
    const half = join(indexDir(), genName(2))
    await mkdir(half, { recursive: true })
    await writeFile(join(half, INDEX_VECTOR_FILE), Buffer.alloc(VECTOR_ROW_BYTES))
    await setPointer(1)

    const store = new PhotoIndexStore(userDataDir)
    const outcome = await store.load(MODEL_VERSION)

    expect(outcome.rebuilt).toBe(false)
    expect(store.indexed()).toHaveLength(2)
    expect(await genDirs()).toEqual([genName(1)])
  })

  it('never loads a fully written new generation while the pointer still names the old one', async () => {
    // The compaction wrote gen-2 completely but crashed before flipping CURRENT.
    // The old generation must remain authoritative and the new one discarded.
    await writeGeneration(1, [indexedEntry('a.jpg', 0, 0.1)])
    await writeGeneration(2, [indexedEntry('a.jpg', 0, 0.9), indexedEntry('b.jpg', 1, 0.8)])
    await setPointer(1)

    const store = new PhotoIndexStore(userDataDir)
    await store.load(MODEL_VERSION)

    expect(store.indexed()).toHaveLength(1)
    // The old vector, not the new generation's rewrite, is what survives.
    expect(await store.readVector(store.get(computeImageId('root-a', 'a.jpg'))!.vectorRow!)).toEqual(vector(0.1))
    expect(await genDirs()).toEqual([genName(1)])
  })

  it('loads the new generation once the pointer flip has landed', async () => {
    await writeGeneration(1, [indexedEntry('a.jpg', 0, 0.1)])
    await writeGeneration(2, [indexedEntry('a.jpg', 0, 0.9), indexedEntry('b.jpg', 1, 0.8)])
    await setPointer(2)

    const store = new PhotoIndexStore(userDataDir)
    await store.load(MODEL_VERSION)

    expect(store.indexed()).toHaveLength(2)
    expect(await store.readVector(0)).toEqual(vector(0.9))
    expect(await genDirs()).toEqual([genName(2)])
  })

  it('rebuilds and moves to a fresh generation when the pointer names a missing directory', async () => {
    await setPointer(7)

    const store = new PhotoIndexStore(userDataDir)
    const outcome = await store.load(MODEL_VERSION)

    expect(outcome.rebuilt).toBe(true)
    expect(store.counts().total).toBe(0)
    // A fresh generation is created rather than reusing the dangling number.
    expect(await pointer()).not.toBe(genName(7))
    expect(store.isLoaded()).toBe(true)
  })

  it('keeps a survivor bound to its own vector across a real compaction and reload', async () => {
    const store = await loadedStore()
    // Distinct fills so a mis-association would be visible.
    for (let index = 0; index < 6; index += 1) {
      const row = await store.appendVector(vector((index + 1) / 10))
      await store.put(record({ relativePath: `photo-${index}.jpg`, status: 'indexed', vectorRow: row }))
    }
    for (const index of [0, 2, 4]) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 1)
    }

    const survivorExpected = new Map<string, Float32Array>()
    for (const index of [1, 3, 5]) {
      survivorExpected.set(computeImageId('root-a', `photo-${index}.jpg`), vector((index + 1) / 10))
    }

    await store.compact()

    const reopened = await loadedStore()
    for (const [imageId, expected] of survivorExpected) {
      const reloaded = reopened.get(imageId)
      expect(reloaded?.status).toBe('indexed')
      expect(await reopened.readVector(reloaded!.vectorRow!)).toEqual(expected)
    }
    expect(reopened.indexed()).toHaveLength(3)
  })
})

describe('concurrency', () => {
  it('gives overlapping appendVector calls distinct, contiguous rows', async () => {
    const store = await loadedStore()

    const rows = await Promise.all([
      store.appendVector(vector(0.1)),
      store.appendVector(vector(0.2)),
      store.appendVector(vector(0.3))
    ])

    expect([...rows].sort((a, b) => a - b)).toEqual([0, 1, 2])
    expect(new Set(rows).size).toBe(3)
    // Each row holds exactly the vector it was given.
    expect(await store.readVector(rows[0]!)).toEqual(vector(0.1))
    expect(await store.readVector(rows[1]!)).toEqual(vector(0.2))
    expect(await store.readVector(rows[2]!)).toEqual(vector(0.3))
  })

  it('applies overlapping put calls for one image in submission order', async () => {
    const store = await loadedStore()
    const imageId = computeImageId('root-a', 'holiday/beach.jpg')

    await Promise.all([
      store.put(record({ status: 'pending', attempts: 0 })),
      store.put(record({ status: 'failed', failureCode: 'decode_failed', attempts: 1 })),
      store.put(record({ status: 'indexed', vectorRow: 0, attempts: 2 }))
    ])
    // The vector the last write references still has to exist for a reload.
    await store.appendVector(vector(0.5))

    const reopened = await loadedStore()
    expect(reopened.get(imageId)?.attempts).toBe(2)
    expect(reopened.get(imageId)?.status).toBe('indexed')
  })

  it('does not interleave a compaction with a concurrent append', async () => {
    const store = await loadedStore()
    for (let index = 0; index < 80; index += 1) {
      const row = await store.appendVector(vector((index % 9) / 10))
      await store.put(record({ relativePath: `photo-${index}.jpg`, status: 'indexed', vectorRow: row }))
    }
    for (let index = 0; index < 40; index += 1) {
      await store.markDeleted(computeImageId('root-a', `photo-${index}.jpg`), 1)
    }

    // Fire compaction and an append together; serialization must keep both intact.
    const appendedFill = 0.123
    const [, appendedRow] = await Promise.all([store.compact(), store.appendVector(vector(appendedFill))])

    // Whatever the interleaving, the appended vector is readable and the store
    // reloads to a consistent state.
    expect(await store.readVector(appendedRow)).toEqual(vector(appendedFill))

    const reopened = await loadedStore()
    expect(reopened.indexed().length).toBeGreaterThanOrEqual(40)
    for (const entry of reopened.indexed()) {
      expect(await reopened.readVector(entry.vectorRow!)).not.toBeUndefined()
    }
  })

  it('releases the mutation lock after a failed write so later writes proceed', async () => {
    const store = await loadedStore()

    // An unsafe path is rejected inside the locked section.
    await expect(
      store.put(record({ relativePath: 'C:\\escape\\a.jpg' }))
    ).rejects.toThrow()

    // The lock must not be stuck: a subsequent valid write still lands.
    await store.put(record({ relativePath: 'holiday/ok.jpg', status: 'pending' }))
    expect(store.counts().total).toBe(1)
  })
})
