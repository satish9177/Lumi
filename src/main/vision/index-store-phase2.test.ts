import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MAX_OCR_TEXT_CHARS, MAX_OCR_TOKENS } from '../../shared/ocr-text'
import { FACE_MODEL_VERSION, OCR_MODEL_VERSION } from './extras-manifest'
import {
  computeImageId,
  MAX_STORED_FACE_COUNT,
  PhotoIndexStore,
  type PhotoIndexRecord
} from './index-store'
import { INDEX_JOURNAL_FILE, INDEX_POINTER_FILE, photoIndexDirectory } from './model-location'
import { CLIP_EMBEDDING_LENGTH } from './protocol'

const MODEL_VERSION = 1
const CURRENT = { ocr: OCR_MODEL_VERSION, face: FACE_MODEL_VERSION }

let userDataDir: string

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-phase2-'))
})

afterEach(async () => {
  await rm(userDataDir, { recursive: true, force: true })
})

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

async function loadedStore(phase2 = CURRENT): Promise<PhotoIndexStore> {
  const store = new PhotoIndexStore(userDataDir)
  await store.load(MODEL_VERSION, phase2)
  return store
}

/** An indexed record with a real vector row, as Phase 1 would have left it. */
async function withVector(store: PhotoIndexStore, overrides: Partial<PhotoIndexRecord> = {}): Promise<PhotoIndexRecord> {
  const row = await store.appendVector(new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.1))
  const stored = record({ status: 'indexed', vectorRow: row, ...overrides })
  await store.put(stored)
  return stored
}

describe('a Phase-1 index upgrades in place', () => {
  it('loads a journal written before Phase 2 existed, keeping every vector', async () => {
    const first = await loadedStore()
    const stored = await withVector(first)
    await first.flush()

    // Simulate a genuine Phase-1 journal: no Phase-2 keys at all.
    const journalPath = join(first.activeDirectory(), INDEX_JOURNAL_FILE)
    const phase1Line = JSON.stringify({
      imageId: stored.imageId,
      rootId: stored.rootId,
      relativePath: stored.relativePath,
      name: stored.name,
      mtimeMs: stored.mtimeMs,
      sizeBytes: stored.sizeBytes,
      vectorRow: stored.vectorRow,
      modelVersion: MODEL_VERSION,
      status: 'indexed',
      attempts: 1,
      updatedAtMs: stored.updatedAtMs
    })
    await writeFile(journalPath, `${phase1Line}\n`, 'utf8')

    const reopened = new PhotoIndexStore(userDataDir)
    const { rebuilt } = await reopened.load(MODEL_VERSION, CURRENT)

    expect(rebuilt).toBe(false)
    expect(reopened.counts().indexed).toBe(1)
    expect(reopened.get(stored.imageId)?.vectorRow).toBe(stored.vectorRow)
  })

  it('reports a Phase-1 record as never checked, not as zero faces or no text', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)

    const loaded = store.get(stored.imageId)!
    expect(loaded.ocrStatus).toBeUndefined()
    expect(loaded.faceStatus).toBeUndefined()
    expect(loaded.visibleFaceCount).toBeUndefined()
    expect(loaded.uncertainFaceCount).toBeUndefined()
    expect(loaded.ocrText).toBeUndefined()
  })
})

describe('Phase-2 model versions never cost a CLIP re-index', () => {
  it('drops stale OCR results but keeps the record, its status, and its vector', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'degree certificate', tokens: ['degree', 'certificate'] }, 1)
    await store.flush()

    // A new OCR model ships. The face version is unchanged.
    const upgraded = new PhotoIndexStore(userDataDir)
    const { rebuilt } = await upgraded.load(MODEL_VERSION, { ocr: OCR_MODEL_VERSION + 1, face: FACE_MODEL_VERSION })

    expect(rebuilt).toBe(false)
    const loaded = upgraded.get(stored.imageId)!
    expect(loaded.status).toBe('indexed')
    expect(loaded.vectorRow).toBe(stored.vectorRow)
    expect(loaded.ocrStatus).toBeUndefined()
    expect(loaded.ocrText).toBeUndefined()
    expect(upgraded.counts().indexed).toBe(1)
  })

  it('invalidates the two Phase-2 signals independently of each other', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'interview', tokens: ['interview'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 2, uncertainFaceCount: 0 }, 1)
    await store.flush()

    // Only the face model moved.
    const upgraded = new PhotoIndexStore(userDataDir)
    await upgraded.load(MODEL_VERSION, { ocr: OCR_MODEL_VERSION, face: FACE_MODEL_VERSION + 1 })

    const loaded = upgraded.get(stored.imageId)!
    expect(loaded.ocrStatus).toBe('done')
    expect(loaded.ocrText).toBe('interview')
    expect(loaded.faceStatus).toBeUndefined()
    expect(loaded.visibleFaceCount).toBeUndefined()
    expect(loaded.vectorRow).toBe(stored.vectorRow)
  })

  it('counts a superseded result as not done, rather than claiming coverage', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'a', tokens: ['aa'] }, 1)
    expect(store.phase2Counts().ocrDone).toBe(1)
    await store.flush()

    const upgraded = new PhotoIndexStore(userDataDir)
    await upgraded.load(MODEL_VERSION, { ocr: OCR_MODEL_VERSION + 1, face: FACE_MODEL_VERSION })
    expect(upgraded.phase2Counts().ocrDone).toBe(0)
    expect(upgraded.phase2Counts().total).toBe(1)
  })
})

describe('a changed file invalidates its stored Phase-2 results', () => {
  it('clears OCR and face results when the record is rewritten after a change', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'old text', tokens: ['old'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 3 }, 1)
    expect(store.get(stored.imageId)?.ocrText).toBe('old text')

    // The coordinator re-puts a pending record when mtime/size change.
    await store.put(record({ status: 'pending', mtimeMs: stored.mtimeMs + 5_000 }))

    const loaded = store.get(stored.imageId)!
    expect(loaded.ocrStatus).toBeUndefined()
    expect(loaded.ocrText).toBeUndefined()
    expect(loaded.faceStatus).toBeUndefined()
    expect(loaded.visibleFaceCount).toBeUndefined()
  })
})

describe('Phase-2 writers merge and cannot damage Phase-1 state', () => {
  it('does not resurrect a deleted record', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.markDeleted(stored.imageId, 2)

    await store.recordOcr(stored.imageId, { status: 'done', text: 'text', tokens: ['text'] }, 3)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 1 }, 3)

    expect(store.get(stored.imageId)?.status).toBe('deleted')
    expect(store.get(stored.imageId)?.ocrStatus).toBeUndefined()
    expect(store.counts().total).toBe(0)
  })

  it('ignores a result for an image the index has never seen', async () => {
    const store = await loadedStore()
    await store.recordFaces('never-seen', { status: 'done', visibleFaceCount: 2 }, 1)
    expect(store.get('never-seen')).toBeUndefined()
  })

  it('leaves the vector row intact across both Phase-2 writes', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 't', tokens: ['tt'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 0 }, 1)

    const loaded = store.get(stored.imageId)!
    expect(loaded.vectorRow).toBe(stored.vectorRow)
    expect(loaded.status).toBe('indexed')
  })

  it('clears text when a previously successful image later fails', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'secret', tokens: ['secret'] }, 1)
    await store.recordOcr(stored.imageId, { status: 'failed', failureCode: 'ocr_timeout' }, 2)

    const loaded = store.get(stored.imageId)!
    expect(loaded.ocrStatus).toBe('failed')
    expect(loaded.ocrText).toBeUndefined()
    expect(loaded.ocrFailureCode).toBe('ocr_timeout')
  })
})

describe('stored Phase-2 values are bounded', () => {
  it('truncates oversized OCR text and token lists on the way in', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(
      stored.imageId,
      {
        status: 'done',
        text: 'x'.repeat(MAX_OCR_TEXT_CHARS * 3),
        tokens: Array.from({ length: MAX_OCR_TOKENS * 3 }, (_, i) => `token${i}`)
      },
      1
    )

    const loaded = store.get(stored.imageId)!
    expect(loaded.ocrText!.length).toBe(MAX_OCR_TEXT_CHARS)
    expect(loaded.ocrTokens!.length).toBe(MAX_OCR_TOKENS)
  })

  it('caps an implausible face count rather than storing it', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 10_000 }, 1)
    expect(store.get(stored.imageId)!.visibleFaceCount).toBe(MAX_STORED_FACE_COUNT)
  })

  it('rejects a negative or fractional count rather than coercing it', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: -3, uncertainFaceCount: 1.5 }, 1)
    const loaded = store.get(stored.imageId)!
    expect(loaded.visibleFaceCount).toBeUndefined()
    expect(loaded.uncertainFaceCount).toBeUndefined()
  })
})

describe('a corrupt or hostile journal cannot inject Phase-2 state', () => {
  async function loadWithLine(line: string): Promise<PhotoIndexStore> {
    const seed = await loadedStore()
    await seed.flush()
    const directory = seed.activeDirectory()
    await writeFile(join(directory, INDEX_JOURNAL_FILE), `${line}\n`, 'utf8')
    const store = new PhotoIndexStore(userDataDir)
    await store.load(MODEL_VERSION, CURRENT)
    return store
  }

  const base = {
    imageId: 'abc123',
    rootId: 'root-a',
    name: 'a.jpg',
    mtimeMs: 1,
    sizeBytes: 1,
    modelVersion: MODEL_VERSION,
    status: 'indexed',
    attempts: 1,
    updatedAtMs: 1
  }

  it('drops an unknown ocrStatus rather than trusting it', async () => {
    const store = await loadWithLine(
      JSON.stringify({ ...base, relativePath: 'a.jpg', ocrStatus: 'trusted', ocrVersion: OCR_MODEL_VERSION, ocrText: 'x' })
    )
    expect(store.get('abc123')?.ocrStatus).toBeUndefined()
    expect(store.get('abc123')?.ocrText).toBeUndefined()
  })

  it('drops an unknown failure code rather than surfacing it', async () => {
    const store = await loadWithLine(
      JSON.stringify({
        ...base,
        relativePath: 'a.jpg',
        faceStatus: 'failed',
        faceVersion: FACE_MODEL_VERSION,
        faceFailureCode: 'C:\\Users\\satis\\model.onnx crashed'
      })
    )
    expect(store.get('abc123')?.faceStatus).toBe('failed')
    expect(store.get('abc123')?.faceFailureCode).toBeUndefined()
  })

  it('bounds an unbounded stored text on the way out, not only on the way in', async () => {
    const store = await loadWithLine(
      JSON.stringify({
        ...base,
        relativePath: 'a.jpg',
        ocrStatus: 'done',
        ocrVersion: OCR_MODEL_VERSION,
        ocrText: 'y'.repeat(MAX_OCR_TEXT_CHARS * 4),
        ocrTokens: Array.from({ length: MAX_OCR_TOKENS * 4 }, () => 'tok')
      })
    )
    expect(store.get('abc123')!.ocrText!.length).toBe(MAX_OCR_TEXT_CHARS)
    expect(store.get('abc123')!.ocrTokens!.length).toBe(MAX_OCR_TOKENS)
  })

  it('ignores a Phase-2 field carrying no version stamp', async () => {
    const store = await loadWithLine(
      JSON.stringify({ ...base, relativePath: 'a.jpg', faceStatus: 'done', visibleFaceCount: 4 })
    )
    expect(store.get('abc123')?.faceStatus).toBeUndefined()
    expect(store.get('abc123')?.visibleFaceCount).toBeUndefined()
  })
})

describe('the index never persists anything derived from an image beyond counts and text', () => {
  it('writes no absolute path, bitmap, or embedding into a Phase-2 journal line', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'aadhaar 1234', tokens: ['aadhaar', '1234'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 2, uncertainFaceCount: 1 }, 1)
    await store.flush()

    const journal = await readFile(join(store.activeDirectory(), INDEX_JOURNAL_FILE), 'utf8')
    expect(journal).not.toMatch(/[A-Za-z]:\\/)
    expect(journal).not.toMatch(/faceBox|boundingBox|embedding|descriptor|landmark|crop/i)
    expect(journal).not.toMatch(/identity|personName|whoIs/i)

    // Only the two counts describe the faces. Nothing locates them.
    const line = JSON.parse(journal.trim().split('\n').pop()!) as Record<string, unknown>
    const faceKeys = Object.keys(line).filter((key) => key.toLowerCase().includes('face'))
    expect(faceKeys.sort()).toEqual(['faceAttempts', 'faceStatus', 'faceVersion', 'uncertainFaceCount', 'visibleFaceCount'].sort())
  })

  it('keeps the pointer and journal where Phase 1 left them', async () => {
    const store = await loadedStore()
    await store.flush()
    const pointer = await readFile(join(photoIndexDirectory(userDataDir), INDEX_POINTER_FILE), 'utf8')
    expect(pointer.trim()).toMatch(/^gen-\d+$/)
  })
})
