/**
 * Wire protocol for the local vision worker.
 *
 * Both sides validate before acting: main parses every worker event, the worker
 * parses every main command. Nothing here imports Electron or ONNX Runtime, so
 * the rules stay unit-testable and the worker cannot be steered by a malformed
 * message into loading something it was not told to load.
 *
 * Errors that cross the boundary are bounded codes, never native exception
 * text, stack traces, DLL paths, model paths, or image paths.
 */

export const VISION_BITMAP_WIDTH = 224
export const VISION_BITMAP_HEIGHT = 224
/** Chromium's nativeImage.toBitmap() is BGRA on Windows. */
export const VISION_BITMAP_FORMAT = 'bgra'
export const VISION_BITMAP_BYTES = VISION_BITMAP_WIDTH * VISION_BITMAP_HEIGHT * 4

/** The joint image/text space Phase 1 searches in. */
export const CLIP_EMBEDDING_LENGTH = 512
/**
 * The only embedding width the pipeline accepts. The pinned CLIP ViT-B/32 export
 * projects both towers to exactly this, so any other length is a wrong, corrupt,
 * or mismatched model output and fails closed rather than reaching ranking or
 * persistence.
 */
export const ALLOWED_EMBEDDING_LENGTHS: readonly number[] = [CLIP_EMBEDDING_LENGTH]
export const EMBEDDING_SAMPLE_COUNT = 6

/** CLIP's fixed context window. Every text request is padded to exactly this. */
export const CLIP_CONTEXT_LENGTH = 77
export const CLIP_TOKEN_BYTES = CLIP_CONTEXT_LENGTH * 4

const MAX_MODEL_PATH_LENGTH = 1_024
const MAX_REQUEST_ID_LENGTH = 64

/**
 * Independent sessions: the image tower is disposable, the text tower is cheap
 * to keep, and the face detector is small and loaded only while a face scan is
 * running.
 */
export const VISION_MODEL_KINDS = ['image', 'text', 'face', 'faceEmbed'] as const
export type VisionModelKind = (typeof VISION_MODEL_KINDS)[number]

/** The fixed input the pinned YuNet export accepts. */
export const FACE_BITMAP_WIDTH = 640
export const FACE_BITMAP_HEIGHT = 640
export const FACE_BITMAP_BYTES = FACE_BITMAP_WIDTH * FACE_BITMAP_HEIGHT * 4

/**
 * The most detections whose scores may cross the boundary. Only scores cross:
 * see `face-detect.ts` for why no box ever does.
 */
export const MAX_FACE_SCORES = 64

/**
 * Phase 3 adds a *second*, separate detection command that also returns boxes
 * and landmarks. The Phase-2 `detect_faces` command above is untouched and still
 * answers with scores alone.
 *
 * Keeping them apart is the point. Visible-face counting must remain incapable
 * of locating a face inside a photograph, and it stays that way because its
 * response type has nowhere to put a box. Only the labelled-person path, which
 * the user has explicitly enabled and enrolled into, receives geometry — and it
 * needs it: landmarks are what make alignment possible, and boxes are what let
 * a user point at the right face in a group photo.
 *
 * Geometry crosses to *main* only. It is never persisted, never given to the
 * renderer except as a bounded preview image, and never sent to Realtime.
 */
export const MAX_DETAILED_FACES = 16

/** x, y, width, height, score — in 640x640 detector input space. */
export const FACE_BOX_STRIDE = 5
/** Five landmarks, x and y each. */
export const FACE_LANDMARK_STRIDE = 10

/**
 * The exact input the pinned SFace export accepts, and the width of what it
 * returns. Measured against the pinned revision: `data` [1,3,112,112] and
 * `fc1` [1,128].
 */
export const FACE_EMBED_SIZE = 112
export const FACE_EMBED_TENSOR_FLOATS = 3 * FACE_EMBED_SIZE * FACE_EMBED_SIZE
export const FACE_EMBED_LENGTH = 128

/**
 * A ceiling on faces embedded from one image. A crowd scene must not be able to
 * turn one queued photo into a minute of inference, and the bound also caps the
 * message size in both directions.
 */
export const MAX_EMBED_FACES = 16

export function isVisionModelKind(value: unknown): value is VisionModelKind {
  return typeof value === 'string' && (VISION_MODEL_KINDS as readonly string[]).includes(value)
}

export const VISION_ERROR_CODES = [
  'worker_start_failed',
  'worker_exited',
  'worker_timeout',
  'runtime_load_failed',
  'model_missing',
  'model_load_failed',
  'invalid_message',
  'unknown_command',
  'invalid_bitmap',
  'invalid_tokens',
  'not_initialised',
  'inference_failed',
  'invalid_output',
  'unexpected_embedding_length',
  'non_finite_embedding',
  'image_unavailable',
  'invalid_detection_output',
  'detection_failed',
  'invalid_face_tensor',
  'face_embed_failed'
] as const

export type VisionErrorCode = (typeof VISION_ERROR_CODES)[number]

/** App-authored, path-free, stack-free text for every bounded failure. */
export const VISION_ERROR_MESSAGES: Record<VisionErrorCode, string> = {
  worker_start_failed: 'The local vision worker could not start.',
  worker_exited: 'The local vision worker stopped unexpectedly.',
  worker_timeout: 'The local vision worker did not respond in time.',
  runtime_load_failed: 'The local inference runtime could not be loaded.',
  model_missing: 'The local vision model is not installed.',
  model_load_failed: 'The local vision model could not be loaded.',
  invalid_message: 'The local vision worker received a malformed message.',
  unknown_command: 'The local vision worker received an unsupported command.',
  invalid_bitmap: 'That image was not in the expected bounded format.',
  invalid_tokens: 'That search text could not be prepared for the local model.',
  not_initialised: 'The local vision worker is not ready yet.',
  inference_failed: 'Local inference failed.',
  invalid_output: 'The local model returned an unreadable result.',
  unexpected_embedding_length: 'The local model returned an unexpected embedding size.',
  non_finite_embedding: 'The local model returned a non-finite embedding.',
  image_unavailable: 'That image could not be prepared.',
  invalid_detection_output: 'The local face detector returned an unreadable result.',
  detection_failed: 'Counting visible faces failed for that image.',
  invalid_face_tensor: 'That face could not be prepared for the local model.',
  face_embed_failed: 'Comparing that face against your saved people failed.'
}

export class VisionProtocolError extends Error {
  constructor(readonly code: VisionErrorCode) {
    super(VISION_ERROR_MESSAGES[code])
    this.name = 'VisionProtocolError'
  }
}

export function isVisionErrorCode(value: unknown): value is VisionErrorCode {
  return typeof value === 'string' && (VISION_ERROR_CODES as readonly string[]).includes(value)
}

export function boundedMessageFor(code: VisionErrorCode): string {
  return VISION_ERROR_MESSAGES[code]
}

export interface VisionLoadModelCommand {
  type: 'load_model'
  kind: VisionModelKind
  /** Main-owned absolute path. Never sourced from renderer, IPC, or a model. */
  modelPath: string
}

export interface VisionUnloadModelCommand {
  type: 'unload_model'
  kind: VisionModelKind
}

export interface VisionEmbedImageCommand {
  type: 'embed_image'
  requestId: string
  width: number
  height: number
  format: typeof VISION_BITMAP_FORMAT
  bitmap: ArrayBuffer
}

export interface VisionEmbedTextCommand {
  type: 'embed_text'
  requestId: string
  /** Int32Array of exactly CLIP_CONTEXT_LENGTH token ids, already padded. */
  tokenIds: ArrayBuffer
  /** Count of real (non-padding) tokens, used to build the attention mask. */
  tokenCount: number
}

export interface VisionDetectFacesCommand {
  type: 'detect_faces'
  requestId: string
  width: number
  height: number
  format: typeof VISION_BITMAP_FORMAT
  /** A 640x640 letterboxed BGRA bitmap, prepared in main. */
  bitmap: ArrayBuffer
}

export interface VisionShutdownCommand {
  type: 'shutdown'
}

/**
 * Phase 3's detection request. Same bounded 640x640 bitmap as `detect_faces`,
 * different answer: this one returns geometry, and only the labelled-person
 * path issues it.
 */
export interface VisionDetectFacesDetailedCommand {
  type: 'detect_faces_detailed'
  requestId: string
  width: number
  height: number
  format: typeof VISION_BITMAP_FORMAT
  bitmap: ArrayBuffer
}

/**
 * Already-aligned faces, ready for SFace.
 *
 * The command carries pixels and nothing else. There is deliberately no field
 * for a file path, a model path, a model identifier, a profile id, or a label:
 * the worker cannot be steered toward a different model or a different file by
 * anything in this message, because none of those are expressible in it.
 */
export interface VisionEmbedFacesCommand {
  type: 'embed_faces'
  requestId: string
  /** How many aligned faces the buffer holds. */
  count: number
  /** Float32Array of exactly `count * FACE_EMBED_TENSOR_FLOATS` values. */
  tensors: ArrayBuffer
}

export type VisionCommand =
  | VisionLoadModelCommand
  | VisionUnloadModelCommand
  | VisionEmbedImageCommand
  | VisionEmbedTextCommand
  | VisionDetectFacesCommand
  | VisionDetectFacesDetailedCommand
  | VisionEmbedFacesCommand
  | VisionShutdownCommand

export interface VisionReadyEvent {
  type: 'ready'
  runtimeVersion: string
}

export interface VisionModelLoadedEvent {
  type: 'model_loaded'
  kind: VisionModelKind
  sessionLoadMs: number
}

export interface VisionModelUnloadedEvent {
  type: 'model_unloaded'
  kind: VisionModelKind
}

export interface VisionEmbeddingResultEvent {
  type: 'embedding_result'
  requestId: string
  kind: VisionModelKind
  /** Float32Array payload. Stays in main; never reaches the renderer or OpenAI. */
  vector: ArrayBuffer
  elapsedMs: number
  workerRssBytes: number
}

export interface VisionBoundedErrorEvent {
  type: 'bounded_error'
  code: VisionErrorCode
  requestId?: string
}

/**
 * The result of a face scan: confidence scores only.
 *
 * There is deliberately no field for boxes, landmarks, crops, or any descriptor.
 * The worker decodes and suppresses geometry internally and discards it, so the
 * only thing that can be learned on this side of the boundary is how many
 * face-shaped regions were found and how sure the detector was.
 */
export interface VisionFaceResultEvent {
  type: 'face_result'
  requestId: string
  /** Float32Array of at most MAX_FACE_SCORES values, each in [0, 1]. */
  scores: ArrayBuffer
  elapsedMs: number
  workerRssBytes: number
}

/**
 * Boxes and landmarks for one image, in detector input space.
 *
 * Received by main, used to align and to let the user point at a face, then
 * dropped. Nothing here is written to the index; see `index-store.ts`, which has
 * no field capable of holding a box or a landmark.
 */
export interface VisionFaceDetailResultEvent {
  type: 'face_detail_result'
  requestId: string
  count: number
  /** Float32Array of `count * FACE_BOX_STRIDE` values. */
  boxes: ArrayBuffer
  /** Float32Array of `count * FACE_LANDMARK_STRIDE` values. */
  landmarks: ArrayBuffer
  elapsedMs: number
  workerRssBytes: number
}

/**
 * One L2-normalized 128-float embedding per submitted face.
 *
 * Normalization happens in the worker, before this event is constructed, so
 * there is no path by which an un-normalized vector reaches comparison. SFace
 * does not return a unit vector on its own.
 */
export interface VisionFaceEmbeddingsResultEvent {
  type: 'face_embeddings_result'
  requestId: string
  count: number
  /** Float32Array of exactly `count * FACE_EMBED_LENGTH` values. */
  embeddings: ArrayBuffer
  elapsedMs: number
  workerRssBytes: number
}

export type VisionEvent =
  | VisionReadyEvent
  | VisionModelLoadedEvent
  | VisionModelUnloadedEvent
  | VisionEmbeddingResultEvent
  | VisionFaceResultEvent
  | VisionFaceDetailResultEvent
  | VisionFaceEmbeddingsResultEvent
  | VisionBoundedErrorEvent

export function parseVisionCommand(raw: unknown): VisionCommand {
  const record = asRecord(raw)
  switch (record.type) {
    case 'load_model': {
      assertOnlyKeys(record, ['type', 'kind', 'modelPath'])
      if (!isVisionModelKind(record.kind)) {
        throw new VisionProtocolError('invalid_message')
      }
      const { modelPath } = record
      if (typeof modelPath !== 'string' || modelPath.length === 0 || modelPath.length > MAX_MODEL_PATH_LENGTH) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'load_model', kind: record.kind, modelPath }
    }
    case 'unload_model': {
      assertOnlyKeys(record, ['type', 'kind'])
      if (!isVisionModelKind(record.kind)) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'unload_model', kind: record.kind }
    }
    case 'embed_image': {
      assertOnlyKeys(record, ['type', 'requestId', 'width', 'height', 'format', 'bitmap'])
      const requestId = asRequestId(record.requestId)
      if (record.format !== VISION_BITMAP_FORMAT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      if (record.width !== VISION_BITMAP_WIDTH || record.height !== VISION_BITMAP_HEIGHT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      const bitmap = record.bitmap
      if (!(bitmap instanceof ArrayBuffer) || bitmap.byteLength !== VISION_BITMAP_BYTES) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      return {
        type: 'embed_image',
        requestId,
        width: VISION_BITMAP_WIDTH,
        height: VISION_BITMAP_HEIGHT,
        format: VISION_BITMAP_FORMAT,
        bitmap
      }
    }
    case 'embed_text': {
      assertOnlyKeys(record, ['type', 'requestId', 'tokenIds', 'tokenCount'])
      const requestId = asRequestId(record.requestId)
      const tokenIds = record.tokenIds
      if (!(tokenIds instanceof ArrayBuffer) || tokenIds.byteLength !== CLIP_TOKEN_BYTES) {
        throw new VisionProtocolError('invalid_tokens')
      }
      const tokenCount = record.tokenCount
      if (
        typeof tokenCount !== 'number' ||
        !Number.isInteger(tokenCount) ||
        tokenCount < 2 ||
        tokenCount > CLIP_CONTEXT_LENGTH
      ) {
        throw new VisionProtocolError('invalid_tokens')
      }
      return { type: 'embed_text', requestId, tokenIds, tokenCount }
    }
    case 'detect_faces': {
      assertOnlyKeys(record, ['type', 'requestId', 'width', 'height', 'format', 'bitmap'])
      const requestId = asRequestId(record.requestId)
      if (record.format !== VISION_BITMAP_FORMAT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      if (record.width !== FACE_BITMAP_WIDTH || record.height !== FACE_BITMAP_HEIGHT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      const bitmap = record.bitmap
      if (!(bitmap instanceof ArrayBuffer) || bitmap.byteLength !== FACE_BITMAP_BYTES) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      return {
        type: 'detect_faces',
        requestId,
        width: FACE_BITMAP_WIDTH,
        height: FACE_BITMAP_HEIGHT,
        format: VISION_BITMAP_FORMAT,
        bitmap
      }
    }
    case 'detect_faces_detailed': {
      assertOnlyKeys(record, ['type', 'requestId', 'width', 'height', 'format', 'bitmap'])
      const requestId = asRequestId(record.requestId)
      if (record.format !== VISION_BITMAP_FORMAT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      if (record.width !== FACE_BITMAP_WIDTH || record.height !== FACE_BITMAP_HEIGHT) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      const bitmap = record.bitmap
      if (!(bitmap instanceof ArrayBuffer) || bitmap.byteLength !== FACE_BITMAP_BYTES) {
        throw new VisionProtocolError('invalid_bitmap')
      }
      return {
        type: 'detect_faces_detailed',
        requestId,
        width: FACE_BITMAP_WIDTH,
        height: FACE_BITMAP_HEIGHT,
        format: VISION_BITMAP_FORMAT,
        bitmap
      }
    }
    case 'embed_faces': {
      assertOnlyKeys(record, ['type', 'requestId', 'count', 'tensors'])
      const requestId = asRequestId(record.requestId)
      const count = record.count
      if (typeof count !== 'number' || !Number.isInteger(count) || count < 1 || count > MAX_EMBED_FACES) {
        throw new VisionProtocolError('invalid_face_tensor')
      }
      const tensors = record.tensors
      // The buffer must be exactly the declared number of complete 112x112x3
      // tensors — not more, not fewer, and not a partial one.
      if (
        !(tensors instanceof ArrayBuffer) ||
        tensors.byteLength !== count * FACE_EMBED_TENSOR_FLOATS * 4
      ) {
        throw new VisionProtocolError('invalid_face_tensor')
      }
      for (const value of new Float32Array(tensors)) {
        // Pixels, in the 0-255 range the aligner produces. A NaN or an
        // out-of-range value means the buffer is not what this protocol
        // describes, so it is refused rather than clamped into plausibility.
        if (!Number.isFinite(value) || value < 0 || value > 255) {
          throw new VisionProtocolError('invalid_face_tensor')
        }
      }
      return { type: 'embed_faces', requestId, count, tensors }
    }
    case 'shutdown':
      assertOnlyKeys(record, ['type'])
      return { type: 'shutdown' }
    default:
      throw new VisionProtocolError('unknown_command')
  }
}

export function parseVisionEvent(raw: unknown): VisionEvent {
  const record = asRecord(raw)
  switch (record.type) {
    case 'ready': {
      assertOnlyKeys(record, ['type', 'runtimeVersion'])
      const { runtimeVersion } = record
      if (typeof runtimeVersion !== 'string' || runtimeVersion.length === 0 || runtimeVersion.length > 64) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'ready', runtimeVersion }
    }
    case 'model_loaded': {
      assertOnlyKeys(record, ['type', 'kind', 'sessionLoadMs'])
      if (!isVisionModelKind(record.kind)) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'model_loaded', kind: record.kind, sessionLoadMs: asDuration(record.sessionLoadMs) }
    }
    case 'model_unloaded': {
      assertOnlyKeys(record, ['type', 'kind'])
      if (!isVisionModelKind(record.kind)) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'model_unloaded', kind: record.kind }
    }
    case 'embedding_result': {
      assertOnlyKeys(record, ['type', 'requestId', 'kind', 'vector', 'elapsedMs', 'workerRssBytes'])
      const requestId = asRequestId(record.requestId)
      if (!isVisionModelKind(record.kind)) {
        throw new VisionProtocolError('invalid_message')
      }
      const vector = record.vector
      if (!(vector instanceof ArrayBuffer)) {
        throw new VisionProtocolError('invalid_output')
      }
      if (vector.byteLength % 4 !== 0 || !ALLOWED_EMBEDDING_LENGTHS.includes(vector.byteLength / 4)) {
        throw new VisionProtocolError('unexpected_embedding_length')
      }
      const values = new Float32Array(vector)
      for (const value of values) {
        if (!Number.isFinite(value)) {
          throw new VisionProtocolError('non_finite_embedding')
        }
      }
      return {
        type: 'embedding_result',
        requestId,
        kind: record.kind,
        vector,
        elapsedMs: asDuration(record.elapsedMs),
        workerRssBytes: asDuration(record.workerRssBytes)
      }
    }
    case 'face_result': {
      assertOnlyKeys(record, ['type', 'requestId', 'scores', 'elapsedMs', 'workerRssBytes'])
      const requestId = asRequestId(record.requestId)
      const scores = record.scores
      if (!(scores instanceof ArrayBuffer) || scores.byteLength % 4 !== 0) {
        throw new VisionProtocolError('invalid_detection_output')
      }
      if (scores.byteLength / 4 > MAX_FACE_SCORES) {
        throw new VisionProtocolError('invalid_detection_output')
      }
      // A score outside [0, 1] is not a probability, so the message is not the
      // one this protocol describes and is rejected rather than clamped.
      for (const value of new Float32Array(scores)) {
        if (!Number.isFinite(value) || value < 0 || value > 1) {
          throw new VisionProtocolError('invalid_detection_output')
        }
      }
      return {
        type: 'face_result',
        requestId,
        scores,
        elapsedMs: asDuration(record.elapsedMs),
        workerRssBytes: asDuration(record.workerRssBytes)
      }
    }
    case 'face_detail_result': {
      assertOnlyKeys(record, ['type', 'requestId', 'count', 'boxes', 'landmarks', 'elapsedMs', 'workerRssBytes'])
      const requestId = asRequestId(record.requestId)
      const count = record.count
      if (typeof count !== 'number' || !Number.isInteger(count) || count < 0 || count > MAX_DETAILED_FACES) {
        throw new VisionProtocolError('invalid_detection_output')
      }
      const boxes = record.boxes
      const landmarks = record.landmarks
      if (
        !(boxes instanceof ArrayBuffer) ||
        !(landmarks instanceof ArrayBuffer) ||
        boxes.byteLength !== count * FACE_BOX_STRIDE * 4 ||
        landmarks.byteLength !== count * FACE_LANDMARK_STRIDE * 4
      ) {
        throw new VisionProtocolError('invalid_detection_output')
      }
      for (const value of new Float32Array(boxes)) {
        if (!Number.isFinite(value)) {
          throw new VisionProtocolError('invalid_detection_output')
        }
      }
      for (const value of new Float32Array(landmarks)) {
        if (!Number.isFinite(value)) {
          throw new VisionProtocolError('invalid_detection_output')
        }
      }
      return {
        type: 'face_detail_result',
        requestId,
        count,
        boxes,
        landmarks,
        elapsedMs: asDuration(record.elapsedMs),
        workerRssBytes: asDuration(record.workerRssBytes)
      }
    }
    case 'face_embeddings_result': {
      assertOnlyKeys(record, ['type', 'requestId', 'count', 'embeddings', 'elapsedMs', 'workerRssBytes'])
      const requestId = asRequestId(record.requestId)
      const count = record.count
      if (typeof count !== 'number' || !Number.isInteger(count) || count < 0 || count > MAX_EMBED_FACES) {
        throw new VisionProtocolError('invalid_output')
      }
      const embeddings = record.embeddings
      // Exactly 128 floats per face. A different width means a different model
      // produced this, which must fail closed rather than reach comparison.
      if (
        !(embeddings instanceof ArrayBuffer) ||
        embeddings.byteLength !== count * FACE_EMBED_LENGTH * 4
      ) {
        throw new VisionProtocolError('unexpected_embedding_length')
      }
      const values = new Float32Array(embeddings)
      for (const value of values) {
        if (!Number.isFinite(value)) {
          throw new VisionProtocolError('non_finite_embedding')
        }
      }
      // Re-checked on receipt, not merely on production: an embedding that is
      // not a unit vector would make every cosine comparison silently wrong.
      for (let face = 0; face < count; face += 1) {
        let sumOfSquares = 0
        for (let index = 0; index < FACE_EMBED_LENGTH; index += 1) {
          const value = values[face * FACE_EMBED_LENGTH + index]!
          sumOfSquares += value * value
        }
        if (Math.abs(Math.sqrt(sumOfSquares) - 1) > 1e-3) {
          throw new VisionProtocolError('invalid_output')
        }
      }
      return {
        type: 'face_embeddings_result',
        requestId,
        count,
        embeddings,
        elapsedMs: asDuration(record.elapsedMs),
        workerRssBytes: asDuration(record.workerRssBytes)
      }
    }
    case 'bounded_error': {
      assertOnlyKeys(record, ['type', 'code', 'requestId'])
      if (!isVisionErrorCode(record.code)) {
        throw new VisionProtocolError('invalid_message')
      }
      const requestId = record.requestId
      if (requestId !== undefined && (typeof requestId !== 'string' || requestId.length > MAX_REQUEST_ID_LENGTH)) {
        throw new VisionProtocolError('invalid_message')
      }
      return { type: 'bounded_error', code: record.code, requestId: requestId as string | undefined }
    }
    default:
      throw new VisionProtocolError('invalid_message')
  }
}

function asRequestId(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_REQUEST_ID_LENGTH) {
    throw new VisionProtocolError('invalid_message')
  }
  return value
}

function asRecord(raw: unknown): Record<string, unknown> {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new VisionProtocolError('invalid_message')
  }
  return raw as Record<string, unknown>
}

function assertOnlyKeys(record: Record<string, unknown>, allowed: readonly string[]): void {
  for (const key of Object.keys(record)) {
    if (!allowed.includes(key)) {
      throw new VisionProtocolError('invalid_message')
    }
  }
}

function asDuration(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new VisionProtocolError('invalid_message')
  }
  return value
}
