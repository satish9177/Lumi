/**
 * Matching one photograph against the people the user has labelled.
 *
 * This is the only place in Lumi where a face belonging to someone who was
 * *not* enrolled becomes a vector. That makes the lifetime of those vectors the
 * central design constraint of this file, so it is stated plainly:
 *
 *   A library-face embedding exists inside `scanPhotoForPeople` and nowhere
 *   else. It is a local `const` in a loop body, it is compared, and the
 *   function returns bounded per-profile outcomes that cannot contain it. It is
 *   never returned, stored in a field, pushed to an array, cached, logged,
 *   journalled, or handed to a callback. There is no parameter on this function
 *   through which a caller could ask to keep one.
 *
 * The same holds for everything upstream of the embedding: the decoded bitmap,
 * the detector geometry, the landmarks, and the aligned 112x112 tensor are all
 * locals. When this function returns, the only thing that survives is a small
 * array of `{ profileId, status, matchingFaces, profileRevision }`.
 *
 * That is what makes the claim "Lumi does not build a face database of everyone
 * in your photos" structural rather than a policy. The database it would need
 * is never assembled in the first place.
 */

import { alignFaceToTensor, type SourceImage } from './face-align'
import { CONFIDENT_FACE_SCORE, UNCERTAIN_FACE_SCORE } from './face-detect'
import type { Point } from './face-landmarks'
import { matchImage, type FaceObservation } from './face-matching'
import type { PeopleMatchRecord } from './index-store'
import { LANDMARK_COUNT } from './face-landmarks'
import { FACE_EMBED_DIMENSIONS } from './people-manifest'
import type { StoredPersonProfile } from './person-profiles'
import { FACE_BOX_STRIDE, MAX_EMBED_FACES } from './protocol'

/**
 * A face below this many pixels on its longer edge, measured in the *detector's*
 * 640x640 space, is not worth embedding. It is deliberately far below the
 * enrolment floor: enrolling on a poor face poisons a profile permanently,
 * whereas matching a poor face merely produces a cautious answer that the tier
 * logic already demotes.
 */
export const MIN_SCAN_FACE_PX = 24

/** Faces the detector is not even uncertainly confident about are not faces. */
export const MIN_SCAN_DETECTION_SCORE = UNCERTAIN_FACE_SCORE

/** Geometry as the engine returns it, in detector input space. */
export interface DetectedGeometry {
  count: number
  boxes: Float32Array
  landmarks: Float32Array
}

export interface PeopleScanInput {
  /** The 640x640 letterboxed BGRA bitmap the detector was given. */
  source: SourceImage
  geometry: DetectedGeometry
  /** Source-to-detector scale, so face size can be reported in real pixels. */
  scale: number
  /** Profiles to check. Every one of them gets an outcome. */
  profiles: readonly StoredPersonProfile[]
  /** Embeds `count` aligned tensors; resolves to `count * 128` unit values. */
  embed: (tensors: Float32Array, count: number) => Promise<Float32Array>
}

export interface PeopleScanOutcome {
  matches: PeopleMatchRecord[]
  /** Faces that passed the gates and were actually embedded. */
  embeddedFaces: number
  /** Faces the detector found but that were too small or too uncertain. */
  rejectedFaces: number
}

export class PeopleScanError extends Error {
  constructor(readonly code: 'alignment_failed' | 'embedding_failed') {
    super('A face could not be read.')
    this.name = 'PeopleScanError'
  }
}

/**
 * Scans one already-decoded, already-detected photo against a set of profiles.
 *
 * Decoding and detection happen in the caller so that the coordinator keeps its
 * single "one expensive image operation at a time" discipline and its
 * revalidation checkpoints in one place. What this function owns is the part
 * that must not leak: alignment, embedding, comparison, and discard.
 */
export async function scanPhotoForPeople(input: PeopleScanInput): Promise<PeopleScanOutcome> {
  const { source, geometry, scale, profiles, embed } = input

  if (profiles.length === 0) {
    return { matches: [], embeddedFaces: 0, rejectedFaces: 0 }
  }

  // Gate first, align second, embed third — so a photo of a crowd costs one
  // batched inference rather than one per face, and so faces that were never
  // going to produce a usable answer are dropped before any of that work.
  const usable: Array<{ tensor: Float32Array; detectionScore: number; faceSizePx: number }> = []
  let rejectedFaces = 0

  const faceCount = Math.min(geometry.count, MAX_EMBED_FACES)
  for (let index = 0; index < faceCount; index += 1) {
    const box = geometry.boxes.subarray(index * FACE_BOX_STRIDE, (index + 1) * FACE_BOX_STRIDE)
    const width = box[2] ?? 0
    const height = box[3] ?? 0
    const score = box[4] ?? 0
    const detectorEdge = Math.max(width, height)

    if (score < MIN_SCAN_DETECTION_SCORE || detectorEdge < MIN_SCAN_FACE_PX) {
      rejectedFaces += 1
      continue
    }

    const landmarks = readLandmarks(geometry.landmarks, index)
    if (!landmarks) {
      rejectedFaces += 1
      continue
    }

    const tensor = alignFaceToTensor(source, landmarks)
    if (!tensor) {
      // A face whose landmarks cannot produce a similarity transform is skipped
      // rather than embedded from a distorted crop. A sheared face resembles
      // someone, just not the person it belongs to.
      rejectedFaces += 1
      continue
    }

    usable.push({
      tensor,
      detectionScore: score,
      // Reported in source pixels so the low-resolution caution means what it
      // says regardless of how far the photo was scaled down to reach 640.
      faceSizePx: scale > 0 ? detectorEdge / scale : detectorEdge
    })
  }

  if (usable.length === 0) {
    // Scanned, nothing readable in it. Every profile gets an explicit negative:
    // "we looked and it wasn't them" is a different claim from "we never
    // looked", and only a written record can carry the first one.
    return { matches: negativesFor(profiles), embeddedFaces: 0, rejectedFaces }
  }

  const batch = new Float32Array(usable.length * (usable[0]!.tensor.length))
  for (let index = 0; index < usable.length; index += 1) {
    batch.set(usable[index]!.tensor, index * usable[0]!.tensor.length)
  }

  const embeddings = await embed(batch, usable.length)
  if (embeddings.length !== usable.length * FACE_EMBED_DIMENSIONS) {
    throw new PeopleScanError('embedding_failed')
  }

  const observations: FaceObservation[] = usable.map((face, index) => ({
    // A subarray view, not a copy that outlives this scope. `observations` is a
    // local, and every reference to it dies with this function.
    embedding: embeddings.subarray(index * FACE_EMBED_DIMENSIONS, (index + 1) * FACE_EMBED_DIMENSIONS),
    detectionScore: face.detectionScore,
    faceSizePx: face.faceSizePx
  }))

  const tiered = matchImage(observations, profiles)
  const byProfile = new Map(tiered.map((match) => [match.profileId, match]))

  // Every profile considered gets a row, matched or not. See people-records.ts:
  // absence of a row must mean "not checked", which is only true if a completed
  // scan writes one for everything it looked at.
  const matches: PeopleMatchRecord[] = profiles.map((profile) => {
    const match = byProfile.get(profile.id)
    return {
      profileId: profile.id,
      status: match ? match.tier : 'checked_no_reliable_match',
      matchingFaces: match ? Math.min(match.matchingFaces, usable.length) : 0,
      profileRevision: profile.revision
    }
  })

  return { matches, embeddedFaces: usable.length, rejectedFaces }
}

/** Explicit negatives for a photo that was scanned and held no usable face. */
function negativesFor(profiles: readonly StoredPersonProfile[]): PeopleMatchRecord[] {
  return profiles.map((profile) => ({
    profileId: profile.id,
    status: 'checked_no_reliable_match' as const,
    matchingFaces: 0,
    profileRevision: profile.revision
  }))
}

/**
 * Reads one face's five landmark points, refusing anything non-finite.
 *
 * A NaN reaching `estimateSimilarity` would produce a transform that samples
 * garbage, and the resulting embedding would be a confident-looking vector for
 * a crop of nothing.
 */
function readLandmarks(landmarks: Float32Array, index: number): Point[] | undefined {
  const stride = LANDMARK_COUNT * 2
  const start = index * stride
  if (start + stride > landmarks.length) {
    return undefined
  }
  const points: Point[] = []
  for (let point = 0; point < LANDMARK_COUNT; point += 1) {
    const x = landmarks[start + point * 2]
    const y = landmarks[start + point * 2 + 1]
    if (x === undefined || y === undefined || !Number.isFinite(x) || !Number.isFinite(y)) {
      return undefined
    }
    points.push({ x, y })
  }
  return points
}

/** Re-exported so the coordinator's gate and this module's cannot drift apart. */
export { CONFIDENT_FACE_SCORE }
