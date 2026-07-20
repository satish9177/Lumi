/**
 * Decoding YuNet's raw output into a count of visible faces.
 *
 * **This is face detection, not face recognition.** The model answers "is there
 * a face-shaped region here, and how sure am I" and nothing else. It produces
 * no descriptor, no embedding, and nothing comparable between two photographs.
 * Two pictures of the same person yield two independent detections that this
 * code cannot tell apart from two pictures of different people — and that
 * property is deliberate, not a limitation to be engineered away later.
 *
 * YuNet also emits five facial landmarks per detection. This module never reads
 * them: they are the one output that could seed identity work, and the decode
 * loop below simply does not look at the `kps_*` tensors.
 *
 * Geometry stays on the worker side of the process boundary. The worker runs
 * decode and NMS, then hands the parent nothing but a bounded list of
 * confidence scores. Main counts those against calibrated thresholds. So no box
 * — nothing locating a face inside a photograph — is ever transferred, stored,
 * or made reachable from the renderer.
 */

/** The exact input the pinned YuNet export accepts. Its dimensions are fixed. */
export const FACE_INPUT_SIZE = 640
export const FACE_STRIDES: readonly number[] = [8, 16, 32]

/**
 * Calibrated on YuNet's own scale, where OpenCV's reference sample uses 0.9 as
 * its single accept threshold. Two thresholds rather than one, because "I am
 * not sure" is a real answer that the user-facing copy is allowed to give:
 *
 *   >= 0.90  confident   — counted, and may be stated plainly
 *   >= 0.60  uncertain   — counted separately, and only ever hedged
 *   <  0.60  rejected    — not counted at all
 */
export const CONFIDENT_FACE_SCORE = 0.9
export const UNCERTAIN_FACE_SCORE = 0.6

/** Overlap above which two boxes are the same face found twice. */
export const FACE_NMS_IOU = 0.3

/**
 * A hard ceiling on detections carried across the boundary. A crowd scene or a
 * pathological result must not be able to grow the message without bound.
 */
export const MAX_FACE_DETECTIONS = 64

export interface FaceDetection {
  x: number
  y: number
  width: number
  height: number
  score: number
}

export interface FaceCounts {
  visible: number
  uncertain: number
}

/** The four tensors per stride that this module reads. `kps_*` is not among them. */
export interface YunetOutputs {
  cls: Record<number, Float32Array>
  obj: Record<number, Float32Array>
  bbox: Record<number, Float32Array>
}

/**
 * Turns YuNet's per-anchor tensors into boxes in 640x640 input space.
 *
 * The model predicts, for every cell of three feature maps, an objectness, a
 * classification, and a box expressed as an offset from that cell. The combined
 * score is the geometric mean of the two probabilities, which is how OpenCV's
 * own YuNet wrapper scores a detection, so the thresholds above sit on the
 * scale the model was tuned against.
 */
export function decodeYunet(outputs: YunetOutputs, inputSize: number = FACE_INPUT_SIZE): FaceDetection[] {
  const detections: FaceDetection[] = []

  for (const stride of FACE_STRIDES) {
    const cls = outputs.cls[stride]
    const obj = outputs.obj[stride]
    const bbox = outputs.bbox[stride]
    if (!cls || !obj || !bbox) {
      continue
    }

    const columns = Math.floor(inputSize / stride)
    const anchors = Math.min(cls.length, obj.length, Math.floor(bbox.length / 4))

    for (let index = 0; index < anchors; index += 1) {
      const classification = cls[index]!
      const objectness = obj[index]!
      if (!Number.isFinite(classification) || !Number.isFinite(objectness)) {
        continue
      }

      const score = Math.sqrt(clamp01(classification) * clamp01(objectness))
      // Filtering here rather than after decode keeps the exp() and the sort
      // off the ~8,400 anchors that are overwhelmingly background.
      if (score < UNCERTAIN_FACE_SCORE) {
        continue
      }

      const column = index % columns
      const row = Math.floor(index / columns)
      const offset = index * 4

      const centreX = (column + bbox[offset]!) * stride
      const centreY = (row + bbox[offset + 1]!) * stride
      const width = Math.exp(bbox[offset + 2]!) * stride
      const height = Math.exp(bbox[offset + 3]!) * stride

      if (!Number.isFinite(centreX) || !Number.isFinite(centreY) || !Number.isFinite(width) || !Number.isFinite(height)) {
        continue
      }
      if (width <= 0 || height <= 0 || width > inputSize * 2 || height > inputSize * 2) {
        continue
      }

      detections.push({
        x: centreX - width / 2,
        y: centreY - height / 2,
        width,
        height,
        score
      })
    }
  }

  return detections
}

/**
 * Greedy non-maximum suppression. The same face is found at several strides and
 * several neighbouring cells; without this a single face counts as many, which
 * would turn "one person" into "group photo".
 */
export function suppressOverlapping(
  detections: readonly FaceDetection[],
  iouThreshold: number = FACE_NMS_IOU,
  limit: number = MAX_FACE_DETECTIONS
): FaceDetection[] {
  const ordered = [...detections].sort((left, right) => right.score - left.score)
  const kept: FaceDetection[] = []

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

export function intersectionOverUnion(left: FaceDetection, right: FaceDetection): number {
  const overlapWidth = Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x)
  const overlapHeight = Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y)
  if (overlapWidth <= 0 || overlapHeight <= 0) {
    return 0
  }
  const overlap = overlapWidth * overlapHeight
  const union = left.width * left.height + right.width * right.height - overlap
  return union > 0 ? overlap / union : 0
}

/**
 * Splits surviving scores into the two counts the index stores.
 *
 * Note what this does *not* return: a total. A caller that added the two
 * together would be asserting that an uncertain detection is a person, which is
 * exactly the claim the two-threshold split exists to avoid making.
 */
export function countFaces(scores: Iterable<number>): FaceCounts {
  let visible = 0
  let uncertain = 0
  for (const score of scores) {
    if (!Number.isFinite(score)) {
      continue
    }
    if (score >= CONFIDENT_FACE_SCORE) {
      visible += 1
    } else if (score >= UNCERTAIN_FACE_SCORE) {
      uncertain += 1
    }
  }
  return { visible, uncertain }
}

/** The full worker-side pipeline: decode, suppress, and reduce to scores. */
export function detectionScores(outputs: YunetOutputs, inputSize: number = FACE_INPUT_SIZE): Float32Array {
  const kept = suppressOverlapping(decodeYunet(outputs, inputSize))
  return Float32Array.from(kept.map((detection) => detection.score))
}

function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value
}
