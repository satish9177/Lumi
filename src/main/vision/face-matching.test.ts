import { describe, expect, it } from 'vitest'
import {
  AMBIGUITY_MARGIN,
  CONFIDENT_DETECTION_SCORE,
  LIKELY_MATCH_THRESHOLD,
  LOW_RESOLUTION_FACE_PX,
  MATCH_TIERS,
  NO_CAUTION,
  SAME_IDENTITY_THRESHOLD,
  THIN_PROFILE_REFERENCES,
  findInconsistentReference,
  matchImage,
  scoreAgainstProfile,
  tierFor,
  type FaceObservation
} from './face-matching'
import { normalizeEmbedding, type StoredPersonProfile } from './person-profiles'
import { FACE_EMBED_DIMENSIONS } from './people-manifest'

/**
 * A set of mutually perpendicular unit vectors, built deterministically.
 *
 * Every person in these tests gets their own axis. That matters: vectors drawn
 * from a single plane are all correlated with one another, which would make two
 * supposedly unrelated people score highly against each other and quietly
 * invalidate every assertion built on top.
 */
const BASIS = buildOrthonormalBasis(16)

function buildOrthonormalBasis(count: number): number[][] {
  const basis: number[][] = []
  for (let axis = 0; axis < count; axis += 1) {
    const values = new Array<number>(FACE_EMBED_DIMENSIONS)
    for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
      values[index] = Math.sin((axis + 1) * 1.7 + index * (0.3 + axis * 0.11))
    }
    // Gram-Schmidt against everything already accepted.
    for (const existing of basis) {
      let dot = 0
      for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
        dot += values[index]! * existing[index]!
      }
      for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
        values[index] = values[index]! - dot * existing[index]!
      }
    }
    basis.push(normalizeEmbedding(values))
  }
  return basis
}

/** The canonical face of a person: their own axis, unmixed. */
function faceOf(person: number): number[] {
  return BASIS[person]!
}

/**
 * A reference photo of `person`, at a chosen similarity to their canonical
 * face. The remainder is spent along a noise axis belonging to nobody, so a
 * reference of one person stays perpendicular to every other person.
 */
function referenceOf(person: number, similarity: number, noiseAxis: number): number[] {
  const identity = BASIS[person]!
  const noise = BASIS[8 + (noiseAxis % 8)]!
  const remainder = Math.sqrt(Math.max(0, 1 - similarity * similarity))
  const values = new Array<number>(FACE_EMBED_DIMENSIONS)
  for (let index = 0; index < FACE_EMBED_DIMENSIONS; index += 1) {
    values[index] = similarity * identity[index]! + remainder * noise[index]!
  }
  return normalizeEmbedding(values)
}

/** Several references of one person, each with its own noise direction. */
function referencesOf(person: number, count: number, similarity: number): number[][] {
  return Array.from({ length: count }, (_unused, index) => referenceOf(person, similarity, index))
}

function profile(id: string, embeddings: number[][], label = id): StoredPersonProfile {
  return {
    id,
    label,
    normalizedLabel: label.toLowerCase(),
    modelVersion: 1,
    indexVersion: 1,
    references: embeddings.map((embedding, index) => ({
      id: `${id}-r${index}`,
      embedding,
      quality: { detectionScore: 0.99, faceSizePx: 200 },
      addedAt: 'x'
    })),
    createdAt: 'x',
    updatedAt: 'x'
  }
}

/**
 * A well-enrolled profile: more than the minimum references, so the
 * `thinProfile` caution is off and the confident tier is reachable.
 */
function richProfile(id: string, person: number, similarity = 0.95, label = id): StoredPersonProfile {
  return profile(id, referencesOf(person, THIN_PROFILE_REFERENCES + 1, similarity), label)
}

function face(embedding: number[], overrides: Partial<FaceObservation> = {}): FaceObservation {
  return { embedding, detectionScore: 0.99, faceSizePx: 200, ...overrides }
}

describe('the fixture itself keeps people apart', () => {
  it('gives two people near-perpendicular faces', () => {
    // If this ever drifts, every assertion below becomes meaningless.
    const target = richProfile('p1', 0)
    expect(scoreAgainstProfile(faceOf(1), target)).toBeLessThan(SAME_IDENTITY_THRESHOLD)
  })

  it('gives one person a strong score against their own references', () => {
    expect(scoreAgainstProfile(faceOf(0), richProfile('p1', 0))).toBeCloseTo(0.95, 6)
  })
})

describe('the score is the best reference, not the average', () => {
  it('takes the strongest reference match', () => {
    const target = profile('p1', [referenceOf(0, 0.2, 0), referenceOf(0, 0.9, 1), referenceOf(0, 0.1, 2)])
    // References vary in pose and lighting on purpose, so a true match agreeing
    // with one strongly and the others weakly is the expected shape. Averaging
    // would wash that out.
    expect(scoreAgainstProfile(faceOf(0), target)).toBeCloseTo(0.9, 6)
  })

  it('returns a low score when nothing matches', () => {
    const target = profile('p1', [referenceOf(0, 0.05, 0), referenceOf(0, 0.02, 1)])
    expect(scoreAgainstProfile(faceOf(1), target)).toBeLessThan(SAME_IDENTITY_THRESHOLD)
  })
})

describe('tiers are conservative by construction', () => {
  it('offers no tier stronger than "likely"', () => {
    // There is deliberately no "certain" or "confirmed" tier, because the
    // measurement cannot support that claim.
    expect(MATCH_TIERS).toEqual(['likely', 'possible', 'none'])
  })

  it('uses OpenCV’s published same-identity threshold as the floor', () => {
    expect(tierFor(SAME_IDENTITY_THRESHOLD - 0.001)).toBe('none')
    expect(tierFor(SAME_IDENTITY_THRESHOLD)).toBe('possible')
  })

  it('requires a stricter score than the published floor to say "likely"', () => {
    expect(LIKELY_MATCH_THRESHOLD).toBeGreaterThan(SAME_IDENTITY_THRESHOLD)
    expect(tierFor(LIKELY_MATCH_THRESHOLD - 0.001)).toBe('possible')
    expect(tierFor(LIKELY_MATCH_THRESHOLD)).toBe('likely')
  })

  it('treats a non-finite score as no match at all', () => {
    // Including infinity. A score that is not a real number is a bug upstream,
    // and the safe reading of a bug is "no match", never "definitely them".
    expect(tierFor(Number.NaN)).toBe('none')
    expect(tierFor(Number.POSITIVE_INFINITY, NO_CAUTION)).toBe('none')
  })

  it('demotes but never promotes', () => {
    const strong = 0.95
    expect(tierFor(strong, NO_CAUTION)).toBe('likely')
    for (const key of ['lowResolution', 'uncertainDetection', 'thinProfile', 'ambiguous'] as const) {
      expect(tierFor(strong, { ...NO_CAUTION, [key]: true })).toBe('possible')
    }
    // A weak score with every caution cleared is still not a match.
    expect(tierFor(0.1, NO_CAUTION)).toBe('none')
  })
})

describe('cautions come from real conditions', () => {
  it('demotes a low-resolution face', () => {
    const matches = matchImage(
      [face(faceOf(0), { faceSizePx: LOW_RESOLUTION_FACE_PX - 1 })],
      [richProfile('p1', 0)]
    )
    expect(matches[0]!.tier).toBe('possible')
  })

  it('demotes an uncertain detection', () => {
    const matches = matchImage(
      [face(faceOf(0), { detectionScore: CONFIDENT_DETECTION_SCORE - 0.01 })],
      [richProfile('p1', 0)]
    )
    expect(matches[0]!.tier).toBe('possible')
  })

  it('demotes a profile enrolled with only the minimum references', () => {
    const thin = profile('p1', referencesOf(0, THIN_PROFILE_REFERENCES, 0.95))
    expect(matchImage([face(faceOf(0))], [thin])[0]!.tier).toBe('possible')
  })

  it('demotes both people when two similar-looking profiles score alike', () => {
    // Two siblings sitting within a hundredth of each other. Picking the higher
    // one and stating it plainly is how a system confidently names the wrong
    // person, so neither gets the confident tier.
    const first = richProfile('p1', 0, 0.9)
    const second = richProfile('p2', 0, 0.9 - AMBIGUITY_MARGIN / 2)
    const matches = matchImage([face(faceOf(0))], [first, second])

    expect(matches).toHaveLength(2)
    for (const match of matches) {
      expect(match.tier).toBe('possible')
    }
  })

  it('leaves a clear winner confident when the runner-up is far behind', () => {
    const matches = matchImage([face(faceOf(0))], [richProfile('p1', 0), richProfile('p2', 1)])

    expect(matches).toHaveLength(1)
    expect(matches[0]!.profileId).toBe('p1')
    expect(matches[0]!.tier).toBe('likely')
  })
})

describe('matching a whole image', () => {
  it('omits a profile that matched nothing rather than recording a negative', () => {
    // Absence and a confident "not in this photo" are different claims, and
    // only the first one is true.
    expect(matchImage([face(faceOf(1))], [richProfile('p1', 0)])).toEqual([])
  })

  it('counts how many faces in the image matched a profile', () => {
    const matches = matchImage(
      [face(faceOf(0)), face(referenceOf(0, 0.99, 3)), face(faceOf(2))],
      [richProfile('p1', 0)]
    )
    expect(matches[0]!.matchingFaces).toBe(2)
  })

  it('lets the strongest face decide the image tier', () => {
    const matches = matchImage(
      [face(referenceOf(0, 0.4, 5)), face(faceOf(0))],
      [richProfile('p1', 0)]
    )
    expect(matches[0]!.tier).toBe('likely')
    expect(matches[0]!.matchingFaces).toBe(2)
  })

  it('finds two different people in one group photo', () => {
    const matches = matchImage(
      [face(faceOf(0)), face(faceOf(1))],
      [richProfile('p1', 0, 0.95, 'Father'), richProfile('p2', 1, 0.95, 'Mother')]
    )
    expect(matches.map((match) => match.profileId).sort()).toEqual(['p1', 'p2'])
  })

  it('returns nothing when there are no faces or no profiles', () => {
    expect(matchImage([], [richProfile('p1', 0)])).toEqual([])
    expect(matchImage([face(faceOf(0))], [])).toEqual([])
  })

  it('never returns one profile’s outcome under another profile’s id', () => {
    const matches = matchImage(
      [face(faceOf(0))],
      [richProfile('p1', 0, 0.95, 'Father'), richProfile('p2', 3, 0.95, 'Stranger')]
    )
    expect(matches).toHaveLength(1)
    expect(matches[0]!.profileId).toBe('p1')
  })
})

describe('reference consistency', () => {
  it('accepts references that agree with each other', () => {
    expect(findInconsistentReference(referencesOf(0, 3, 0.95))).toBeUndefined()
  })

  it('flags a reference that agrees with none of the others', () => {
    // Enrolling a second person by accident produces a profile that matches
    // both and is impossible to debug from the outside.
    const embeddings = [referenceOf(0, 0.95, 0), referenceOf(0, 0.9, 1), faceOf(5)]
    expect(findInconsistentReference(embeddings)).toBe(2)
  })

  it('needs only one agreeing partner, so a varied angle is not rejected', () => {
    // A profile view genuinely scores lower against a frontal one. Requiring
    // agreement with *every* reference would reject exactly the variety that
    // makes a profile robust.
    const embeddings = [referenceOf(0, 0.95, 0), referenceOf(0, 0.93, 0), referenceOf(0, 0.6, 1)]
    expect(findInconsistentReference(embeddings)).toBeUndefined()
  })

  it('has nothing to check with fewer than two references', () => {
    expect(findInconsistentReference([])).toBeUndefined()
    expect(findInconsistentReference([faceOf(0)])).toBeUndefined()
  })
})

describe('what the matcher exposes', () => {
  it('returns a tier rather than a similarity score', () => {
    const matches = matchImage([face(faceOf(0))], [richProfile('p1', 0)])
    const serialized = JSON.stringify(matches)

    expect(Object.keys(matches[0]!).sort()).toEqual(['matchingFaces', 'profileId', 'tier'])
    // No raw similarity anywhere in what a caller receives.
    expect(serialized).not.toContain('score')
    expect(serialized).not.toContain('similarity')
  })

  it('carries no label or embedding out of the matcher', () => {
    const serialized = JSON.stringify(matchImage([face(faceOf(0))], [richProfile('p1', 0, 0.95, 'Father')]))
    expect(serialized).not.toContain('Father')
    expect(serialized).not.toContain('embedding')
  })
})
