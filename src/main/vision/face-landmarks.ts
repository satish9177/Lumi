/**
 * Decoding YuNet's output *including* its five facial landmarks.
 *
 * This is a separate module from `face-detect.ts` on purpose, and the
 * duplication of the anchor arithmetic is the price of that separation.
 *
 * `face-detect.ts` implements Phase 2's visible-face counting, and one of its
 * stated guarantees is that it never reads the `kps_*` tensors — a guarantee a
 * test enforces by inspecting its source. Counting people and locating their
 * eyes are different capabilities with different risks, and the counting path
 * should stay provably incapable of the second. Adding landmark support there
 * would have quietly widened a module whose narrowness was the point.
 *
 * So Phase 3 reads landmarks here instead. Landmarks exist for exactly one
 * purpose: aligning a face into the 112x112 frame SFace expects. They are used
 * within the worker and discarded. They are never persisted, never returned to
 * main, never sent to the renderer, and never sent to Realtime.
 */

import {
  FACE_INPUT_SIZE,
  FACE_STRIDES,
  MAX_FACE_DETECTIONS,
  UNCERTAIN_FACE_SCORE,
  intersectionOverUnion,
  type FaceDetection
} from './face-detect'

/** The five points YuNet emits, in its own order. */
export const LANDMARK_COUNT = 5

export interface Point {
  x: number
  y: number
}

export interface LandmarkedFace extends FaceDetection {
  /** Right eye, left eye, nose, right mouth corner, left mouth corner. */
  landmarks: Point[]
}

/** The same four tensors `face-detect` reads, plus the one it deliberately does not. */
export interface YunetLandmarkOutputs {
  cls: Record<number, Float32Array>
  obj: Record<number, Float32Array>
  bbox: Record<number, Float32Array>
  kps: Record<number, Float32Array>
}

/**
 * Decodes anchors into boxes and landmarks in 640x640 input space.
 *
 * The box arithmetic matches `decodeYunet` exactly — same score formula, same
 * centre and exponential size decode — because both are reading the same
 * tensors from the same pinned export. Landmarks use the simpler offset form:
 * each point is an offset from its anchor cell, scaled by the stride, with no
 * exponential.
 */
export function decodeYunetLandmarks(
  outputs: YunetLandmarkOutputs,
  inputSize: number = FACE_INPUT_SIZE
): LandmarkedFace[] {
  const faces: LandmarkedFace[] = []

  for (const stride of FACE_STRIDES) {
    const cls = outputs.cls[stride]
    const obj = outputs.obj[stride]
    const bbox = outputs.bbox[stride]
    const kps = outputs.kps[stride]
    if (!cls || !obj || !bbox || !kps) {
      continue
    }

    const columns = Math.floor(inputSize / stride)
    const anchors = Math.min(
      cls.length,
      obj.length,
      Math.floor(bbox.length / 4),
      Math.floor(kps.length / (LANDMARK_COUNT * 2))
    )

    for (let index = 0; index < anchors; index += 1) {
      const classification = cls[index]!
      const objectness = obj[index]!
      if (!Number.isFinite(classification) || !Number.isFinite(objectness)) {
        continue
      }

      const score = Math.sqrt(clamp01(classification) * clamp01(objectness))
      if (score < UNCERTAIN_FACE_SCORE) {
        continue
      }

      const column = index % columns
      const row = Math.floor(index / columns)
      const boxOffset = index * 4

      const centreX = (column + bbox[boxOffset]!) * stride
      const centreY = (row + bbox[boxOffset + 1]!) * stride
      const width = Math.exp(bbox[boxOffset + 2]!) * stride
      const height = Math.exp(bbox[boxOffset + 3]!) * stride

      if (!Number.isFinite(centreX) || !Number.isFinite(centreY) || !Number.isFinite(width) || !Number.isFinite(height)) {
        continue
      }
      if (width <= 0 || height <= 0 || width > inputSize * 2 || height > inputSize * 2) {
        continue
      }

      const landmarkOffset = index * LANDMARK_COUNT * 2
      const landmarks: Point[] = []
      let usable = true
      for (let point = 0; point < LANDMARK_COUNT; point += 1) {
        const x = (column + kps[landmarkOffset + point * 2]!) * stride
        const y = (row + kps[landmarkOffset + point * 2 + 1]!) * stride
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
          usable = false
          break
        }
        landmarks.push({ x, y })
      }
      // A face without usable landmarks cannot be aligned, and an unaligned
      // crop produces an embedding that compares badly against every profile.
      // Dropping it is better than embedding it wrongly.
      if (!usable) {
        continue
      }

      faces.push({
        x: centreX - width / 2,
        y: centreY - height / 2,
        width,
        height,
        score,
        landmarks
      })
    }
  }

  return faces
}

/**
 * The same greedy suppression `face-detect` applies, carrying landmarks along
 * with the winning box rather than discarding them.
 */
export function suppressLandmarkedFaces(
  faces: readonly LandmarkedFace[],
  iouThreshold = 0.3,
  limit: number = MAX_FACE_DETECTIONS
): LandmarkedFace[] {
  const ordered = [...faces].sort((left, right) => right.score - left.score)
  const kept: LandmarkedFace[] = []

  for (const candidate of ordered) {
    if (kept.length >= limit) {
      break
    }
    if (!kept.some((existing) => intersectionOverUnion(existing, candidate) > iouThreshold)) {
      kept.push(candidate)
    }
  }

  return kept
}

export function detectLandmarkedFaces(
  outputs: YunetLandmarkOutputs,
  inputSize: number = FACE_INPUT_SIZE
): LandmarkedFace[] {
  return suppressLandmarkedFaces(decodeYunetLandmarks(outputs, inputSize))
}

function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value
}
