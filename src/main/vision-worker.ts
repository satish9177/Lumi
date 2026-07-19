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
import { bgraToClipTensor, CLIP_TENSOR_DIMS, normalizedEmbedding } from './vision/preprocess'
import {
  CLIP_CONTEXT_LENGTH,
  parseVisionCommand,
  VisionProtocolError,
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
  text: ['text_embeds', 'pooler_output']
}
const PREFERRED_IMAGE_INPUT = 'pixel_values'
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

function selectInputName(kind: VisionModelKind, session: InferenceSession): string {
  if (kind === 'text') {
    return session.inputNames.includes(TEXT_INPUT_IDS) ? TEXT_INPUT_IDS : session.inputNames[0] ?? TEXT_INPUT_IDS
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
