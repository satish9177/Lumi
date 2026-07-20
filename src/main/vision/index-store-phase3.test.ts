/**
 * Phase-3 match records in the photo index.
 *
 * The properties under test here are mostly *negative* ones — what a match
 * record cannot carry, what a version bump must not destroy, and what a deletion
 * must not leave behind. Those are the claims the privacy documentation makes,
 * so they are the ones worth pinning down.
 */

import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { FACE_MODEL_VERSION, OCR_MODEL_VERSION } from './extras-manifest'
import {
  computeImageId,
  MAX_PEOPLE_MATCH_RECORDS,
  PhotoIndexStore,
  type PeopleMatchRecord,
  type PhotoIndexRecord
} from './index-store'
import { INDEX_JOURNAL_FILE, photoIndexDirectory } from './model-location'
import { FACE_EMBED_MODEL_VERSION, PEOPLE_INDEX_VERSION } from './people-manifest'
import { CLIP_EMBEDDING_LENGTH } from './protocol'

const MODEL_VERSION = 1
const PHASE2 = { ocr: OCR_MODEL_VERSION, face: FACE_MODEL_VERSION }
const PEOPLE = { model: FACE_EMBED_MODEL_VERSION, index: PEOPLE_INDEX_VERSION }

let userDataDir: string

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-phase3-'))
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

async function loadedStore(people = PEOPLE): Promise<PhotoIndexStore> {
  const store = new PhotoIndexStore(userDataDir)
  await store.load(MODEL_VERSION, PHASE2, people)
  return store
}

async function withVector(
  store: PhotoIndexStore,
  overrides: Partial<PhotoIndexRecord> = {}
): Promise<PhotoIndexRecord> {
  const row = await store.appendVector(new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.1))
  const stored = record({ status: 'indexed', vectorRow: row, ...overrides })
  await store.put(stored)
  return stored
}

function match(overrides: Partial<PeopleMatchRecord> = {}): PeopleMatchRecord {
  return { profileId: 'profile-a', status: 'likely', matchingFaces: 1, profileRevision: 1, ...overrides }
}

async function journalText(): Promise<string> {
  const store = new PhotoIndexStore(userDataDir)
  await store.load(MODEL_VERSION, PHASE2, PEOPLE)
  return readFile(join(store.activeDirectory(), INDEX_JOURNAL_FILE), 'utf8').catch(() => '')
}

describe('match records survive a reload', () => {
  it('stores and reads back a likely match', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)

    const reloaded = await loadedStore()
    const read = reloaded.get(stored.imageId)
    expect(read?.peopleStatus).toBe('done')
    expect(read?.peopleMatches).toEqual([match()])
  })

  it('replaces the previous match set wholesale rather than merging', async () => {
    // A rescan that no longer finds someone must not leave their old answer
    // sitting beside the new ones.
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      { status: 'done', matches: [match({ profileId: 'a' }), match({ profileId: 'b' })] },
      1
    )
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match({ profileId: 'a' })] }, 2)

    expect(store.get(stored.imageId)?.peopleMatches?.map((entry) => entry.profileId)).toEqual(['a'])
  })

  it('does not resurrect a deleted record', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.markDeleted(stored.imageId, 1)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 2)

    expect(store.get(stored.imageId)?.peopleMatches).toBeUndefined()
  })
})

describe('a match record cannot carry anything biometric', () => {
  it('drops fields that are not part of the closed schema', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      {
        status: 'done',
        matches: [
          {
            ...match(),
            // Everything a careless future caller might try to smuggle through.
            similarity: 0.91,
            embedding: [0.1, 0.2],
            label: 'Father',
            box: [1, 2, 3, 4],
            referencePath: 'C:/Users/someone/father.jpg'
          } as unknown as PeopleMatchRecord
        ]
      },
      1
    )

    const journal = await journalText()
    expect(journal).not.toContain('similarity')
    expect(journal).not.toContain('embedding')
    expect(journal).not.toContain('Father')
    expect(journal).not.toContain('referencePath')
    expect(journal).not.toContain('0.91')

    const read = store.get(stored.imageId)?.peopleMatches?.[0]
    expect(Object.keys(read ?? {}).sort()).toEqual([
      'matchingFaces',
      'profileId',
      'profileRevision',
      'status'
    ])
  })

  it('refuses a status that only exists in memory', async () => {
    // `not_checked` and `checking` are derived, never written. A journal line
    // claiming either would survive a crash and become a permanent lie.
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      { status: 'done', matches: [match({ status: 'checking' }), match({ profileId: 'b', status: 'not_checked' })] },
      1
    )

    const reloaded = await loadedStore()
    expect(reloaded.get(stored.imageId)?.peopleMatches).toBeUndefined()
  })

  it('bounds the number of records and rejects duplicates', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    const many = Array.from({ length: MAX_PEOPLE_MATCH_RECORDS + 10 }, (_unused, index) =>
      match({ profileId: `profile-${index}` })
    )
    await store.recordPeople(stored.imageId, { status: 'done', matches: [...many, match({ profileId: 'profile-0' })] }, 1)

    const read = store.get(stored.imageId)?.peopleMatches ?? []
    expect(read.length).toBe(MAX_PEOPLE_MATCH_RECORDS)
    expect(new Set(read.map((entry) => entry.profileId)).size).toBe(read.length)
  })
})

describe('version changes invalidate Phase 3 and nothing else', () => {
  it('drops match records when the embedding model changes', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'invoice 4471', tokens: ['invoice', '4471'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 2, uncertainFaceCount: 0 }, 1)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)

    const reloaded = await loadedStore({ model: FACE_EMBED_MODEL_VERSION + 1, index: PEOPLE_INDEX_VERSION })
    const read = reloaded.get(stored.imageId)

    expect(read?.peopleStatus).toBeUndefined()
    expect(read?.peopleMatches).toBeUndefined()
    // The expensive work survives untouched. This is the whole reason the
    // people versions are kept out of index-meta.json.
    expect(read?.vectorRow).toBe(stored.vectorRow)
    expect(read?.ocrText).toBe('invoice 4471')
    expect(read?.visibleFaceCount).toBe(2)
  })

  it('drops match records when the matching rules change', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)

    const reloaded = await loadedStore({ model: FACE_EMBED_MODEL_VERSION, index: PEOPLE_INDEX_VERSION + 1 })
    expect(reloaded.get(stored.imageId)?.peopleMatches).toBeUndefined()
    expect(reloaded.get(stored.imageId)?.vectorRow).toBe(stored.vectorRow)
  })

  it('leaves a Phase-1 and Phase-2 journal loading unchanged', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'receipt', tokens: ['receipt'] }, 1)

    const reloaded = await loadedStore()
    const read = reloaded.get(stored.imageId)
    expect(read?.ocrText).toBe('receipt')
    expect(read?.peopleStatus).toBeUndefined()
  })
})

describe('deleting a profile leaves nothing readable', () => {
  it('removes that profile from every photo and rewrites the journal', async () => {
    const store = await loadedStore()
    const first = await withVector(store, { relativePath: 'a.jpg' })
    const second = await withVector(store, { relativePath: 'b.jpg' })
    for (const stored of [first, second]) {
      await store.recordPeople(
        stored.imageId,
        { status: 'done', matches: [match({ profileId: 'keep-me' }), match({ profileId: 'delete-me' })] },
        1
      )
    }

    const touched = await store.removeProfileRecords('delete-me', 2)
    expect(touched).toBe(2)

    // Not merely superseded by a later line: the id must be gone from the file.
    const journal = await journalText()
    expect(journal).not.toContain('delete-me')
    expect(journal).toContain('keep-me')

    const reloaded = await loadedStore()
    expect(reloaded.get(first.imageId)?.peopleMatches?.map((entry) => entry.profileId)).toEqual(['keep-me'])
  })

  it('cannot take another profile’s records with it', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      { status: 'done', matches: [match({ profileId: 'a', status: 'likely' }), match({ profileId: 'b', status: 'possible' })] },
      1
    )
    await store.removeProfileRecords('a', 2)

    expect(store.get(stored.imageId)?.peopleMatches).toEqual([
      match({ profileId: 'b', status: 'possible' })
    ])
  })
})

describe('delete-all clears Phase 3 and preserves the rest', () => {
  it('strips every people field while keeping vectors, text and counts', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordOcr(stored.imageId, { status: 'done', text: 'ticket 99', tokens: ['ticket', '99'] }, 1)
    await store.recordFaces(stored.imageId, { status: 'done', visibleFaceCount: 3, uncertainFaceCount: 1 }, 1)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)

    const touched = await store.clearPeopleRecords(2)
    expect(touched).toBe(1)

    const journal = await journalText()
    expect(journal).not.toContain('peopleMatches')
    expect(journal).not.toContain('profile-a')

    const reloaded = await loadedStore()
    const read = reloaded.get(stored.imageId)
    expect(read?.peopleStatus).toBeUndefined()
    expect(read?.peopleMatches).toBeUndefined()
    expect(read?.ocrText).toBe('ticket 99')
    expect(read?.visibleFaceCount).toBe(3)
    expect(read?.uncertainFaceCount).toBe(1)
    expect(read?.vectorRow).toBe(stored.vectorRow)
  })

  it('survives a restart without the data returning', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)
    await store.clearPeopleRecords(2)

    // Two reloads: the first proves the pointer flipped, the second proves the
    // superseded generation was not left behind for a later load to find.
    await loadedStore()
    const reloaded = await loadedStore()
    expect(reloaded.get(stored.imageId)?.peopleMatches).toBeUndefined()
    expect(await journalText()).not.toContain('profile-a')
  })
})

describe('failures are recorded without becoming answers', () => {
  it('keeps a bounded failure code and no matches', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      { status: 'failed', failureCode: 'file_locked', matches: [match()] },
      1
    )

    const read = store.get(stored.imageId)
    expect(read?.peopleFailureCode).toBe('file_locked')
    // A failed scan has no findings, whatever the caller passed.
    expect(read?.peopleMatches).toBeUndefined()
  })

  it('drops a failure code this build does not define', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(
      stored.imageId,
      { status: 'failed', failureCode: 'went_wrong' as never },
      1
    )

    const reloaded = await loadedStore()
    expect(reloaded.get(stored.imageId)?.peopleFailureCode).toBeUndefined()
  })
})

describe('the index directory never holds an absolute path', () => {
  it('writes no drive-qualified path even after a people scan', async () => {
    const store = await loadedStore()
    const stored = await withVector(store)
    await store.recordPeople(stored.imageId, { status: 'done', matches: [match()] }, 1)

    const journal = await journalText()
    expect(journal).not.toMatch(/[a-zA-Z]:[\\/]/)
    expect(journal).not.toContain(photoIndexDirectory(userDataDir))
  })
})
