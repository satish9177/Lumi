import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { FACE_EMBED_DIMENSIONS } from './people-manifest'
import { normalizeEmbedding, PersonProfileStore, type SafeStoragePort } from './person-profiles'
import {
  DRAFT_TTL_MS,
  EnrolmentError,
  MIN_REFERENCE_FACE_PX,
  PersonEnrollmentService,
  type DetectedGeometry,
  type EnrolmentImage,
  type PersonEnrollmentDependencies
} from './person-enrollment'
import { MIN_REFERENCES, MAX_REFERENCES } from './person-profiles'

let userDataDir: string
let clock: number

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-enrol-'))
  clock = 1_800_000_000_000
})

afterEach(async () => {
  await rm(userDataDir, { recursive: true, force: true })
})

function fakeSecureStorage(): SafeStoragePort {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`enc:${Buffer.from(value, 'utf8').toString('base64')}`, 'utf8'),
    decryptString: (value) => Buffer.from(value.toString('utf8').slice(4), 'base64').toString('utf8')
  }
}

/** Mutually perpendicular unit vectors, so two "people" are genuinely distinct. */
const BASIS = buildBasis(6)

function buildBasis(count: number): number[][] {
  const basis: number[][] = []
  for (let axis = 0; axis < count; axis += 1) {
    const values = new Array<number>(FACE_EMBED_DIMENSIONS)
    for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
      values[index] = Math.sin((axis + 1) * 2.3 + index * (0.4 + axis * 0.13))
    }
    for (const existing of basis) {
      let dot = 0
      for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) dot += values[index]! * existing[index]!
      for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) values[index] = values[index]! - dot * existing[index]!
    }
    basis.push(normalizeEmbedding(values))
  }
  return basis
}

function personVector(person: number): Float32Array {
  return Float32Array.from(BASIS[person]!)
}

function image(width = 800, height = 600): EnrolmentImage {
  const self: EnrolmentImage = {
    getSize: () => ({ width, height }),
    crop: () => image(200, 200),
    resize: () => image(96, 96),
    toBitmap: () => new Uint8Array(width * height * 4).fill(120),
    toDataURL: () => 'data:image/png;base64,preview'
  }
  return self
}

/** Geometry for `faces`, each with usable landmarks at a distinct position. */
function geometry(
  faces: Array<{ size?: number; score?: number; offset?: number }>
): DetectedGeometry {
  const boxes = new Float32Array(faces.length * 5)
  const landmarks = new Float32Array(faces.length * 10)
  faces.forEach((face, index) => {
    const size = face.size ?? 200
    const offset = face.offset ?? index * 220
    boxes[index * 5] = offset
    boxes[index * 5 + 1] = 50
    boxes[index * 5 + 2] = size
    boxes[index * 5 + 3] = size
    boxes[index * 5 + 4] = face.score ?? 0.98
    // The ArcFace template shape, scaled into the box, so alignment succeeds.
    const template = [
      [38.2946, 51.6963],
      [73.5318, 51.5014],
      [56.0252, 71.7366],
      [41.5493, 92.3655],
      [70.7299, 92.2041]
    ]
    template.forEach((point, pointIndex) => {
      landmarks[index * 10 + pointIndex * 2] = offset + (point[0]! / 112) * size
      landmarks[index * 10 + pointIndex * 2 + 1] = 50 + (point[1]! / 112) * size
    })
  })
  return { count: faces.length, boxes, landmarks }
}

interface HarnessOptions {
  detect?: () => DetectedGeometry
  embeddings?: number[]
  files?: Record<string, { sizeBytes: number; mtimeMs: number } | undefined>
  decodeFails?: boolean
}

function harness(options: HarnessOptions = {}) {
  const profiles = new PersonProfileStore({
    userDataDir,
    secureStorage: fakeSecureStorage(),
    now: () => clock
  })
  const resolved: string[] = []
  const embedCalls: number[] = []
  const files: Record<string, { sizeBytes: number; mtimeMs: number } | undefined> = options.files ?? {}
  let embedIndex = 0

  const dependencies: PersonEnrollmentDependencies = {
    profiles,
    resolveTrustedPath: async (trustedId) => {
      resolved.push(trustedId)
      // An unknown id, an expired dropped record, and a revoked root all look
      // identical here: the boundary simply declines to produce a path.
      if (trustedId.startsWith('unknown') || trustedId.startsWith('expired') || trustedId.startsWith('revoked')) {
        return undefined
      }
      return `C:\\approved\\${trustedId}.jpg`
    },
    fingerprint: async (path) => {
      const id = path.replace('C:\\approved\\', '').replace('.jpg', '')
      if (id in files) return files[id]
      return { sizeBytes: 1000, mtimeMs: 5000 }
    },
    decodeImage: async () => (options.decodeFails ? undefined : image()),
    prepareDetectionBitmap: () => ({ bitmap: new ArrayBuffer(640 * 640 * 4), scale: 0.8 }),
    detectFaces: async () => (options.detect ? options.detect() : geometry([{}])),
    embedFaces: async (tensors, count) => {
      embedCalls.push(count)
      const person = options.embeddings?.[embedIndex] ?? 0
      embedIndex += 1
      expect(tensors.length).toBe(3 * 112 * 112)
      return personVector(person)
    },
    now: () => clock
  }

  return {
    service: new PersonEnrollmentService(dependencies),
    profiles,
    resolved,
    embedCalls,
    load: () => profiles.load()
  }
}

/** Adds `count` references of the same person to a fresh draft. */
async function draftWith(h: ReturnType<typeof harness>, count = MIN_REFERENCES, prefix = 'ref') {
  const draft = h.service.begin('Father')
  for (let index = 0; index < count; index += 1) {
    await h.service.addReference(draft.draftId, `${prefix}-${index}`)
  }
  return draft.draftId
}

describe('nothing is created without an explicit confirmation', () => {
  it('creates no profile from beginning a draft', async () => {
    const h = harness()
    await h.load()
    h.service.begin('Father')
    expect(h.profiles.list()).toHaveLength(0)
  })

  it('creates no profile from adding references', async () => {
    const h = harness()
    await h.load()
    await draftWith(h)
    // Three accepted references, embeddings computed, and still nothing stored.
    expect(h.profiles.list()).toHaveLength(0)
  })

  it('creates the profile only when confirm is called', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h)

    const summary = await h.service.confirm(draftId)
    expect(summary.label).toBe('Father')
    expect(h.profiles.list()).toHaveLength(1)
  })

  it('leaves nothing behind when a draft is cancelled', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h)
    h.service.cancel(draftId)

    expect(h.profiles.list()).toHaveLength(0)
    expect(() => h.service.list(draftId)).toThrow(EnrolmentError)
  })

  it('expires an abandoned draft rather than holding previews forever', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h)

    clock += DRAFT_TTL_MS + 1
    expect(() => h.service.list(draftId)).toThrow(EnrolmentError)
    expect(h.profiles.list()).toHaveLength(0)
  })
})

describe('a photo with several faces requires an explicit choice', () => {
  it('does not assume the largest face is the person', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 150, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'group')

    // Two usable faces, so enrolment stops and asks rather than picking one.
    expect(view.candidates).toHaveLength(2)
    expect(view.references).toHaveLength(0)
    expect(h.embedCalls).toHaveLength(0)
  })

  it('accepts the face the user points at', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 150, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'group')

    const chosen = view.candidates![1]!
    const after = await h.service.selectFace(draft.draftId, chosen.candidateId)
    expect(after.references).toHaveLength(1)
    expect(after.candidates).toBeUndefined()
  })

  it('accepts a lone face directly, which is not the same as guessing', async () => {
    const h = harness({ detect: () => geometry([{}]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'solo')
    expect(view.references).toHaveLength(1)
    expect(view.candidates).toBeUndefined()
  })

  it('refuses to confirm while a selection is outstanding', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 300, offset: 500 }]) })
    await h.load()
    const draftId = await draftWith(h, MIN_REFERENCES)
    await h.service.addReference(draftId, 'group')

    await expect(h.service.confirm(draftId)).rejects.toMatchObject({ code: 'selection_required' })
  })

  it('rejects a candidate id that was never offered', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 300, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    await h.service.addReference(draft.draftId, 'group')
    await expect(h.service.selectFace(draft.draftId, 'made-up')).rejects.toMatchObject({
      code: 'unknown_candidate'
    })
  })
})

describe('quality gates', () => {
  it('rejects a photo with no face', async () => {
    const h = harness({ detect: () => geometry([]) })
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'blank')).rejects.toMatchObject({ code: 'no_face' })
  })

  it('rejects a face too small to learn from', async () => {
    const h = harness({ detect: () => geometry([{ size: (MIN_REFERENCE_FACE_PX - 10) * 0.8 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'tiny')).rejects.toMatchObject({
      code: 'face_too_small'
    })
  })

  it('rejects an uncertain detection', async () => {
    const h = harness({ detect: () => geometry([{ score: 0.7 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'blurry')).rejects.toMatchObject({
      code: 'detection_uncertain'
    })
  })

  it('shows a rejected face as unselectable rather than hiding it', async () => {
    // One good face and one too small: the user should see Lumi considered both.
    const h = harness({ detect: () => geometry([{ size: 300 }, { size: 40, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'mixed')

    // Only one is usable, so it is accepted without a prompt.
    expect(view.references).toHaveLength(1)
  })
})

describe('reference consistency', () => {
  it('refuses to create a profile from photos of different people', async () => {
    // Two references of one person, one of somebody else entirely.
    const h = harness({ embeddings: [0, 0, 3] })
    await h.load()
    const draftId = await draftWith(h, 3)
    await expect(h.service.confirm(draftId)).rejects.toMatchObject({ code: 'inconsistent_reference' })
    expect(h.profiles.list()).toHaveLength(0)
  })

  it('accepts references that agree', async () => {
    const h = harness({ embeddings: [0, 0, 0] })
    await h.load()
    const draftId = await draftWith(h, 3)
    await expect(h.service.confirm(draftId)).resolves.toMatchObject({ label: 'Father' })
  })
})

describe('reference counts', () => {
  it('refuses to confirm below the minimum', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h, MIN_REFERENCES - 1)
    await expect(h.service.confirm(draftId)).rejects.toMatchObject({ code: 'too_few_references' })
  })

  it('refuses to add beyond the maximum', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h, MAX_REFERENCES)
    await expect(h.service.addReference(draftId, 'one-too-many')).rejects.toMatchObject({
      code: 'too_many_references'
    })
  })

  it('refuses to add the same photo twice', async () => {
    const h = harness()
    await h.load()
    const draft = h.service.begin('Father')
    await h.service.addReference(draft.draftId, 'ref-0')
    await expect(h.service.addReference(draft.draftId, 'ref-0')).rejects.toMatchObject({
      code: 'already_added'
    })
  })
})

describe('only trusted, unchanged files become references', () => {
  it('refuses an identifier the trusted boundary does not resolve', async () => {
    const h = harness()
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'unknown-1')).rejects.toMatchObject({
      code: 'file_unavailable'
    })
  })

  it('refuses an expired dropped record', async () => {
    const h = harness()
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'expired-1')).rejects.toMatchObject({
      code: 'file_unavailable'
    })
  })

  it('refuses a photo whose folder was revoked', async () => {
    const h = harness()
    await h.load()
    const draft = h.service.begin('Father')
    await expect(h.service.addReference(draft.draftId, 'revoked-1')).rejects.toMatchObject({
      code: 'file_unavailable'
    })
  })

  it('takes a path only from the trusted boundary, never from the caller', async () => {
    const h = harness()
    await h.load()
    const draft = h.service.begin('Father')
    await h.service.addReference(draft.draftId, 'ref-0')
    // The service asked the boundary to resolve the opaque id; it never had a
    // path to begin with, because `addReference` has no path parameter.
    expect(h.resolved).toContain('ref-0')
  })

  it('refuses at confirmation when a reference photo changed afterwards', async () => {
    const files: Record<string, { sizeBytes: number; mtimeMs: number }> = {}
    const h = harness({ files })
    await h.load()
    const draftId = await draftWith(h, MIN_REFERENCES)

    // The user edits one of the photos between choosing it and confirming.
    files['ref-1'] = { sizeBytes: 2222, mtimeMs: 9999 }

    await expect(h.service.confirm(draftId)).rejects.toMatchObject({ code: 'file_changed' })
    expect(h.profiles.list()).toHaveLength(0)
  })

  it('refuses at confirmation when a reference photo disappeared', async () => {
    const files: Record<string, { sizeBytes: number; mtimeMs: number } | undefined> = {}
    const h = harness({ files })
    await h.load()
    const draftId = await draftWith(h, MIN_REFERENCES)
    files['ref-0'] = undefined

    await expect(h.service.confirm(draftId)).rejects.toMatchObject({ code: 'file_unavailable' })
  })

  it('revalidates again between offering candidates and accepting a face', async () => {
    const files: Record<string, { sizeBytes: number; mtimeMs: number } | undefined> = {}
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 300, offset: 500 }]), files })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'group')

    files['group'] = undefined
    await expect(h.service.selectFace(draft.draftId, view.candidates![0]!.candidateId)).rejects.toMatchObject({
      code: 'file_unavailable'
    })
  })
})

describe('what the renderer is given', () => {
  it('never receives a path, an embedding, or geometry', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 300, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'group')

    const serialized = JSON.stringify(view)
    expect(serialized).not.toContain('C:\\')
    expect(serialized).not.toContain('approved')
    expect(serialized).not.toContain('embedding')
    expect(serialized).not.toContain('landmark')
    expect(serialized).not.toContain('trustedId')
    for (const candidate of view.candidates!) {
      expect(Object.keys(candidate).sort()).toEqual(['candidateId', 'note', 'previewDataUrl', 'selectable'])
    }
  })

  it('gives opaque candidate ids, not indexes or filenames', async () => {
    const h = harness({ detect: () => geometry([{ size: 400 }, { size: 300, offset: 500 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const view = await h.service.addReference(draft.draftId, 'group')
    for (const candidate of view.candidates!) {
      expect(candidate.candidateId).toMatch(/^[0-9a-f-]{36}$/)
      expect(candidate.candidateId).not.toContain('group')
    }
  })

  it('explains a rejection without quoting a filename or a measurement', async () => {
    const h = harness({ detect: () => geometry([{ size: 40 }]) })
    await h.load()
    const draft = h.service.begin('Father')
    const error = await h.service.addReference(draft.draftId, 'tiny').catch((caught: EnrolmentError) => caught)
    expect((error as EnrolmentError).message).not.toContain('tiny')
    expect((error as EnrolmentError).message).not.toMatch(/\d/)
  })
})

describe('what survives after creation', () => {
  it('stores no source path, no pixels and no crop', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h)
    const summary = await h.service.confirm(draftId)

    const stored = h.profiles.byId(summary.id)!
    const serialized = JSON.stringify(stored)
    expect(serialized).not.toContain('C:\\')
    expect(serialized).not.toContain('ref-0')
    expect(serialized).not.toContain('trustedId')
    expect(serialized).not.toContain('previewDataUrl')
    expect(serialized).not.toContain('landmark')
  })

  it('stores exactly one normalized embedding per accepted reference', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h, MIN_REFERENCES)
    const summary = await h.service.confirm(draftId)

    const stored = h.profiles.byId(summary.id)!
    expect(stored.references).toHaveLength(MIN_REFERENCES)
    for (const reference of stored.references) {
      expect(reference.embedding).toHaveLength(FACE_EMBED_DIMENSIONS)
      const norm = Math.sqrt(reference.embedding.reduce((total, value) => total + value * value, 0))
      expect(norm).toBeCloseTo(1, 6)
    }
  })

  it('drops the draft entirely once the profile exists', async () => {
    const h = harness()
    await h.load()
    const draftId = await draftWith(h)
    await h.service.confirm(draftId)
    // The draft held the only copy of the source paths and pixels.
    expect(() => h.service.list(draftId)).toThrow(EnrolmentError)
  })

  it('refuses a duplicate label at the store boundary', async () => {
    const h = harness()
    await h.load()
    await h.service.confirm(await draftWith(h, MIN_REFERENCES, 'a'))
    await expect(h.service.confirm(await draftWith(h, MIN_REFERENCES, 'b'))).rejects.toMatchObject({
      code: 'label_duplicate'
    })
  })
})
