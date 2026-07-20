import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  CONFIDENT_FACE_SCORE,
  countFaces,
  decodeYunet,
  detectionScores,
  FACE_INPUT_SIZE,
  FACE_STRIDES,
  intersectionOverUnion,
  MAX_FACE_DETECTIONS,
  suppressOverlapping,
  UNCERTAIN_FACE_SCORE,
  type FaceDetection,
  type YunetOutputs
} from './face-detect'

/**
 * Builds YuNet-shaped tensors with faces planted at chosen anchors, so decoding
 * is checked against known ground truth rather than against itself.
 */
function outputsWith(
  planted: { stride: number; index: number; score: number; size?: number }[]
): YunetOutputs {
  const outputs: YunetOutputs = { cls: {}, obj: {}, bbox: {} }
  for (const stride of FACE_STRIDES) {
    const anchors = (FACE_INPUT_SIZE / stride) ** 2
    outputs.cls[stride] = new Float32Array(anchors)
    outputs.obj[stride] = new Float32Array(anchors)
    outputs.bbox[stride] = new Float32Array(anchors * 4)
  }
  for (const face of planted) {
    // score = sqrt(cls * obj), so setting both to the score yields the score.
    outputs.cls[face.stride]![face.index] = face.score
    outputs.obj[face.stride]![face.index] = face.score
    const offset = face.index * 4
    outputs.bbox[face.stride]![offset] = 0.5
    outputs.bbox[face.stride]![offset + 1] = 0.5
    outputs.bbox[face.stride]![offset + 2] = Math.log(face.size ?? 4)
    outputs.bbox[face.stride]![offset + 3] = Math.log(face.size ?? 4)
  }
  return outputs
}

/** Removes block and line comments so source assertions test code, not prose. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
}

const box = (overrides: Partial<FaceDetection> = {}): FaceDetection => ({
  x: 0,
  y: 0,
  width: 100,
  height: 100,
  score: 0.95,
  ...overrides
})

describe('decoding YuNet output', () => {
  it('finds a planted face and places it in input space', () => {
    // Anchor 0 at stride 8, with centre offset 0.5 and size log(4).
    const detections = decodeYunet(outputsWith([{ stride: 8, index: 0, score: 0.99 }]))
    expect(detections).toHaveLength(1)

    const [face] = detections
    expect(face!.score).toBeCloseTo(0.99, 2)
    // centre = (0 + 0.5) * 8 = 4; size = exp(log(4)) * 8 = 32.
    expect(face!.x).toBeCloseTo(4 - 16, 5)
    expect(face!.y).toBeCloseTo(4 - 16, 5)
    expect(face!.width).toBeCloseTo(32, 5)
  })

  it('maps an anchor index to the right row and column', () => {
    const columns = FACE_INPUT_SIZE / 16
    // One full row down, three columns across.
    const index = columns + 3
    const [face] = decodeYunet(outputsWith([{ stride: 16, index, score: 0.95 }]))
    expect(face!.x + face!.width / 2).toBeCloseTo((3 + 0.5) * 16, 5)
    expect(face!.y + face!.height / 2).toBeCloseTo((1 + 0.5) * 16, 5)
  })

  it('decodes every stride', () => {
    const detections = decodeYunet(
      outputsWith(FACE_STRIDES.map((stride) => ({ stride, index: 0, score: 0.95 })))
    )
    // Distinct strides give distinct sizes, so none are suppressed as duplicates.
    expect(detections.length).toBe(FACE_STRIDES.length)
  })

  it('discards anchors below the uncertain floor without decoding them', () => {
    expect(decodeYunet(outputsWith([{ stride: 8, index: 0, score: 0.4 }]))).toHaveLength(0)
  })

  it('survives missing tensors rather than throwing', () => {
    expect(decodeYunet({ cls: {}, obj: {}, bbox: {} })).toEqual([])
  })

  it('rejects non-finite and absurd geometry', () => {
    const outputs = outputsWith([{ stride: 8, index: 0, score: 0.99 }])
    outputs.bbox[8]![2] = Number.NaN
    expect(decodeYunet(outputs)).toHaveLength(0)

    const huge = outputsWith([{ stride: 8, index: 1, score: 0.99, size: 1e6 }])
    expect(decodeYunet(huge)).toHaveLength(0)
  })

  it('ignores a score outside a probability range instead of trusting it', () => {
    const outputs = outputsWith([{ stride: 8, index: 0, score: 0.99 }])
    outputs.cls[8]![0] = 50
    outputs.obj[8]![0] = 50
    // Clamped to 1, so the detection survives but cannot exceed a probability.
    const [face] = decodeYunet(outputs)
    expect(face!.score).toBeLessThanOrEqual(1)
  })
})

describe('suppressing the same face found twice', () => {
  it('collapses heavily overlapping boxes to the highest scoring one', () => {
    const kept = suppressOverlapping([
      box({ score: 0.9 }),
      box({ x: 5, y: 5, score: 0.95 }),
      box({ x: 8, y: 8, score: 0.8 })
    ])
    expect(kept).toHaveLength(1)
    expect(kept[0]!.score).toBe(0.95)
  })

  it('keeps genuinely separate faces', () => {
    const kept = suppressOverlapping([box(), box({ x: 300 }), box({ x: 600 })])
    expect(kept).toHaveLength(3)
  })

  it('turns a face detected at three strides into one person, not a group', () => {
    // This is the failure that would silently reclassify a portrait as a crowd.
    const detections = decodeYunet(
      outputsWith(FACE_STRIDES.map((stride) => ({ stride, index: 0, score: 0.95 })))
    )
    // Force them onto the same region, as a real multi-stride hit would be.
    const sameRegion = detections.map((detection) => ({ ...detection, x: 100, y: 100, width: 80, height: 80 }))
    expect(suppressOverlapping(sameRegion)).toHaveLength(1)
  })

  it('bounds how many detections can survive', () => {
    const many = Array.from({ length: 500 }, (_, index) => box({ x: index * 200, score: 0.99 }))
    expect(suppressOverlapping(many).length).toBeLessThanOrEqual(MAX_FACE_DETECTIONS)
  })

  it('is deterministic for the same input', () => {
    const input = [box({ score: 0.9 }), box({ x: 400, score: 0.8 }), box({ x: 5, score: 0.95 })]
    expect(suppressOverlapping(input)).toEqual(suppressOverlapping(input))
  })
})

describe('intersection over union', () => {
  it('is 1 for identical boxes and 0 for disjoint ones', () => {
    expect(intersectionOverUnion(box(), box())).toBe(1)
    expect(intersectionOverUnion(box(), box({ x: 1000 }))).toBe(0)
  })

  it('is symmetric', () => {
    const a = box()
    const b = box({ x: 50, y: 50 })
    expect(intersectionOverUnion(a, b)).toBeCloseTo(intersectionOverUnion(b, a), 10)
  })

  it('handles a touching edge as no overlap', () => {
    expect(intersectionOverUnion(box(), box({ x: 100 }))).toBe(0)
  })
})

describe('counting is calibrated and honest', () => {
  it('separates confident from uncertain detections', () => {
    expect(countFaces([0.99, 0.95, 0.7, 0.65, 0.3])).toEqual({ visible: 2, uncertain: 2 })
  })

  it('counts nothing when every detection is below the floor', () => {
    expect(countFaces([0.5, 0.2, 0.01])).toEqual({ visible: 0, uncertain: 0 })
  })

  it('counts an empty result as zero of both', () => {
    expect(countFaces([])).toEqual({ visible: 0, uncertain: 0 })
  })

  it('places the thresholds where the copy assumes they are', () => {
    expect(countFaces([CONFIDENT_FACE_SCORE])).toEqual({ visible: 1, uncertain: 0 })
    expect(countFaces([UNCERTAIN_FACE_SCORE])).toEqual({ visible: 0, uncertain: 1 })
    expect(countFaces([UNCERTAIN_FACE_SCORE - 0.001])).toEqual({ visible: 0, uncertain: 0 })
  })

  it('ignores non-finite scores rather than counting them', () => {
    expect(countFaces([Number.NaN, Number.POSITIVE_INFINITY, 0.99])).toEqual({ visible: 1, uncertain: 0 })
  })

  it('never merges the two counts into a single total', () => {
    // An uncertain detection must stay separable, so the caller cannot claim a
    // person is present on evidence the detector was not sure about.
    const counts = countFaces([0.99, 0.7])
    expect(counts.visible).toBe(1)
    expect(counts.uncertain).toBe(1)
    expect(Object.keys(counts).sort()).toEqual(['uncertain', 'visible'])
  })
})

describe('the worker-side pipeline', () => {
  it('reduces a full decode to scores alone', () => {
    const scores = detectionScores(
      outputsWith([
        { stride: 8, index: 0, score: 0.99 },
        { stride: 16, index: 500, score: 0.95 }
      ])
    )
    expect(scores).toBeInstanceOf(Float32Array)
    expect(scores.length).toBe(2)
    expect(countFaces(scores)).toEqual({ visible: 2, uncertain: 0 })
  })

  it('returns an empty score list for a photo with no faces', () => {
    expect(detectionScores(outputsWith([])).length).toBe(0)
  })
})

describe('this is detection, not recognition', () => {
  it('never reads the landmark tensors, in either the decoder or the worker', async () => {
    // Comments are stripped first: both files *document* that landmarks are not
    // used, and that prose is worth keeping. The assertion is about code.
    for (const file of ['face-detect.ts', '../vision-worker.ts']) {
      const code = stripComments(await readFile(join(__dirname, file), 'utf8'))
      expect(code).not.toMatch(/\bkps\b|kps_|['"`]kps/)
    }
  })

  it('builds nothing resembling a face descriptor', async () => {
    // Scoped to the face decoder: the worker legitimately handles CLIP
    // embeddings for the unrelated Phase-1 semantic path.
    const code = stripComments(await readFile(join(__dirname, 'face-detect.ts'), 'utf8'))
    expect(code).not.toMatch(/embedding|descriptor|faceprint|identity|recogni[sz]/i)
  })

  it('collects only the three tensor families needed to count', async () => {
    const code = stripComments(await readFile(join(__dirname, '../vision-worker.ts'), 'utf8'))
    const families = /\['cls', 'obj', 'bbox'\] as const/
    expect(code).toMatch(families)
  })

  it('produces nothing that could match one photo against another', () => {
    const scores = detectionScores(outputsWith([{ stride: 8, index: 0, score: 0.99 }]))
    // A count and a confidence. There is no vector here to compare.
    expect(scores.length).toBe(1)
    expect(typeof scores[0]).toBe('number')
  })

  it('gives the same answer for two different people photographed alike', () => {
    // Identical geometry and confidence yield identical output: the pipeline
    // cannot distinguish who is in the frame, only that someone is.
    const first = detectionScores(outputsWith([{ stride: 8, index: 42, score: 0.97 }]))
    const second = detectionScores(outputsWith([{ stride: 8, index: 42, score: 0.97 }]))
    expect([...first]).toEqual([...second])
  })
})
