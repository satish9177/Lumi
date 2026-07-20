/**
 * Warping a detected face into the 112x112 frame SFace was trained on.
 *
 * A face embedding is only comparable to another embedding if both faces were
 * presented to the model the same way. Feeding it a raw bounding-box crop makes
 * every comparison noisier — the model then has to absorb head tilt, scale, and
 * framing differences that alignment removes for free. So the five landmarks are
 * fitted to a fixed reference template with a similarity transform, and the
 * result is sampled into the model's input frame.
 *
 * Everything here happens inside the vision worker and nothing here is
 * persisted. The aligned pixels exist for one inference and are then dropped;
 * no face crop is ever written to disk.
 */

import { FACE_EMBED_INPUT_SIZE } from './people-manifest'
import type { Point } from './face-landmarks'

/**
 * The ArcFace reference landmarks, in the 112x112 output frame. These are the
 * same five points OpenCV's own SFace wrapper aligns to, which matters: the
 * pinned weights were exported against that convention, and a different template
 * would present every face slightly off-distribution.
 *
 * Order matches YuNet's output exactly — right eye, left eye, nose tip, right
 * mouth corner, left mouth corner — where "right" is the subject's right, which
 * appears on the left of the image. That is why the first point sits at x=38 in
 * a 112-wide frame rather than at x=73.
 */
export const REFERENCE_LANDMARKS: readonly Point[] = Object.freeze([
  Object.freeze({ x: 38.2946, y: 51.6963 }),
  Object.freeze({ x: 73.5318, y: 51.5014 }),
  Object.freeze({ x: 56.0252, y: 71.7366 }),
  Object.freeze({ x: 41.5493, y: 92.3655 }),
  Object.freeze({ x: 70.7299, y: 92.2041 })
]) as readonly Point[]

/**
 * A 2D similarity transform: rotation and uniform scale in `a`/`b`, translation
 * in `tx`/`ty`. Deliberately *not* a full affine — a similarity cannot shear or
 * stretch a face, so a bad landmark cannot distort the crop into something that
 * happens to resemble somebody else.
 *
 *   u = a*x - b*y + tx
 *   v = b*x + a*y + ty
 */
export interface SimilarityTransform {
  a: number
  b: number
  tx: number
  ty: number
}

/**
 * Least-squares similarity fit from `source` points onto `target` points.
 *
 * Closed form rather than iterative, so it is deterministic: the same landmarks
 * always produce the same crop, and therefore the same embedding. Matching
 * results that changed run to run would be impossible to reason about.
 */
export function estimateSimilarity(
  source: readonly Point[],
  target: readonly Point[] = REFERENCE_LANDMARKS
): SimilarityTransform | undefined {
  const count = Math.min(source.length, target.length)
  if (count < 2) {
    return undefined
  }

  let sourceMeanX = 0
  let sourceMeanY = 0
  let targetMeanX = 0
  let targetMeanY = 0
  for (let index = 0; index < count; index += 1) {
    sourceMeanX += source[index]!.x
    sourceMeanY += source[index]!.y
    targetMeanX += target[index]!.x
    targetMeanY += target[index]!.y
  }
  sourceMeanX /= count
  sourceMeanY /= count
  targetMeanX /= count
  targetMeanY /= count

  let numeratorA = 0
  let numeratorB = 0
  let denominator = 0
  for (let index = 0; index < count; index += 1) {
    const dx = source[index]!.x - sourceMeanX
    const dy = source[index]!.y - sourceMeanY
    const du = target[index]!.x - targetMeanX
    const dv = target[index]!.y - targetMeanY
    numeratorA += dx * du + dy * dv
    numeratorB += dx * dv - dy * du
    denominator += dx * dx + dy * dy
  }

  // Degenerate: every landmark landed on the same spot, so there is no scale or
  // rotation to recover and any transform would be a guess.
  if (!(denominator > 1e-9)) {
    return undefined
  }

  const a = numeratorA / denominator
  const b = numeratorB / denominator
  if (!Number.isFinite(a) || !Number.isFinite(b) || (a === 0 && b === 0)) {
    return undefined
  }

  return {
    a,
    b,
    tx: targetMeanX - (a * sourceMeanX - b * sourceMeanY),
    ty: targetMeanY - (b * sourceMeanX + a * sourceMeanY)
  }
}

/**
 * Inverts the transform, because sampling runs backwards: for each output pixel
 * we ask which source pixel it came from, which avoids the holes a forward map
 * would leave.
 */
export function invertSimilarity(transform: SimilarityTransform): SimilarityTransform | undefined {
  const determinant = transform.a * transform.a + transform.b * transform.b
  if (!(determinant > 1e-12)) {
    return undefined
  }
  const a = transform.a / determinant
  const b = -transform.b / determinant
  return {
    a,
    b,
    tx: -(a * transform.tx - b * transform.ty),
    ty: -(b * transform.tx + a * transform.ty)
  }
}

export function applySimilarity(transform: SimilarityTransform, point: Point): Point {
  return {
    x: transform.a * point.x - transform.b * point.y + transform.tx,
    y: transform.b * point.x + transform.a * point.y + transform.ty
  }
}

export interface SourceImage {
  /** BGRA, four bytes per pixel, as Electron's nativeImage produces. */
  data: Uint8Array
  width: number
  height: number
}

/**
 * Produces the planar BGR tensor SFace expects: `[1, 3, 112, 112]`, channel
 * order B, G, R, values left in 0-255.
 *
 * That range is not an oversight. OpenCV's SFace wrapper feeds the network a
 * blob with scale factor 1 and no mean subtraction, and the pinned export was
 * measured against that convention; dividing by 255 here would shift every
 * embedding away from the distribution the weights expect.
 *
 * Returns undefined when the landmarks cannot produce a usable transform, so a
 * caller can decline to embed rather than embed something meaningless.
 */
export function alignFaceToTensor(
  source: SourceImage,
  landmarks: readonly Point[],
  size: number = FACE_EMBED_INPUT_SIZE
): Float32Array | undefined {
  if (source.width <= 0 || source.height <= 0) {
    return undefined
  }
  if (source.data.length < source.width * source.height * 4) {
    return undefined
  }

  const forward = estimateSimilarity(landmarks, REFERENCE_LANDMARKS)
  if (!forward) {
    return undefined
  }
  const inverse = invertSimilarity(forward)
  if (!inverse) {
    return undefined
  }

  const plane = size * size
  const tensor = new Float32Array(3 * plane)

  for (let outputY = 0; outputY < size; outputY += 1) {
    for (let outputX = 0; outputX < size; outputX += 1) {
      // Sample at pixel centres, so the crop is not shifted by half a pixel.
      const sourcePoint = applySimilarity(inverse, { x: outputX + 0.5, y: outputY + 0.5 })
      const sample = bilinearSample(source, sourcePoint.x - 0.5, sourcePoint.y - 0.5)
      const index = outputY * size + outputX
      tensor[index] = sample.b
      tensor[plane + index] = sample.g
      tensor[2 * plane + index] = sample.r
    }
  }

  return tensor
}

interface Sample {
  b: number
  g: number
  r: number
}

/**
 * Bilinear sample with edge clamping. Clamping rather than zero-filling matters
 * for faces near a photo's border: a black wedge across a cheek is a feature the
 * model would happily encode, and it is an artefact of our sampling, not of the
 * person.
 */
function bilinearSample(source: SourceImage, x: number, y: number): Sample {
  const x0 = Math.floor(x)
  const y0 = Math.floor(y)
  const fx = x - x0
  const fy = y - y0

  const x0c = clampIndex(x0, source.width)
  const x1c = clampIndex(x0 + 1, source.width)
  const y0c = clampIndex(y0, source.height)
  const y1c = clampIndex(y0 + 1, source.height)

  const topLeft = (y0c * source.width + x0c) * 4
  const topRight = (y0c * source.width + x1c) * 4
  const bottomLeft = (y1c * source.width + x0c) * 4
  const bottomRight = (y1c * source.width + x1c) * 4

  const weightTopLeft = (1 - fx) * (1 - fy)
  const weightTopRight = fx * (1 - fy)
  const weightBottomLeft = (1 - fx) * fy
  const weightBottomRight = fx * fy

  const channel = (offset: number): number =>
    source.data[topLeft + offset]! * weightTopLeft +
    source.data[topRight + offset]! * weightTopRight +
    source.data[bottomLeft + offset]! * weightBottomLeft +
    source.data[bottomRight + offset]! * weightBottomRight

  return { b: channel(0), g: channel(1), r: channel(2) }
}

function clampIndex(value: number, limit: number): number {
  if (value < 0) return 0
  if (value > limit - 1) return limit - 1
  return value
}
