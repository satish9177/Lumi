/**
 * The local vision worker: an Electron utilityProcess entry point.
 *
 * It holds at most two ONNX Runtime sessions (the CLIP image tower and the CLIP
 * text tower), runs CPU-only inference, and talks to the parent solely over the
 * validated protocol. It persists nothing, opens no network connection, and
 * knows no path except the ones main hands it.
 *
 * Failures never escape as native text: every catch maps to a bounded code.
 *
 * The message-handling core is a factory whose runtime, output sink, and exit
 * are injected, so every branch can be exercised in-process with a stubbed ONNX
 * Runtime and no real utilityProcess. The module tail wires that core to the
 * real parentPort and the real dynamic import.
 */

import { existsSync } from 'node:fs'
import { detectionScores, FACE_INPUT_SIZE, FACE_STRIDES, type YunetOutputs } from './vision/face-detect'
import { detectLandmarkedFaces, type YunetLandmarkOutputs } from './vision/face-landmarks'
import { bgraToClipTensor, CLIP_TENSOR_DIMS, normalizedEmbedding } from './vision/preprocess'
import {
  CLIP_CONTEXT_LENGTH,
  FACE_BITMAP_HEIGHT,
  FACE_BITMAP_WIDTH,
  FACE_BOX_STRIDE,
  FACE_EMBED_LENGTH,
  FACE_EMBED_SIZE,
  FACE_EMBED_TENSOR_FLOATS,
  FACE_LANDMARK_STRIDE,
  MAX_DETAILED_FACES,
  parseVisionCommand,
  VisionProtocolError,
  type VisionDetectFacesCommand,
  type VisionDetectFacesDetailedCommand,
  type VisionEmbedFacesCommand,
  type VisionEmbedImageCommand,
  type VisionEmbedTextCommand,
  type VisionErrorCode,
  type VisionEvent,
  type VisionLoadModelCommand,
  type VisionModelKind
} from './vision/protocol'

/** Preferred CLIP outputs, most specific first. */
const PREFERRED_OUTPUTS: Record<VisionModelKind, readonly string[]> = {
  image: ['image_embeds', 'pooler_output'],
  text: ['text_embeds', 'pooler_output'],
  // The face detector's outputs are read by name per stride, not through this
  // table; the entry exists so the record stays total over the model kinds.
  face: [],
  // SFace's single output, measured against the pinned export.
  faceEmbed: ['fc1']
}
const PREFERRED_IMAGE_INPUT = 'pixel_values'
/** SFace's single input, measured against the pinned export. */
const FACE_EMBED_INPUT = 'data'
const TEXT_INPUT_IDS = 'input_ids'
const TEXT_ATTENTION_MASK = 'attention_mask'

type OrtTensorData = { data: unknown }

export type InferenceSession = {
  inputNames: readonly string[]
  outputNames: readonly string[]
  run: (feeds: Record<string, unknown>) => Promise<Record<string, OrtTensorData>>
  release?: () => Promise<void>
}

type TensorConstructor = new (type: string, data: Float32Array | BigInt64Array, dims: readonly number[]) => unknown

export type OrtModule = {
  InferenceSession: { create: (path: string, options?: unknown) => Promise<InferenceSession> }
  Tensor: TensorConstructor
  env?: { versions?: { common?: string } }
}

interface LoadedModel {
  session: InferenceSession
  inputName: string
}

export interface VisionWorkerDeps {
  /** Loads (and validates) ONNX Runtime, or resolves undefined when it cannot. */
  loadRuntime: () => Promise<OrtModule | undefined>
  /** Where a validated event goes. In production, the parent port. */
  emit: (event: VisionEvent) => void
  /** Ends the process on shutdown. Injected so a test need not exit its runner. */
  exit?: (code: number) => void
  now?: () => number
  rssBytes?: () => number
  fileExists?: (path: string) => boolean
}

export interface VisionWorker {
  handleMessage: (raw: unknown) => Promise<void>
}

export function createVisionWorker(deps: VisionWorkerDeps): VisionWorker {
  const models = new Map<VisionModelKind, LoadedModel>()
  let ort: OrtModule | undefined

  const now = deps.now ?? (() => Date.now())
  const rssBytes = deps.rssBytes ?? (() => process.memoryUsage().rss)
  const fileExists = deps.fileExists ?? existsSync
  const exit = deps.exit ?? ((code: number) => process.exit(code))

  function emit(event: VisionEvent): void {
    deps.emit(event)
  }

  function emitError(code: VisionErrorCode, requestId?: string): void {
    emit({ type: 'bounded_error', code, ...(requestId === undefined ? {} : { requestId }) })
  }

  async function ensureRuntime(): Promise<OrtModule | undefined> {
    if (ort) {
      return ort
    }
    const loaded = await deps.loadRuntime()
    if (!loaded) {
      return undefined
    }
    ort = loaded
    return ort
  }

  async function handleMessage(raw: unknown): Promise<void> {
    let command
    try {
      command = parseVisionCommand(raw)
    } catch (error) {
      emitError(codeFrom(error, 'invalid_message'))
      return
    }

    switch (command.type) {
      case 'load_model':
        await handleLoadModel(command)
        return
      case 'unload_model':
        await handleUnloadModel(command.kind)
        return
      case 'embed_image':
        await handleEmbedImage(command)
        return
      case 'embed_text':
        await handleEmbedText(command)
        return
      case 'detect_faces':
        await handleDetectFaces(command)
        return
      case 'detect_faces_detailed':
        await handleDetectFacesDetailed(command)
        return
      case 'embed_faces':
        await handleEmbedFaces(command)
        return
      case 'shutdown':
        models.clear()
        exit(0)
    }
  }

  async function handleLoadModel(command: VisionLoadModelCommand): Promise<void> {
    if (models.has(command.kind)) {
      emit({ type: 'model_loaded', kind: command.kind, sessionLoadMs: 0 })
      return
    }

    if (!fileExists(command.modelPath)) {
      emitError('model_missing')
      return
    }

    const runtime = await ensureRuntime()
    if (!runtime) {
      emitError('runtime_load_failed')
      return
    }

    const startedAt = now()
    let session: InferenceSession
    try {
      // CPU only. DirectML is deliberately not requested.
      session = await runtime.InferenceSession.create(command.modelPath, {
        executionProviders: ['cpu'],
        graphOptimizationLevel: 'all'
      })
    } catch {
      emitError('model_load_failed')
      return
    }

    models.set(command.kind, { session, inputName: selectInputName(command.kind, session) })
    emit({ type: 'model_loaded', kind: command.kind, sessionLoadMs: now() - startedAt })
  }

  async function handleUnloadModel(kind: VisionModelKind): Promise<void> {
    const loaded = models.get(kind)
    models.delete(kind)
    // Releasing frees the session's arenas; without it the image tower's memory
    // would stay resident for the life of the worker.
    await loaded?.session.release?.().catch(() => undefined)
    emit({ type: 'model_unloaded', kind })
  }

  async function handleEmbedImage(command: VisionEmbedImageCommand): Promise<void> {
    const runtime = ort
    const loaded = models.get('image')
    if (!loaded || !runtime) {
      emitError('not_initialised', command.requestId)
      return
    }

    let tensor: unknown
    try {
      tensor = new runtime.Tensor('float32', bgraToClipTensor(new Uint8Array(command.bitmap)), CLIP_TENSOR_DIMS)
    } catch (error) {
      emitError(codeFrom(error, 'invalid_bitmap'), command.requestId)
      return
    }

    await runAndEmit('image', loaded, { [loaded.inputName]: tensor }, command.requestId)
  }

  async function handleEmbedText(command: VisionEmbedTextCommand): Promise<void> {
    const runtime = ort
    const loaded = models.get('text')
    if (!loaded || !runtime) {
      emitError('not_initialised', command.requestId)
      return
    }

    let feeds: Record<string, unknown>
    try {
      const tokens = new Int32Array(command.tokenIds)
      // CLIP's text tower takes int64 ids.
      const ids = new BigInt64Array(CLIP_CONTEXT_LENGTH)
      const mask = new BigInt64Array(CLIP_CONTEXT_LENGTH)
      for (let index = 0; index < CLIP_CONTEXT_LENGTH; index += 1) {
        ids[index] = BigInt(tokens[index] ?? 0)
        mask[index] = index < command.tokenCount ? 1n : 0n
      }

      const dims = [1, CLIP_CONTEXT_LENGTH] as const
      feeds = { [loaded.inputName]: new runtime.Tensor('int64', ids, dims) }
      if (loaded.session.inputNames.includes(TEXT_ATTENTION_MASK)) {
        feeds[TEXT_ATTENTION_MASK] = new runtime.Tensor('int64', mask, dims)
      }
    } catch (error) {
      emitError(codeFrom(error, 'invalid_tokens'), command.requestId)
      return
    }

    await runAndEmit('text', loaded, feeds, command.requestId)
  }

  /**
   * Counts visible faces.
   *
   * The decoded boxes exist only inside this function. They are needed to
   * suppress the same face detected at several strides, and are dropped the
   * moment that is done: what leaves here is a bounded list of scores. Nothing
   * that could locate a face within the photograph crosses the boundary.
   */
  async function handleDetectFaces(command: VisionDetectFacesCommand): Promise<void> {
    const runtime = ort
    const loaded = models.get('face')
    if (!loaded || !runtime) {
      emitError('not_initialised', command.requestId)
      return
    }

    let tensor: unknown
    try {
      tensor = new runtime.Tensor('float32', bgraToFaceTensor(new Uint8Array(command.bitmap)), [
        1,
        3,
        FACE_BITMAP_HEIGHT,
        FACE_BITMAP_WIDTH
      ])
    } catch (error) {
      emitError(codeFrom(error, 'invalid_bitmap'), command.requestId)
      return
    }

    const startedAt = now()
    let outputs: Record<string, OrtTensorData>
    try {
      outputs = await loaded.session.run({ [loaded.inputName]: tensor })
    } catch {
      emitError('detection_failed', command.requestId)
      return
    }
    const elapsedMs = now() - startedAt

    try {
      const scores = detectionScores(collectYunetOutputs(outputs), FACE_INPUT_SIZE)
      emit({
        type: 'face_result',
        requestId: command.requestId,
        scores: scores.buffer as ArrayBuffer,
        elapsedMs,
        workerRssBytes: rssBytes()
      })
    } catch (error) {
      emitError(codeFrom(error, 'invalid_detection_output'), command.requestId)
    }
  }

  /**
   * Phase 3's detection: the same YuNet session, read for geometry as well.
   *
   * This is the only place landmarks are decoded, and it runs only when the
   * labelled-person feature is active. `handleDetectFaces` above still collects
   * three tensor families and cannot reach the fourth.
   */
  async function handleDetectFacesDetailed(command: VisionDetectFacesDetailedCommand): Promise<void> {
    const runtime = ort
    const loaded = models.get('face')
    if (!loaded || !runtime) {
      emitError('not_initialised', command.requestId)
      return
    }

    let tensor: unknown
    try {
      tensor = new runtime.Tensor('float32', bgraToFaceTensor(new Uint8Array(command.bitmap)), [
        1,
        3,
        FACE_BITMAP_HEIGHT,
        FACE_BITMAP_WIDTH
      ])
    } catch (error) {
      emitError(codeFrom(error, 'invalid_bitmap'), command.requestId)
      return
    }

    const startedAt = now()
    let outputs: Record<string, OrtTensorData>
    try {
      outputs = await loaded.session.run({ [loaded.inputName]: tensor })
    } catch {
      emitError('detection_failed', command.requestId)
      return
    }
    const elapsedMs = now() - startedAt

    try {
      const faces = detectLandmarkedFaces(collectYunetLandmarkOutputs(outputs), FACE_INPUT_SIZE).slice(
        0,
        MAX_DETAILED_FACES
      )
      const boxes = new Float32Array(faces.length * FACE_BOX_STRIDE)
      const landmarks = new Float32Array(faces.length * FACE_LANDMARK_STRIDE)
      for (let index = 0; index < faces.length; index += 1) {
        const face = faces[index]!
        boxes[index * FACE_BOX_STRIDE] = face.x
        boxes[index * FACE_BOX_STRIDE + 1] = face.y
        boxes[index * FACE_BOX_STRIDE + 2] = face.width
        boxes[index * FACE_BOX_STRIDE + 3] = face.height
        boxes[index * FACE_BOX_STRIDE + 4] = face.score
        for (let point = 0; point < face.landmarks.length; point += 1) {
          landmarks[index * FACE_LANDMARK_STRIDE + point * 2] = face.landmarks[point]!.x
          landmarks[index * FACE_LANDMARK_STRIDE + point * 2 + 1] = face.landmarks[point]!.y
        }
      }
      emit({
        type: 'face_detail_result',
        requestId: command.requestId,
        count: faces.length,
        boxes: boxes.buffer as ArrayBuffer,
        landmarks: landmarks.buffer as ArrayBuffer,
        elapsedMs,
        workerRssBytes: rssBytes()
      })
    } catch (error) {
      emitError(codeFrom(error, 'invalid_detection_output'), command.requestId)
    }
  }

  /**
   * Embeds already-aligned faces with SFace.
   *
   * One inference at a time, in order, because the sessions are shared and a
   * concurrent run would contend for the same arenas. Every vector is
   * L2-normalized here, before it is emitted: SFace returns a vector whose norm
   * is around 4.8, and an un-normalized vector would make every downstream
   * cosine comparison quietly wrong.
   *
   * Nothing about the tensors or the resulting vectors is logged.
   */
  async function handleEmbedFaces(command: VisionEmbedFacesCommand): Promise<void> {
    const runtime = ort
    const loaded = models.get('faceEmbed')
    if (!loaded || !runtime) {
      emitError('not_initialised', command.requestId)
      return
    }

    const source = new Float32Array(command.tensors)
    const embeddings = new Float32Array(command.count * FACE_EMBED_LENGTH)
    const startedAt = now()

    for (let face = 0; face < command.count; face += 1) {
      const begin = face * FACE_EMBED_TENSOR_FLOATS
      const slice = source.subarray(begin, begin + FACE_EMBED_TENSOR_FLOATS)

      let tensor: unknown
      try {
        tensor = new runtime.Tensor('float32', Float32Array.from(slice), [
          1,
          3,
          FACE_EMBED_SIZE,
          FACE_EMBED_SIZE
        ])
      } catch (error) {
        emitError(codeFrom(error, 'invalid_face_tensor'), command.requestId)
        return
      }

      let outputs: Record<string, OrtTensorData>
      try {
        outputs = await loaded.session.run({ [loaded.inputName]: tensor })
      } catch {
        emitError('face_embed_failed', command.requestId)
        return
      }

      try {
        const vector = normalizedFaceEmbedding(selectOutput('faceEmbed', loaded.session, outputs))
        embeddings.set(vector, face * FACE_EMBED_LENGTH)
      } catch (error) {
        emitError(codeFrom(error, 'invalid_output'), command.requestId)
        return
      }
    }

    emit({
      type: 'face_embeddings_result',
      requestId: command.requestId,
      count: command.count,
      embeddings: embeddings.buffer as ArrayBuffer,
      elapsedMs: now() - startedAt,
      workerRssBytes: rssBytes()
    })
  }

  async function runAndEmit(
    kind: VisionModelKind,
    loaded: LoadedModel,
    feeds: Record<string, unknown>,
    requestId: string
  ): Promise<void> {
    const startedAt = now()
    let outputs: Record<string, OrtTensorData>
    try {
      outputs = await loaded.session.run(feeds)
    } catch {
      emitError('inference_failed', requestId)
      return
    }
    const elapsedMs = now() - startedAt

    try {
      // Normalizing in the worker means every stored and queried vector is unit
      // length, so ranking can use a plain dot product.
      const vector = normalizedEmbedding(selectOutput(kind, loaded.session, outputs))
      emit({
        type: 'embedding_result',
        requestId,
        kind,
        vector: vector.buffer as ArrayBuffer,
        elapsedMs,
        workerRssBytes: rssBytes()
      })
    } catch (error) {
      emitError(codeFrom(error, 'invalid_output'), requestId)
    }
  }

  return { handleMessage }
}

/**
 * BGRA bytes to YuNet's expected planar BGR float tensor.
 *
 * No mean subtraction or scaling: this export consumes raw 0-255 values in BGR
 * order, which is also the order Chromium's `toBitmap()` already produces, so
 * the channels are copied straight across rather than swapped.
 */
export function bgraToFaceTensor(bitmap: Uint8Array): Float32Array {
  const pixels = FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT
  if (bitmap.length !== pixels * 4) {
    throw new VisionProtocolError('invalid_bitmap')
  }

  const tensor = new Float32Array(pixels * 3)
  for (let pixel = 0; pixel < pixels; pixel += 1) {
    const source = pixel * 4
    tensor[pixel] = bitmap[source]! // B plane
    tensor[pixels + pixel] = bitmap[source + 1]! // G plane
    tensor[pixels * 2 + pixel] = bitmap[source + 2]! // R plane
  }
  return tensor
}

/**
 * L2-normalizes one SFace output into a fixed 128-float unit vector.
 *
 * Separate from `normalizedEmbedding`, which enforces CLIP's 512 width. The
 * checks here are the same in spirit: wrong width, non-finite value, or a norm
 * too small to divide by all fail closed rather than producing a vector that
 * would compare meaninglessly against every enrolled profile.
 */
export function normalizedFaceEmbedding(raw: unknown): Float32Array {
  if (!(raw instanceof Float32Array)) {
    throw new VisionProtocolError('invalid_output')
  }
  if (raw.length !== FACE_EMBED_LENGTH) {
    throw new VisionProtocolError('unexpected_embedding_length')
  }

  let sumOfSquares = 0
  for (const value of raw) {
    if (!Number.isFinite(value)) {
      throw new VisionProtocolError('non_finite_embedding')
    }
    sumOfSquares += value * value
  }

  const norm = Math.sqrt(sumOfSquares)
  // A zero-norm vector carries no direction, so it cannot be normalized and
  // must not be passed off as an embedding.
  if (!(norm > 1e-6)) {
    throw new VisionProtocolError('invalid_output')
  }

  const unit = new Float32Array(FACE_EMBED_LENGTH)
  for (let index = 0; index < FACE_EMBED_LENGTH; index += 1) {
    unit[index] = raw[index]! / norm
  }
  return unit
}

/**
 * Gathers YuNet's outputs including the landmark tensors, for Phase 3 only.
 *
 * The counting path uses `collectYunetOutputs` below, which cannot see `kps_*`.
 * Two collectors rather than one parameterised collector, so that the counting
 * path has no argument it could be called with that would return landmarks.
 */
function collectYunetLandmarkOutputs(outputs: Record<string, OrtTensorData>): YunetLandmarkOutputs {
  const collected: YunetLandmarkOutputs = { cls: {}, obj: {}, bbox: {}, kps: {} }
  let found = 0

  for (const stride of FACE_STRIDES) {
    for (const family of ['cls', 'obj', 'bbox', 'kps'] as const) {
      const data = outputs[`${family}_${stride}`]?.data
      if (data instanceof Float32Array) {
        collected[family][stride] = data
        found += 1
      }
    }
  }

  if (found === 0) {
    throw new VisionProtocolError('invalid_detection_output')
  }
  return collected
}

/**
 * Gathers the twelve YuNet outputs into the three this application reads.
 *
 * `kps_*` is deliberately absent: the landmark tensors are the one output that
 * could seed identity work, so they are never collected, never decoded, and
 * never leave the session.
 */
function collectYunetOutputs(outputs: Record<string, OrtTensorData>): YunetOutputs {
  const collected: YunetOutputs = { cls: {}, obj: {}, bbox: {} }
  let found = 0

  for (const stride of FACE_STRIDES) {
    for (const family of ['cls', 'obj', 'bbox'] as const) {
      const data = outputs[`${family}_${stride}`]?.data
      if (data instanceof Float32Array) {
        collected[family][stride] = data
        found += 1
      }
    }
  }

  if (found === 0) {
    throw new VisionProtocolError('invalid_detection_output')
  }
  return collected
}

function selectInputName(kind: VisionModelKind, session: InferenceSession): string {
  if (kind === 'text') {
    return session.inputNames.includes(TEXT_INPUT_IDS) ? TEXT_INPUT_IDS : session.inputNames[0] ?? TEXT_INPUT_IDS
  }
  if (kind === 'faceEmbed') {
    return session.inputNames.includes(FACE_EMBED_INPUT) ? FACE_EMBED_INPUT : session.inputNames[0] ?? FACE_EMBED_INPUT
  }
  return session.inputNames.includes(PREFERRED_IMAGE_INPUT)
    ? PREFERRED_IMAGE_INPUT
    : session.inputNames[0] ?? PREFERRED_IMAGE_INPUT
}

function selectOutput(
  kind: VisionModelKind,
  session: InferenceSession,
  outputs: Record<string, OrtTensorData>
): unknown {
  for (const name of PREFERRED_OUTPUTS[kind]) {
    const candidate = outputs[name]
    if (candidate) {
      return candidate.data
    }
  }
  const firstName = session.outputNames[0]
  const first = firstName === undefined ? undefined : outputs[firstName]
  if (!first) {
    throw new VisionProtocolError('invalid_output')
  }
  return first.data
}

function codeFrom(error: unknown, fallback: VisionErrorCode): VisionErrorCode {
  return error instanceof VisionProtocolError ? error.code : fallback
}

/**
 * Loads ONNX Runtime lazily so a runtime that cannot load surfaces as a bounded
 * error instead of taking the worker down at import time. The interop dance
 * covers both shapes a CommonJS package can take through a dynamic import.
 */
async function loadOnnxRuntime(): Promise<OrtModule | undefined> {
  try {
    const imported = (await import('onnxruntime-node')) as unknown as Record<string, unknown>
    const candidate = (imported.default ?? imported) as OrtModule
    if (typeof candidate?.InferenceSession?.create !== 'function' || typeof candidate?.Tensor !== 'function') {
      return undefined
    }
    return candidate
  } catch {
    return undefined
  }
}

const parentPort = process.parentPort

if (parentPort) {
  const worker = createVisionWorker({
    loadRuntime: loadOnnxRuntime,
    emit: (event) => parentPort.postMessage(event)
  })
  parentPort.on('message', (event) => {
    void worker.handleMessage(event.data)
  })
}
