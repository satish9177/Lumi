import { mkdtemp, readFile, rm, writeFile, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { peopleDirectory, PEOPLE_PROFILE_FILE } from './model-location'
import { FACE_EMBED_DIMENSIONS, FACE_EMBED_MODEL_VERSION } from './people-manifest'
import {
  cosineSimilarity,
  MAX_LABEL_LENGTH,
  MAX_PROFILES,
  MAX_REFERENCES,
  MIN_REFERENCES,
  normalizeEmbedding,
  normalizeLabel,
  PersonProfileError,
  PersonProfileStore,
  type SafeStoragePort,
  type StoredReference
} from './person-profiles'

let userDataDir: string

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-people-'))
})

afterEach(async () => {
  await rm(userDataDir, { recursive: true, force: true })
})

/**
 * A stand-in for Electron's safeStorage. It genuinely transforms the bytes, so
 * a test asserting "the file on disk is not plaintext" is testing the store's
 * behaviour rather than a no-op fake.
 */
function fakeSecureStorage(available = true): SafeStoragePort {
  return {
    isEncryptionAvailable: () => available,
    encryptString: (value) => Buffer.from(`enc:${Buffer.from(value, 'utf8').toString('base64')}`, 'utf8'),
    decryptString: (value) => {
      const text = value.toString('utf8')
      if (!text.startsWith('enc:')) {
        throw new Error('not encrypted by this port')
      }
      return Buffer.from(text.slice(4), 'base64').toString('utf8')
    }
  }
}

/** A deterministic unit vector, distinct per seed. */
function embedding(seed: number): number[] {
  const values = new Array<number>(FACE_EMBED_DIMENSIONS)
  for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
    values[index] = Math.sin(seed * 12.9898 + index * 78.233)
  }
  return normalizeEmbedding(values)
}

function reference(seed: number): StoredReference {
  return {
    id: `ref-${seed}`,
    embedding: embedding(seed),
    quality: { detectionScore: 0.98, faceSizePx: 220 },
    addedAt: '2026-01-01T00:00:00.000Z'
  }
}

function references(count = MIN_REFERENCES): StoredReference[] {
  return Array.from({ length: count }, (_unused, index) => reference(index + 1))
}

async function store(options: { available?: boolean } = {}): Promise<PersonProfileStore> {
  const created = new PersonProfileStore({
    userDataDir,
    secureStorage: fakeSecureStorage(options.available ?? true),
    now: () => 1_800_000_000_000
  })
  await created.load()
  return created
}

describe('creating a profile', () => {
  it('requires the minimum number of references', async () => {
    const people = await store()
    await expect(people.create('Father', references(MIN_REFERENCES - 1))).rejects.toMatchObject({
      code: 'too_few_references'
    })
    expect(people.list()).toHaveLength(0)
  })

  it('accepts a profile at the minimum and reports its reference count', async () => {
    const people = await store()
    const summary = await people.create('Father', references(3))
    expect(summary.label).toBe('Father')
    expect(summary.referenceCount).toBe(3)
    expect(summary.status).toBe('ready')
  })

  it('refuses more references than it will keep', async () => {
    const people = await store()
    await expect(people.create('Father', references(MAX_REFERENCES + 1))).rejects.toMatchObject({
      code: 'too_many_references'
    })
  })

  it('bounds the number of people', async () => {
    const people = await store()
    for (let index = 0; index < MAX_PROFILES; index += 1) {
      await people.create(`Person ${index}`, references())
    }
    await expect(people.create('One more', references())).rejects.toMatchObject({ code: 'too_many_profiles' })
    expect(people.list()).toHaveLength(MAX_PROFILES)
  })

  it('rejects an empty or oversized label', async () => {
    const people = await store()
    await expect(people.create('   ', references())).rejects.toMatchObject({ code: 'label_empty' })
    await expect(people.create('a'.repeat(MAX_LABEL_LENGTH + 1), references())).rejects.toMatchObject({
      code: 'label_too_long'
    })
  })
})

describe('label uniqueness is case-insensitive', () => {
  it('treats differently-cased labels as the same person', async () => {
    const people = await store()
    await people.create('Father', references())
    await expect(people.create('father', references())).rejects.toMatchObject({ code: 'label_duplicate' })
    await expect(people.create('  FATHER  ', references())).rejects.toMatchObject({ code: 'label_duplicate' })
  })

  it('normalizes whitespace and unicode form', () => {
    expect(normalizeLabel('  Father  ')).toBe('father')
    expect(normalizeLabel('Fáther')).toBe(normalizeLabel('Fáther'))
    expect(normalizeLabel('Two   Words')).toBe('two words')
  })

  it('resolves a label to a profile whatever its casing', async () => {
    const people = await store()
    const created = await people.create('Father', references())
    expect(people.resolveLabel('FATHER')?.id).toBe(created.id)
    expect(people.resolveLabel(' father ')?.id).toBe(created.id)
  })

  it('returns nothing for a label that was never enrolled, and creates nothing', async () => {
    const people = await store()
    expect(people.resolveLabel('Father')).toBeUndefined()
    // The critical half of that assertion: asking did not enrol anyone.
    expect(people.list()).toHaveLength(0)
  })
})

describe('renaming', () => {
  it('changes the label without touching the references', async () => {
    const people = await store()
    const created = await people.create('Father', references())
    const before = people.byId(created.id)!.references.map((entry) => entry.embedding)

    const renamed = await people.rename(created.id, 'Dad')
    expect(renamed.label).toBe('Dad')
    expect(people.resolveLabel('Dad')?.id).toBe(created.id)
    expect(people.resolveLabel('Father')).toBeUndefined()
    expect(people.byId(created.id)!.references.map((entry) => entry.embedding)).toEqual(before)
  })

  it('refuses a rename onto another person’s label', async () => {
    const people = await store()
    const father = await people.create('Father', references())
    await people.create('Mother', references())
    await expect(people.rename(father.id, 'mother')).rejects.toMatchObject({ code: 'label_duplicate' })
  })

  it('allows renaming to a different casing of the same label', async () => {
    const people = await store()
    const created = await people.create('father', references())
    const renamed = await people.rename(created.id, 'Father')
    expect(renamed.label).toBe('Father')
  })

  it('refuses to rename a profile that no longer exists', async () => {
    const people = await store()
    await expect(people.rename('nope', 'Father')).rejects.toMatchObject({ code: 'unknown_profile' })
  })
})

describe('references can be added and removed', () => {
  it('adds a reference and marks the profile as needing a rescan', async () => {
    const people = await store()
    const created = await people.create('Father', references())
    const updated = await people.addReference(created.id, reference(9))
    expect(updated.referenceCount).toBe(4)
    // New evidence changes what the profile matches, so old outcomes are stale.
    expect(updated.status).toBe('needs_rescan')
  })

  it('refuses to add beyond the ceiling', async () => {
    const people = await store()
    const created = await people.create('Father', references(MAX_REFERENCES))
    await expect(people.addReference(created.id, reference(99))).rejects.toMatchObject({
      code: 'too_many_references'
    })
  })

  it('refuses a removal that would drop below the minimum', async () => {
    const people = await store()
    const created = await people.create('Father', references(MIN_REFERENCES))
    await expect(people.removeReference(created.id, 'ref-1')).rejects.toMatchObject({
      code: 'too_few_references'
    })
    expect(people.byId(created.id)!.references).toHaveLength(MIN_REFERENCES)
  })

  it('removes a reference when enough remain', async () => {
    const people = await store()
    const created = await people.create('Father', references(MIN_REFERENCES + 1))
    const updated = await people.removeReference(created.id, 'ref-1')
    expect(updated.referenceCount).toBe(MIN_REFERENCES)
  })
})

describe('deletion', () => {
  it('removes one profile and leaves the others', async () => {
    const people = await store()
    const father = await people.create('Father', references())
    await people.create('Mother', references())

    expect(await people.remove(father.id)).toBe(true)
    expect(people.list().map((summary) => summary.label)).toEqual(['Mother'])
    expect(people.resolveLabel('Father')).toBeUndefined()
    expect(people.byId(father.id)).toBeUndefined()
  })

  it('reports a delete of something already gone rather than throwing', async () => {
    const people = await store()
    expect(await people.remove('nope')).toBe(false)
  })

  it('leaves no file behind when all people data is deleted', async () => {
    const people = await store()
    await people.create('Father', references())
    expect(await readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE)).catch(() => undefined)).toBeDefined()

    await people.removeAll()

    expect(people.list()).toHaveLength(0)
    // An empty encrypted document would still be a file that once held face
    // data. The directory itself has to go.
    await expect(readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE))).rejects.toThrow()
    const survivors = await readFile(peopleDirectory(userDataDir)).catch((error: NodeJS.ErrnoException) => error.code)
    expect(survivors).toBe('ENOENT')
  })

  it('deletes nothing outside its own directory', async () => {
    const people = await store()
    const neighbour = join(userDataDir, 'photo-index')
    await mkdir(neighbour, { recursive: true })
    await writeFile(join(neighbour, 'vectors.bin'), 'clip data')

    await people.create('Father', references())
    await people.removeAll()

    // The photo index is a sibling directory and must survive intact.
    expect(await readFile(join(neighbour, 'vectors.bin'), 'utf8')).toBe('clip data')
  })
})

describe('persistence', () => {
  it('round-trips through a reload', async () => {
    const first = await store()
    const created = await first.create('Father', references())

    const second = await store()
    expect(second.list().map((summary) => summary.label)).toEqual(['Father'])
    expect(second.byId(created.id)!.references).toHaveLength(MIN_REFERENCES)
  })

  it('writes nothing readable as plaintext', async () => {
    const people = await store()
    await people.create('Father', references())

    const raw = await readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE), 'utf8')
    // The label and the JSON structure must not be sitting in the clear.
    expect(raw).not.toContain('Father')
    expect(raw).not.toContain('normalizedLabel')
    expect(raw.startsWith('enc:')).toBe(true)
  })

  it('refuses to persist when the device cannot encrypt', async () => {
    const people = await store({ available: false })
    await expect(people.create('Father', references())).rejects.toMatchObject({
      code: 'storage_unavailable'
    })
    // Nothing was written in the clear as a fallback.
    await expect(readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE))).rejects.toThrow()
  })

  it('leaves no partial document when a write is interrupted', async () => {
    const people = await store()
    await people.create('Father', references())
    const good = await readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE))

    // A temp file left by a crashed write must not be mistaken for the document.
    await writeFile(join(peopleDirectory(userDataDir), `${PEOPLE_PROFILE_FILE}.tmp`), 'torn')
    const reloaded = await store()
    expect(reloaded.list()).toHaveLength(1)
    expect(await readFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE))).toEqual(good)
  })

  it('serializes concurrent writes rather than interleaving them', async () => {
    const people = await store()
    await Promise.all([
      people.create('Father', references()),
      people.create('Mother', references()),
      people.create('Sister', references())
    ])

    const reloaded = await store()
    expect(reloaded.list().map((summary) => summary.label).sort()).toEqual(['Father', 'Mother', 'Sister'])
  })
})

describe('corruption recovery', () => {
  it('recovers to an empty store rather than crashing', async () => {
    await mkdir(peopleDirectory(userDataDir), { recursive: true })
    await writeFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE), 'not decryptable')

    const people = await store()
    expect(people.list()).toHaveLength(0)
    // Reported, not hidden: "no people" and "your people could not be read" are
    // different statements and the user deserves the accurate one.
    expect(people.recoveredFromCorruption()).toBe(true)
  })

  it('does not log the document it failed to read', async () => {
    await mkdir(peopleDirectory(userDataDir), { recursive: true })
    await writeFile(join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE), 'enc:bm90IGpzb24=')

    const spies = [
      vi.spyOn(console, 'log').mockImplementation(() => undefined),
      vi.spyOn(console, 'warn').mockImplementation(() => undefined),
      vi.spyOn(console, 'error').mockImplementation(() => undefined),
      vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    ]
    try {
      await store()
      for (const spy of spies) {
        expect(spy).not.toHaveBeenCalled()
      }
    } finally {
      for (const spy of spies) spy.mockRestore()
    }
  })

  it('drops a profile whose references are the wrong width', async () => {
    const secure = fakeSecureStorage()
    await mkdir(peopleDirectory(userDataDir), { recursive: true })
    const document = {
      version: 1,
      profiles: [
        {
          id: 'p1',
          label: 'Father',
          normalizedLabel: 'father',
          modelVersion: FACE_EMBED_MODEL_VERSION,
          indexVersion: 1,
          references: [{ id: 'r1', embedding: [0.1, 0.2], quality: {}, addedAt: 'x' }],
          createdAt: 'x',
          updatedAt: 'x'
        }
      ]
    }
    await writeFile(
      join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE),
      secure.encryptString(JSON.stringify(document))
    )

    const people = await store()
    // A vector of the wrong width would compare meaninglessly against every
    // face, so the profile is dropped rather than half-loaded.
    expect(people.list()).toHaveLength(0)
  })

  it('keeps only the first of two profiles sharing a normalized label', async () => {
    const secure = fakeSecureStorage()
    await mkdir(peopleDirectory(userDataDir), { recursive: true })
    const profile = (id: string, label: string) => ({
      id,
      label,
      normalizedLabel: 'father',
      modelVersion: FACE_EMBED_MODEL_VERSION,
      indexVersion: 1,
      references: [
        { id: `${id}-r`, embedding: embedding(1), quality: { detectionScore: 1, faceSizePx: 100 }, addedAt: 'x' }
      ],
      createdAt: 'x',
      updatedAt: 'x'
    })
    await writeFile(
      join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE),
      secure.encryptString(JSON.stringify({ version: 1, profiles: [profile('p1', 'Father'), profile('p2', 'father')] }))
    )

    const people = await store()
    expect(people.list()).toHaveLength(1)
    expect(people.byId('p1')).toBeDefined()
  })
})

describe('model-version invalidation', () => {
  it('marks a profile from another embedding model as needing re-enrolment', async () => {
    const secure = fakeSecureStorage()
    await mkdir(peopleDirectory(userDataDir), { recursive: true })
    await writeFile(
      join(peopleDirectory(userDataDir), PEOPLE_PROFILE_FILE),
      secure.encryptString(
        JSON.stringify({
          version: 1,
          profiles: [
            {
              id: 'p1',
              label: 'Father',
              normalizedLabel: 'father',
              modelVersion: FACE_EMBED_MODEL_VERSION + 1,
              indexVersion: 1,
              references: [
                { id: 'r1', embedding: embedding(1), quality: { detectionScore: 1, faceSizePx: 100 }, addedAt: 'x' }
              ],
              createdAt: 'x',
              updatedAt: 'x'
            }
          ]
        })
      )
    )

    const people = await store()
    expect(people.list()[0]!.status).toBe('needs_reenrolment')
    // Excluded from matching: a vector from another model means nothing here.
    expect(people.matchable()).toHaveLength(0)
    // But not destroyed — the user's enrolment work is still theirs.
    expect(people.byId('p1')).toBeDefined()
  })

  it('marks a stale index version as needing a rescan, still matchable', async () => {
    const people = await store()
    const created = await people.create('Father', references())
    await people.invalidateScan(created.id)

    expect(people.list()[0]!.status).toBe('needs_rescan')
    expect(people.matchable().map((profile) => profile.id)).toEqual([created.id])

    await people.markScanned(created.id)
    expect(people.list()[0]!.status).toBe('ready')
  })
})

describe('what leaves this module', () => {
  it('never puts an embedding in a summary', async () => {
    const people = await store()
    await people.create('Father', references())

    const summaries = people.list()
    const serialized = JSON.stringify(summaries)
    expect(serialized).not.toContain('embedding')
    for (const summary of summaries) {
      expect(Object.keys(summary).sort()).toEqual([
        'createdAt',
        'id',
        'label',
        'referenceCount',
        'status',
        'updatedAt'
      ])
    }
  })

  it('gives opaque ids that cannot be derived from the label', async () => {
    const people = await store()
    const created = await people.create('Father', references())
    expect(created.id).not.toContain('Father')
    expect(created.id.toLowerCase()).not.toContain('father')
    expect(created.id).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('carries no label or vector in its error messages', async () => {
    const people = await store()
    await people.create('Father', references())
    const error = await people.create('Father', references()).catch((caught: PersonProfileError) => caught)
    expect(error).toBeInstanceOf(PersonProfileError)
    expect((error as PersonProfileError).message).not.toContain('Father')
  })
})

describe('embedding handling', () => {
  it('rejects a vector of the wrong width', () => {
    expect(() => normalizeEmbedding([1, 2, 3])).toThrow(PersonProfileError)
  })

  it('rejects a non-finite or degenerate vector', () => {
    const withNaN = new Array<number>(FACE_EMBED_DIMENSIONS).fill(0.1)
    withNaN[5] = Number.NaN
    expect(() => normalizeEmbedding(withNaN)).toThrow(PersonProfileError)
    expect(() => normalizeEmbedding(new Array<number>(FACE_EMBED_DIMENSIONS).fill(0))).toThrow(PersonProfileError)
  })

  it('returns a unit vector, because the model does not', () => {
    // SFace output measured an L2 norm around 4.8, so normalization is ours to do.
    const raw = new Array<number>(FACE_EMBED_DIMENSIONS).fill(3)
    const unit = normalizeEmbedding(raw)
    const norm = Math.sqrt(unit.reduce((total, value) => total + value * value, 0))
    expect(norm).toBeCloseTo(1, 10)
  })

  it('scores an identical vector at 1 and an opposed one at -1', () => {
    const first = embedding(1)
    const opposed = first.map((value) => -value)
    expect(cosineSimilarity(first, first)).toBeCloseTo(1, 10)
    expect(cosineSimilarity(first, opposed)).toBeCloseTo(-1, 10)
  })

  it('scores mismatched widths as zero rather than throwing', () => {
    expect(cosineSimilarity([1, 0], [1, 0, 0])).toBe(0)
  })
})
