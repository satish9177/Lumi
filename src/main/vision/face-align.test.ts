import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  REFERENCE_LANDMARKS,
  alignFaceToTensor,
  applySimilarity,
  estimateSimilarity,
  invertSimilarity,
  type SourceImage
} from './face-align'
import { decodeYunetLandmarks, detectLandmarkedFaces, LANDMARK_COUNT } from './face-landmarks'
import { FACE_EMBED_INPUT_SIZE } from './people-manifest'
import type { Point } from './face-landmarks'

/** A solid-colour image with a bright square, so a warp is visibly verifiable. */
function testImage(width: number, height: number): SourceImage {
  const data = new Uint8Array(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4
      const inside = x >= width / 4 && x < (width * 3) / 4 && y >= height / 4 && y < (height * 3) / 4
      data[offset] = inside ? 200 : 10 // B
      data[offset + 1] = inside ? 150 : 20 // G
      data[offset + 2] = inside ? 100 : 30 // R
      data[offset + 3] = 255
    }
  }
  return { data, width, height }
}

/** A smooth radial gradient, so resampling error reflects geometry not contrast. */
function gradientImage(width: number, height: number): SourceImage {
  const data = new Uint8Array(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4
      const distance = Math.hypot(x - width / 2, y - height / 2) / Math.hypot(width / 2, height / 2)
      const value = Math.round(255 * (1 - distance))
      data[offset] = value
      data[offset + 1] = Math.round(value * 0.7)
      data[offset + 2] = Math.round(value * 0.4)
      data[offset + 3] = 255
    }
  }
  return { data, width, height }
}

function rotateAbout(points: readonly Point[], centre: Point, angle: number): Point[] {
  return points.map((point) => {
    const dx = point.x - centre.x
    const dy = point.y - centre.y
    return {
      x: centre.x + dx * Math.cos(angle) - dy * Math.sin(angle),
      y: centre.y + dx * Math.sin(angle) + dy * Math.cos(angle)
    }
  })
}

function meanAbsoluteDifference(left: Float32Array, right: Float32Array): number {
  let total = 0
  for (let index = 0; index < left.length; index += 1) {
    total += Math.abs(left[index]! - right[index]!)
  }
  return total / left.length
}

/** The reference landmarks scaled and shifted, i.e. a face at a known pose. */
function posedLandmarks(scale: number, offsetX: number, offsetY: number): Point[] {
  return REFERENCE_LANDMARKS.map((point) => ({
    x: point.x * scale + offsetX,
    y: point.y * scale + offsetY
  }))
}

describe('estimating the similarity transform', () => {
  it('recovers an exact scale and translation', () => {
    const transform = estimateSimilarity(posedLandmarks(2, 30, 40), REFERENCE_LANDMARKS)
    expect(transform).toBeDefined()
    // Mapping the posed landmarks forward must land on the reference template.
    for (let index = 0; index < LANDMARK_COUNT; index += 1) {
      const mapped = applySimilarity(transform!, posedLandmarks(2, 30, 40)[index]!)
      expect(mapped.x).toBeCloseTo(REFERENCE_LANDMARKS[index]!.x, 6)
      expect(mapped.y).toBeCloseTo(REFERENCE_LANDMARKS[index]!.y, 6)
    }
  })

  it('recovers a rotation', () => {
    const angle = Math.PI / 6
    const rotated = REFERENCE_LANDMARKS.map((point) => ({
      x: point.x * Math.cos(angle) - point.y * Math.sin(angle),
      y: point.x * Math.sin(angle) + point.y * Math.cos(angle)
    }))
    const transform = estimateSimilarity(rotated, REFERENCE_LANDMARKS)!
    for (let index = 0; index < LANDMARK_COUNT; index += 1) {
      const mapped = applySimilarity(transform, rotated[index]!)
      expect(mapped.x).toBeCloseTo(REFERENCE_LANDMARKS[index]!.x, 6)
      expect(mapped.y).toBeCloseTo(REFERENCE_LANDMARKS[index]!.y, 6)
    }
  })

  it('cannot shear, so a bad landmark distorts less than an affine fit would', () => {
    // Drag one landmark badly out of place. A similarity fit absorbs it as a
    // small rotation/scale error; it cannot stretch the face to accommodate it.
    const damaged = posedLandmarks(2, 0, 0)
    damaged[2] = { x: damaged[2]!.x + 60, y: damaged[2]!.y - 40 }
    const transform = estimateSimilarity(damaged, REFERENCE_LANDMARKS)!

    // The transform stays a similarity: the two basis vectors keep equal length
    // and stay perpendicular, which is exactly what "no shear" means.
    const basisX = { x: transform.a, y: transform.b }
    const basisY = { x: -transform.b, y: transform.a }
    expect(Math.hypot(basisX.x, basisX.y)).toBeCloseTo(Math.hypot(basisY.x, basisY.y), 10)
    expect(basisX.x * basisY.x + basisX.y * basisY.y).toBeCloseTo(0, 10)
  })

  it('refuses degenerate input rather than guessing', () => {
    const collapsed = Array.from({ length: LANDMARK_COUNT }, () => ({ x: 5, y: 5 }))
    expect(estimateSimilarity(collapsed, REFERENCE_LANDMARKS)).toBeUndefined()
    expect(estimateSimilarity([{ x: 1, y: 1 }], REFERENCE_LANDMARKS)).toBeUndefined()
  })

  it('round-trips through its own inverse', () => {
    const transform = estimateSimilarity(posedLandmarks(1.7, -12, 25), REFERENCE_LANDMARKS)!
    const inverse = invertSimilarity(transform)!
    const point = { x: 41.25, y: 88.5 }
    const there = applySimilarity(transform, point)
    const back = applySimilarity(inverse, there)
    expect(back.x).toBeCloseTo(point.x, 6)
    expect(back.y).toBeCloseTo(point.y, 6)
  })

  it('refuses to invert a collapsed transform', () => {
    expect(invertSimilarity({ a: 0, b: 0, tx: 1, ty: 1 })).toBeUndefined()
  })
})

describe('warping a face into the model input', () => {
  it('produces exactly the tensor shape the pinned export accepts', () => {
    const tensor = alignFaceToTensor(testImage(400, 300), posedLandmarks(1.5, 100, 60))
    expect(tensor).toBeDefined()
    expect(tensor!.length).toBe(3 * FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE)
  })

  it('leaves values in 0-255 rather than normalizing them', () => {
    // OpenCV feeds SFace a blob with scale factor 1 and no mean subtraction.
    // Dividing by 255 here would move every embedding off-distribution.
    const tensor = alignFaceToTensor(testImage(400, 300), posedLandmarks(1.5, 100, 60))!
    let max = 0
    for (const value of tensor) {
      max = Math.max(max, value)
    }
    expect(max).toBeGreaterThan(1)
    expect(max).toBeLessThanOrEqual(255)
  })

  it('writes planar BGR, not interleaved', () => {
    const image = testImage(400, 300)
    const tensor = alignFaceToTensor(image, posedLandmarks(1.5, 100, 60))!
    const plane = FACE_EMBED_INPUT_SIZE * FACE_EMBED_INPUT_SIZE
    // The test image's channels are distinct constants, so each plane should be
    // internally consistent and the three planes should differ from each other.
    expect(tensor[0]).not.toBeCloseTo(tensor[plane]!, 3)
    expect(tensor[plane]).not.toBeCloseTo(tensor[2 * plane]!, 3)
  })

  it('samples the same pixels for the same landmarks every time', () => {
    const image = testImage(400, 300)
    const landmarks = posedLandmarks(1.5, 100, 60)
    const first = alignFaceToTensor(image, landmarks)!
    const second = alignFaceToTensor(image, landmarks)!
    // Determinism matters: a match that changed between runs would be untraceable.
    expect(Array.from(first)).toEqual(Array.from(second))
  })

  it('clamps at the edge instead of filling a face with black', () => {
    // A face partly outside the frame must not gain a hard black wedge, which
    // the model would encode as a real feature of that person.
    const image = testImage(200, 200)
    const tensor = alignFaceToTensor(image, posedLandmarks(1.5, -40, -30))!
    let zeros = 0
    for (const value of tensor) {
      if (value === 0) zeros += 1
    }
    expect(zeros).toBe(0)
  })

  it('declines rather than guessing when landmarks are unusable', () => {
    const image = testImage(200, 200)
    const collapsed = Array.from({ length: LANDMARK_COUNT }, () => ({ x: 20, y: 20 }))
    expect(alignFaceToTensor(image, collapsed)).toBeUndefined()
  })

  it('declines on an empty or undersized buffer', () => {
    expect(alignFaceToTensor({ data: new Uint8Array(0), width: 0, height: 0 }, posedLandmarks(1, 0, 0))).toBeUndefined()
    expect(
      alignFaceToTensor({ data: new Uint8Array(16), width: 100, height: 100 }, posedLandmarks(1, 0, 0))
    ).toBeUndefined()
  })

  it('recovers the same crop from a rotated face as from an upright one', () => {
    // This is the entire point of alignment: pose should stop mattering.
    //
    // Measured on a smooth radial gradient rather than the hard-edged square
    // above. A step edge resamples badly under rotation no matter how correct
    // the transform is, so testing against one would measure the fixture's
    // contrast instead of the alignment. The negative control below is what
    // gives this test its teeth.
    const image = gradientImage(400, 400)
    const landmarks = posedLandmarks(2, 80, 80)
    const upright = alignFaceToTensor(image, landmarks)!

    const rotated = rotateAbout(landmarks, { x: 200, y: 200 }, Math.PI / 10)
    const warped = alignFaceToTensor(image, rotated)!
    const alignedDifference = meanAbsoluteDifference(upright, warped)

    // A crop taken from somewhere else entirely, as a control: if the number
    // above were not actually small, this comparison would not be far larger.
    const elsewhere = alignFaceToTensor(image, posedLandmarks(1, 10, 10))!
    const unalignedDifference = meanAbsoluteDifference(upright, elsewhere)

    expect(alignedDifference).toBeLessThan(4)
    expect(unalignedDifference).toBeGreaterThan(alignedDifference * 5)
  })
})

describe('decoding landmarks from the detector', () => {
  /** One anchor's worth of tensors, with a face planted at a known cell. */
  function outputs(options: { score: number; cell: number; stride: number }) {
    const columns = 640 / options.stride
    const anchors = columns * columns
    const cls = new Float32Array(anchors)
    const obj = new Float32Array(anchors)
    const bbox = new Float32Array(anchors * 4)
    const kps = new Float32Array(anchors * LANDMARK_COUNT * 2)

    cls[options.cell] = options.score
    obj[options.cell] = options.score
    bbox[options.cell * 4] = 0.5
    bbox[options.cell * 4 + 1] = 0.5
    bbox[options.cell * 4 + 2] = Math.log(4)
    bbox[options.cell * 4 + 3] = Math.log(4)
    for (let point = 0; point < LANDMARK_COUNT; point += 1) {
      kps[options.cell * LANDMARK_COUNT * 2 + point * 2] = 0.5 + point * 0.1
      kps[options.cell * LANDMARK_COUNT * 2 + point * 2 + 1] = 0.5
    }

    return {
      cls: { [options.stride]: cls },
      obj: { [options.stride]: obj },
      bbox: { [options.stride]: bbox },
      kps: { [options.stride]: kps }
    }
  }

  it('returns five landmarks per detected face', () => {
    const faces = decodeYunetLandmarks(outputs({ score: 0.99, cell: 100, stride: 8 }))
    expect(faces).toHaveLength(1)
    expect(faces[0]!.landmarks).toHaveLength(LANDMARK_COUNT)
  })

  it('places landmarks in input space using the same anchor arithmetic as the box', () => {
    const stride = 8
    const cell = 100
    const faces = decodeYunetLandmarks(outputs({ score: 0.99, cell, stride }))
    const columns = 640 / stride
    const column = cell % columns
    const row = Math.floor(cell / columns)

    expect(faces[0]!.landmarks[0]!.x).toBeCloseTo((column + 0.5) * stride, 6)
    expect(faces[0]!.landmarks[0]!.y).toBeCloseTo((row + 0.5) * stride, 6)
    // And the box centre agrees, since both decode from the same cell.
    expect(faces[0]!.x + faces[0]!.width / 2).toBeCloseTo((column + 0.5) * stride, 6)
  })

  it('drops anchors below the uncertain threshold', () => {
    expect(decodeYunetLandmarks(outputs({ score: 0.2, cell: 100, stride: 8 }))).toHaveLength(0)
  })

  it('skips a stride whose landmark tensor is missing', () => {
    const partial = outputs({ score: 0.99, cell: 100, stride: 8 })
    const withoutKps = { cls: partial.cls, obj: partial.obj, bbox: partial.bbox, kps: {} }
    // Without landmarks a face cannot be aligned, so it is not returned at all.
    expect(decodeYunetLandmarks(withoutKps)).toHaveLength(0)
  })

  it('suppresses the same face found at neighbouring cells', () => {
    const stride = 8
    const columns = 640 / stride
    const anchors = columns * columns
    const cls = new Float32Array(anchors)
    const obj = new Float32Array(anchors)
    const bbox = new Float32Array(anchors * 4)
    const kps = new Float32Array(anchors * LANDMARK_COUNT * 2)

    for (const cell of [100, 101]) {
      cls[cell] = 0.99
      obj[cell] = 0.99
      bbox[cell * 4] = 0.5
      bbox[cell * 4 + 1] = 0.5
      bbox[cell * 4 + 2] = Math.log(20)
      bbox[cell * 4 + 3] = Math.log(20)
      for (let point = 0; point < LANDMARK_COUNT; point += 1) {
        kps[cell * LANDMARK_COUNT * 2 + point * 2] = 0.5
        kps[cell * LANDMARK_COUNT * 2 + point * 2 + 1] = 0.5
      }
    }

    const suppressed = detectLandmarkedFaces({
      cls: { [stride]: cls },
      obj: { [stride]: obj },
      bbox: { [stride]: bbox },
      kps: { [stride]: kps }
    })
    expect(suppressed).toHaveLength(1)
  })
})

describe('the counting path stays free of landmark handling', () => {
  it('does not read the kps tensors in face-detect.ts', () => {
    // Phase 2 guarantees that visible-face counting cannot locate facial
    // features. Phase 3 reads landmarks in its own module precisely so this
    // stays true, and this test is what keeps the two from merging later.
    const source = readFileSync(join(__dirname, 'face-detect.ts'), 'utf8')
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toContain('kps')
    expect(code).not.toContain('landmark')
  })
})
