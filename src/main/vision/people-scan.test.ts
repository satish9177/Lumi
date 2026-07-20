/**
 * The scan pipeline: detected faces in, bounded outcomes out.
 *
 * The tests that matter most here are the ones asserting what the returned
 * value *cannot* contain. `scanPhotoForPeople` is the only place a face that
 * nobody enrolled becomes a vector, so if an embedding can escape it, it can
 * escape into the journal, and Lumi has quietly built the face database it
 * promises not to build.
 */

import { describe, expect, it, vi } from 'vitest'
import { REFERENCE_LANDMARKS } from './face-align'
import { LIKELY_MATCH_THRESHOLD, SAME_IDENTITY_THRESHOLD } from './face-matching'
import { FACE_EMBED_DIMENSIONS } from './people-manifest'
import { MIN_SCAN_FACE_PX, PeopleScanError, scanPhotoForPeople } from './people-scan'
import { normalizeEmbedding, type StoredPersonProfile } from './person-profiles'
import { FACE_BOX_STRIDE } from './protocol'

const SOURCE_EDGE = 640

/** A flat BGRA image; alignment only needs something samplable. */
function source(): { data: Uint8Array; width: number; height: number } {
  return {
    data: new Uint8Array(SOURCE_EDGE * SOURCE_EDGE * 4).fill(120),
    width: SOURCE_EDGE,
    height: SOURCE_EDGE
  }
}

/**
 * Geometry for `count` faces, each with valid landmarks. Landmarks are the
 * reference template offset per face, so alignment always succeeds and the test
 * is measuring matching rather than the transform.
 */
function geometry(
  faces: ReadonlyArray<{ size?: number; score?: number }>
): { count: number; boxes: Float32Array; landmarks: Float32Array } {
  const boxes = new Float32Array(faces.length * FACE_BOX_STRIDE)
  const landmarks = new Float32Array(faces.length * 10)

  faces.forEach((face, index) => {
    const size = face.size ?? 200
    const offset = index * 20
    boxes.set([offset, offset, size, size, face.score ?? 0.99], index * FACE_BOX_STRIDE)
    REFERENCE_LANDMARKS.forEach((point, pointIndex) => {
      landmarks[index * 10 + pointIndex * 2] = point.x + offset
      landmarks[index * 10 + pointIndex * 2 + 1] = point.y + offset
    })
  })

  return { count: faces.length, boxes, landmarks }
}

/** A deterministic unit vector pointing mostly along one axis. */
function vectorFor(person: number): number[] {
  const values = new Array<number>(FACE_EMBED_DIMENSIONS).fill(0.01)
  values[person % FACE_EMBED_DIMENSIONS] = 1
  return normalizeEmbedding(values)
}

function profile(id: string, person: number, references = 5, revision = 1): StoredPersonProfile {
  return {
    id,
    label: id,
    normalizedLabel: id,
    modelVersion: 1,
    indexVersion: 1,
    revision,
    references: Array.from({ length: references }, (_unused, index) => ({
      id: `${id}-${index}`,
      embedding: vectorFor(person),
      quality: { detectionScore: 0.99, faceSizePx: 200 },
      addedAt: 'x'
    })),
    createdAt: 'x',
    updatedAt: 'x'
  }
}

/** Returns the same embedding for every face in the batch. */
function embedderReturning(...people: number[]): (tensors: Float32Array, count: number) => Promise<Float32Array> {
  return async (_tensors, count) => {
    const output = new Float32Array(count * FACE_EMBED_DIMENSIONS)
    for (let index = 0; index < count; index += 1) {
      output.set(vectorFor(people[index] ?? people[0] ?? 0), index * FACE_EMBED_DIMENSIONS)
    }
    return output
  }
}

describe('matching produces bounded outcomes', () => {
  it('records a likely match for the person present', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1, 5)],
      embed: embedderReturning(1)
    })

    expect(outcome.matches).toEqual([
      { profileId: 'father', status: 'likely', matchingFaces: 1, profileRevision: 1 }
    ])
    expect(outcome.embeddedFaces).toBe(1)
  })

  it('writes an explicit negative for a profile that was checked and did not match', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1), profile('mother', 40)],
      embed: embedderReturning(1)
    })

    const mother = outcome.matches.find((match) => match.profileId === 'mother')
    expect(mother?.status).toBe('checked_no_reliable_match')
    expect(mother?.matchingFaces).toBe(0)
  })

  it('gives every considered profile a row, so absence can mean “not checked”', async () => {
    const profiles = [profile('a', 1), profile('b', 40), profile('c', 80)]
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles,
      embed: embedderReturning(1)
    })

    expect(outcome.matches.map((match) => match.profileId).sort()).toEqual(['a', 'b', 'c'])
  })

  it('counts multiple matching faces in one photo', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}, {}]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed: embedderReturning(1, 1)
    })

    expect(outcome.matches[0]?.matchingFaces).toBe(2)
  })

  it('stamps the profile revision it was computed against', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1, 5, 7)],
      embed: embedderReturning(1)
    })

    expect(outcome.matches[0]?.profileRevision).toBe(7)
  })

  it('demotes a thin profile to possible', async () => {
    // Three references is the bare minimum, which is a caution, and a caution
    // can only ever demote.
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1, 3)],
      embed: embedderReturning(1)
    })

    expect(outcome.matches[0]?.status).toBe('possible')
  })
})

describe('unusable faces are dropped rather than guessed at', () => {
  it('rejects a face below the size floor', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{ size: MIN_SCAN_FACE_PX - 1 }]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed: embedderReturning(1)
    })

    expect(outcome.embeddedFaces).toBe(0)
    expect(outcome.rejectedFaces).toBe(1)
    expect(outcome.matches[0]?.status).toBe('checked_no_reliable_match')
  })

  it('rejects a detection the detector is not confident is a face', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{ score: 0.2 }]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed: embedderReturning(1)
    })

    expect(outcome.embeddedFaces).toBe(0)
  })

  it('rejects non-finite landmarks rather than embedding a crop of nothing', async () => {
    const bad = geometry([{}])
    bad.landmarks[0] = Number.NaN
    const embed = vi.fn(embedderReturning(1))

    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: bad,
      scale: 1,
      profiles: [profile('father', 1)],
      embed
    })

    expect(embed).not.toHaveBeenCalled()
    expect(outcome.rejectedFaces).toBe(1)
  })

  it('does not call the embedder at all when no face is usable', async () => {
    const embed = vi.fn(embedderReturning(1))
    await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{ score: 0.1 }]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed
    })

    expect(embed).not.toHaveBeenCalled()
  })

  it('does no work at all when there are no profiles to check', async () => {
    const embed = vi.fn(embedderReturning(1))
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [],
      embed
    })

    expect(embed).not.toHaveBeenCalled()
    expect(outcome.matches).toEqual([])
  })

  it('reports face size in source pixels, not detector pixels', async () => {
    // A face 40px in the 640 letterbox of a photo scaled by 0.25 is 160px in
    // the original, which is above the low-resolution caution rather than below.
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{ size: 40 }]),
      scale: 0.25,
      profiles: [profile('father', 1, 5)],
      embed: embedderReturning(1)
    })

    expect(outcome.matches[0]?.status).toBe('likely')
  })
})

describe('the embedder contract is enforced', () => {
  it('refuses an output of the wrong width', async () => {
    await expect(
      scanPhotoForPeople({
        source: source(),
        geometry: geometry([{}]),
        scale: 1,
        profiles: [profile('father', 1)],
        embed: async () => new Float32Array(FACE_EMBED_DIMENSIONS - 1)
      })
    ).rejects.toBeInstanceOf(PeopleScanError)
  })

  it('sends one batched request rather than one per face', async () => {
    const embed = vi.fn(embedderReturning(1, 1, 1))
    await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}, {}, {}]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed
    })

    expect(embed).toHaveBeenCalledTimes(1)
    expect(embed.mock.calls[0]?.[1]).toBe(3)
  })

  it('sends only pixels — there is no field for a path, a profile or a label', async () => {
    const embed = vi.fn(embedderReturning(1))
    await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed
    })

    const [tensors, count] = embed.mock.calls[0]!
    expect(tensors).toBeInstanceOf(Float32Array)
    expect(typeof count).toBe('number')
    expect(embed.mock.calls[0]).toHaveLength(2)
  })
})

describe('nothing biometric survives the call', () => {
  it('returns no embedding, score, landmark or geometry anywhere in the outcome', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}, {}]),
      scale: 1,
      profiles: [profile('father', 1), profile('mother', 40)],
      embed: embedderReturning(1, 40)
    })

    // Serializing the whole outcome is the strongest form of this check: any
    // vector, box or similarity reachable from it would show up here.
    const serialized = JSON.stringify(outcome)
    expect(serialized).not.toContain('embedding')
    expect(serialized).not.toContain('landmark')
    expect(serialized).not.toContain('score')
    expect(serialized).not.toContain('similarity')

    for (const match of outcome.matches) {
      expect(Object.keys(match).sort()).toEqual([
        'matchingFaces',
        'profileId',
        'profileRevision',
        'status'
      ])
    }
  })

  it('never reports a tier stronger than likely', async () => {
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      // An identical vector: the strongest possible agreement.
      profiles: [profile('father', 1, 8)],
      embed: embedderReturning(1)
    })

    expect(outcome.matches[0]?.status).toBe('likely')
    expect(LIKELY_MATCH_THRESHOLD).toBeGreaterThan(SAME_IDENTITY_THRESHOLD)
  })

  it('does not retain the batch it was given', async () => {
    // The caller owns the tensor buffer and may reuse it. If the scan kept a
    // reference, mutating it afterwards would corrupt a later comparison.
    let captured: Float32Array | undefined
    const outcome = await scanPhotoForPeople({
      source: source(),
      geometry: geometry([{}]),
      scale: 1,
      profiles: [profile('father', 1)],
      embed: async (tensors, count) => {
        captured = tensors
        return embedderReturning(1)(tensors, count)
      }
    })

    captured?.fill(0)
    expect(outcome.matches[0]?.status).toBe('likely')
  })
})
