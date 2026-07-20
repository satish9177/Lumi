/**
 * Turning cosine similarity into something a person can act on.
 *
 * The whole design problem here is that a face-matching score is a continuous,
 * poorly-calibrated number and the user needs a discrete, honest answer. The
 * rules below are deliberately conservative, and the vocabulary tops out at
 * "likely" — there is no tier that means "this is definitely them", because the
 * underlying measurement cannot support that claim and stating it would be a
 * lie the user might act on.
 *
 * ## Where these thresholds come from
 *
 * OpenCV's own SFace wrapper documents a cosine threshold of **0.363** for
 * treating two faces as the same identity. That is the published operating
 * point for these exact weights, so it anchors the bottom of the scale rather
 * than a number chosen by feel.
 *
 * Above it sits a stricter bar for the confident tier. A false "likely match"
 * costs the user more than a missed one: they are searching their own photo
 * library, where a near-miss is a mild annoyance and a confident wrong answer
 * about a family member is not.
 *
 * Raw scores never leave this module. Callers get a tier.
 */

import { cosineSimilarity, type StoredPersonProfile } from './person-profiles'

/** OpenCV's published same-identity threshold for these weights. */
export const SAME_IDENTITY_THRESHOLD = 0.363

/** The stricter bar for the confident tier. */
export const LIKELY_MATCH_THRESHOLD = 0.45

export const MATCH_TIERS = ['likely', 'possible', 'none'] as const
export type MatchTier = (typeof MATCH_TIERS)[number]

/**
 * Conditions that make a score less trustworthy than its value suggests. Each
 * one demotes a `likely` to a `possible`; none of them can ever promote.
 */
export interface MatchCaution {
  /** The face occupied few pixels, so its embedding is noisy. */
  lowResolution: boolean
  /** The detector was not confident this was a face at all. */
  uncertainDetection: boolean
  /** The profile has only the bare minimum of references. */
  thinProfile: boolean
  /** Another enrolled person scored nearly as well on the same face. */
  ambiguous: boolean
}

export const NO_CAUTION: MatchCaution = Object.freeze({
  lowResolution: false,
  uncertainDetection: false,
  thinProfile: false,
  ambiguous: false
})

/** Below this many pixels on its longer edge, a face is treated as low-resolution. */
export const LOW_RESOLUTION_FACE_PX = 60

/** A detection below this is not confident enough to state a match plainly. */
export const CONFIDENT_DETECTION_SCORE = 0.9

/**
 * When the best and second-best profiles are this close, the face is ambiguous.
 * Two siblings can sit within a few hundredths of each other, and picking the
 * higher one without hedging is how a system confidently names the wrong person.
 */
export const AMBIGUITY_MARGIN = 0.05

/** A profile at exactly the minimum reference count gets the cautious treatment. */
export const THIN_PROFILE_REFERENCES = 3

/**
 * Scores one face against one profile.
 *
 * The aggregate is the *maximum* over the profile's references rather than the
 * mean. References are deliberately varied in angle and lighting, so a face
 * matching one strongly and the others weakly is the expected shape of a true
 * match; averaging would wash that out.
 */
export function scoreAgainstProfile(
  faceEmbedding: readonly number[],
  profile: Pick<StoredPersonProfile, 'references'>
): number {
  let best = -1
  for (const reference of profile.references) {
    const similarity = cosineSimilarity(faceEmbedding, reference.embedding)
    if (similarity > best) {
      best = similarity
    }
  }
  return best
}

/**
 * Maps a score plus its cautions onto a tier.
 *
 * Note the asymmetry: cautions only ever demote. There is no combination of
 * circumstances under which a weak score becomes a strong claim.
 */
export function tierFor(score: number, caution: MatchCaution = NO_CAUTION): MatchTier {
  if (!Number.isFinite(score) || score < SAME_IDENTITY_THRESHOLD) {
    return 'none'
  }
  if (score < LIKELY_MATCH_THRESHOLD) {
    return 'possible'
  }
  const demoted =
    caution.lowResolution || caution.uncertainDetection || caution.thinProfile || caution.ambiguous
  return demoted ? 'possible' : 'likely'
}

export interface FaceObservation {
  /** L2-normalized embedding of one detected face. */
  embedding: readonly number[]
  /** The detector's confidence in this face. */
  detectionScore: number
  /** The face's longer edge in source-image pixels. */
  faceSizePx: number
}

export interface ProfileMatch {
  profileId: string
  tier: MatchTier
  /** How many faces in this image reached at least the `possible` tier. */
  matchingFaces: number
}

/**
 * Matches every detected face in one image against every matchable profile.
 *
 * Returns one outcome per profile that reached at least `possible`. Profiles
 * that matched nothing are absent rather than recorded as "no", because absence
 * and a confident negative are different claims and only one of them is true.
 */
export function matchImage(
  faces: readonly FaceObservation[],
  profiles: readonly StoredPersonProfile[]
): ProfileMatch[] {
  const byProfile = new Map<string, { tier: MatchTier; matchingFaces: number }>()

  for (const face of faces) {
    // Score this face against every profile first, so ambiguity between two
    // people is visible before any tier is decided.
    const scored = profiles
      .map((profile) => ({ profile, score: scoreAgainstProfile(face.embedding, profile) }))
      .sort((left, right) => right.score - left.score)

    for (let index = 0; index < scored.length; index += 1) {
      const { profile, score } = scored[index]!
      const runnerUp = index === 0 ? scored[1] : scored[0]
      const caution: MatchCaution = {
        lowResolution: face.faceSizePx < LOW_RESOLUTION_FACE_PX,
        uncertainDetection: face.detectionScore < CONFIDENT_DETECTION_SCORE,
        thinProfile: profile.references.length <= THIN_PROFILE_REFERENCES,
        ambiguous:
          runnerUp !== undefined &&
          runnerUp.score >= SAME_IDENTITY_THRESHOLD &&
          Math.abs(score - runnerUp.score) < AMBIGUITY_MARGIN
      }

      const tier = tierFor(score, caution)
      if (tier === 'none') {
        continue
      }

      const existing = byProfile.get(profile.id)
      if (!existing) {
        byProfile.set(profile.id, { tier, matchingFaces: 1 })
      } else {
        byProfile.set(profile.id, {
          // The strongest face in the image decides the image's tier.
          tier: existing.tier === 'likely' || tier === 'likely' ? 'likely' : 'possible',
          matchingFaces: existing.matchingFaces + 1
        })
      }
    }
  }

  return [...byProfile.entries()].map(([profileId, outcome]) => ({
    profileId,
    tier: outcome.tier,
    matchingFaces: outcome.matchingFaces
  }))
}

/**
 * Whether a set of reference embeddings plausibly describes one person.
 *
 * Enrolling references that are actually two different people produces a profile
 * that matches both of them and is impossible to debug from the outside, so this
 * runs before a profile is created. The test is against the same published
 * same-identity threshold: every reference must agree with at least one other.
 */
export function findInconsistentReference(embeddings: readonly (readonly number[])[]): number | undefined {
  if (embeddings.length < 2) {
    return undefined
  }
  for (let index = 0; index < embeddings.length; index += 1) {
    let agrees = false
    for (let other = 0; other < embeddings.length; other += 1) {
      if (other === index) {
        continue
      }
      if (cosineSimilarity(embeddings[index]!, embeddings[other]!) >= SAME_IDENTITY_THRESHOLD) {
        agrees = true
        break
      }
    }
    if (!agrees) {
      return index
    }
  }
  return undefined
}
