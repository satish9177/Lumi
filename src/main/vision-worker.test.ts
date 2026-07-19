import { describe, expect, it, vi } from 'vitest'
import { createVisionWorker, type InferenceSession, type OrtModule } from './vision-worker'
import {
  CLIP_CONTEXT_LENGTH,
  CLIP_EMBEDDING_LENGTH,
  VISION_BITMAP_BYTES,
  type VisionEvent
} from './vision/protocol'

/**
 * Exercises the real worker message handler with a stubbed ONNX Runtime and a
 * captured emit sink — no utilityProcess, no native library. The engine tests
 * cover the parent side with a fake worker; this is the one place the worker's
 * own branches (runtime loading, tensor construction, output validation, bounded
 * error mapping) are driven directly.
 */

interface SessionOptions {
  inputNames?: string[]
  outputNames?: string[]
  run?: InferenceSession['run']
  release?: () => Promise<void>
}

function imageSession(options: SessionOptions = {}): InferenceSession {
  return {
    inputNames: options.inputNames ?? ['pixel_values'],
    outputNames: options.outputNames ?? ['image_embeds'],
    run: options.run ?? (async () => ({ image_embeds: { data: new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.05) } })),
    release: options.release ?? (async () => undefined)
  }
}

function textSession(capture?: (feeds: Record<string, unknown>) => void, options: SessionOptions = {}): InferenceSession {
  return {
    inputNames: options.inputNames ?? ['input_ids', 'attention_mask'],
    outputNames: options.outputNames ?? ['text_embeds'],
    run:
      options.run ??
      (async (feeds) => {
        capture?.(feeds)
        return { text_embeds: { data: new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.02) } }
      }),
    release: options.release ?? (async () => undefined)
  }
}

interface HarnessOptions {
  runtimeUnavailable?: boolean
  createThrows?: boolean
  fileExists?: boolean
  session?: (path: string) => InferenceSession
}

function harness(options: HarnessOptions = {}) {
  const events: VisionEvent[] = []
  const exits: number[] = []
  const created: Array<{ path: string; opts: unknown }> = []

  const Tensor = vi.fn(function (this: Record<string, unknown>, type: string, data: unknown, dims: readonly number[]) {
    this.type = type
    this.data = data
    this.dims = dims
  }) as unknown as OrtModule['Tensor']

  const runtime: OrtModule = {
    InferenceSession: {
      create: vi.fn(async (path: string, opts?: unknown) => {
        created.push({ path, opts })
        if (options.createThrows) {
          // Native text a leak would surface, including a path and a DLL name.
          throw new Error('LoadLibrary failed for C:\\pack\\vision_model_quantized.onnx (onnxruntime.dll)')
        }
        return (options.session ?? defaultSession)(path)
      })
    },
    Tensor,
    env: { versions: { common: '1.27.0' } }
  }

  const worker = createVisionWorker({
    loadRuntime: async () => (options.runtimeUnavailable ? undefined : runtime),
    emit: (event) => events.push(event),
    exit: (code) => exits.push(code),
    now: () => 1_000,
    rssBytes: () => 42_000,
    fileExists: () => options.fileExists ?? true
  })

  return { worker, events, exits, created, Tensor }
}

function defaultSession(path: string): InferenceSession {
  return path.includes('text') ? textSession() : imageSession()
}

function loadImage(path = 'C:\\pack\\vision_model_quantized.onnx') {
  return { type: 'load_model', kind: 'image', modelPath: path }
}

function loadText(path = 'C:\\pack\\text_model_quantized.onnx') {
  return { type: 'load_model', kind: 'text', modelPath: path }
}

function imageCommand(requestId = 'r1') {
  return {
    type: 'embed_image',
    requestId,
    width: 224,
    height: 224,
    format: 'bgra',
    bitmap: new ArrayBuffer(VISION_BITMAP_BYTES)
  }
}

function textCommand(requestId = 'r1', tokenCount = 3) {
  return {
    type: 'embed_text',
    requestId,
    tokenIds: new Int32Array(CLIP_CONTEXT_LENGTH).buffer,
    tokenCount
  }
}

function lastEvent(events: VisionEvent[]): VisionEvent {
  return events[events.length - 1]!
}

describe('vision worker: loading', () => {
  it('loads a tower on CPU and reports it, without requesting DirectML', async () => {
    const { worker, events, created } = harness()

    await worker.handleMessage(loadImage())

    expect(lastEvent(events)).toMatchObject({ type: 'model_loaded', kind: 'image' })
    expect(created).toHaveLength(1)
    expect((created[0]!.opts as { executionProviders: string[] }).executionProviders).toEqual(['cpu'])
    expect(JSON.stringify(created[0]!.opts)).not.toMatch(/dml|directml/i)
  })

  it('reports an already-loaded tower without recreating the session', async () => {
    const { worker, events, created } = harness()
    await worker.handleMessage(loadImage())
    await worker.handleMessage(loadImage())

    expect(created).toHaveLength(1)
    expect(events.filter((event) => event.type === 'model_loaded')).toHaveLength(2)
  })

  it('reports model_missing when the model file is absent', async () => {
    const { worker, events, created } = harness({ fileExists: false })

    await worker.handleMessage(loadImage())

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'model_missing' })
    expect(created).toHaveLength(0)
  })

  it('reports runtime_load_failed when ONNX Runtime cannot be loaded', async () => {
    const { worker, events } = harness({ runtimeUnavailable: true })

    await worker.handleMessage(loadImage())

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'runtime_load_failed' })
  })

  it('reports model_load_failed when session creation throws', async () => {
    const { worker, events } = harness({ createThrows: true })

    await worker.handleMessage(loadImage())

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'model_load_failed' })
  })
})

describe('vision worker: image inference', () => {
  it('embeds a valid bitmap into a unit-length 512-d vector', async () => {
    const { worker, events } = harness()
    await worker.handleMessage(loadImage())

    await worker.handleMessage(imageCommand('img-1'))

    const result = lastEvent(events)
    expect(result.type).toBe('embedding_result')
    if (result.type === 'embedding_result') {
      expect(result.requestId).toBe('img-1')
      expect(result.kind).toBe('image')
      expect(result.workerRssBytes).toBe(42_000)
      const vector = new Float32Array(result.vector)
      expect(vector).toHaveLength(CLIP_EMBEDDING_LENGTH)
      const magnitude = Math.sqrt(vector.reduce((total, value) => total + value * value, 0))
      expect(magnitude).toBeCloseTo(1, 4)
    }
  })

  it('rejects an image request before its tower is loaded', async () => {
    const { worker, events } = harness()

    await worker.handleMessage(imageCommand('img-1'))

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'not_initialised', requestId: 'img-1' })
  })

  it('maps an inference failure to a bounded code carrying the request id', async () => {
    const { worker, events } = harness({
      session: () => imageSession({ run: async () => { throw new Error('kernel crashed at 0xDEADBEEF') } })
    })
    await worker.handleMessage(loadImage())

    await worker.handleMessage(imageCommand('img-9'))

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'inference_failed', requestId: 'img-9' })
  })
})

describe('vision worker: text inference', () => {
  it('embeds tokens and feeds an int64 attention mask when the model takes one', async () => {
    let captured: Record<string, unknown> | undefined
    const { worker, events } = harness({ session: () => textSession((feeds) => (captured = feeds)) })
    await worker.handleMessage(loadText())

    await worker.handleMessage(textCommand('txt-1', 3))

    expect(lastEvent(events).type).toBe('embedding_result')
    expect(captured).toBeDefined()
    expect(Object.keys(captured!)).toEqual(['input_ids', 'attention_mask'])
  })

  it('omits the attention mask for a model that does not declare one', async () => {
    let captured: Record<string, unknown> | undefined
    const { worker } = harness({
      session: () => textSession((feeds) => (captured = feeds), { inputNames: ['input_ids'] })
    })
    await worker.handleMessage(loadText())

    await worker.handleMessage(textCommand('txt-2', 3))

    expect(Object.keys(captured!)).toEqual(['input_ids'])
  })

  it('rejects a text request before its tower is loaded', async () => {
    const { worker, events } = harness()

    await worker.handleMessage(textCommand('txt-3'))

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'not_initialised', requestId: 'txt-3' })
  })
})

describe('vision worker: output validation', () => {
  async function embedWith(data: unknown): Promise<VisionEvent> {
    const { worker, events } = harness({
      session: () => imageSession({ run: async () => ({ image_embeds: { data } }) })
    })
    await worker.handleMessage(loadImage())
    await worker.handleMessage(imageCommand('v'))
    return lastEvent(events)
  }

  it('rejects a wrong embedding width, including the unprojected 768 pooler size', async () => {
    expect(await embedWith(new Float32Array(768).fill(0.1))).toEqual({
      type: 'bounded_error',
      code: 'unexpected_embedding_length',
      requestId: 'v'
    })
  })

  it('rejects a non-finite embedding', async () => {
    const values = new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.1)
    values[3] = Number.POSITIVE_INFINITY
    expect(await embedWith(values)).toEqual({
      type: 'bounded_error',
      code: 'non_finite_embedding',
      requestId: 'v'
    })
  })

  it('rejects a zero-norm embedding that has no direction', async () => {
    expect(await embedWith(new Float32Array(CLIP_EMBEDDING_LENGTH))).toEqual({
      type: 'bounded_error',
      code: 'invalid_output',
      requestId: 'v'
    })
  })

  it('rejects an output that is not a numeric vector', async () => {
    expect(await embedWith(undefined)).toEqual({ type: 'bounded_error', code: 'invalid_output', requestId: 'v' })
  })

  it('falls back to the first output when the preferred name is absent', async () => {
    const { worker, events } = harness({
      session: () =>
        imageSession({
          outputNames: ['some_other_head'],
          run: async () => ({ some_other_head: { data: new Float32Array(CLIP_EMBEDDING_LENGTH).fill(0.05) } })
        })
    })
    await worker.handleMessage(loadImage())
    await worker.handleMessage(imageCommand('v'))

    expect(lastEvent(events).type).toBe('embedding_result')
  })
})

describe('vision worker: lifecycle and protocol', () => {
  it('unloads a tower and releases its session', async () => {
    const release = vi.fn(async () => undefined)
    const { worker, events } = harness({ session: () => imageSession({ release }) })
    await worker.handleMessage(loadImage())

    await worker.handleMessage({ type: 'unload_model', kind: 'image' })

    expect(release).toHaveBeenCalledTimes(1)
    expect(lastEvent(events)).toEqual({ type: 'model_unloaded', kind: 'image' })
  })

  it('clears state and exits on shutdown', async () => {
    const { worker, exits } = harness()
    await worker.handleMessage(loadImage())

    await worker.handleMessage({ type: 'shutdown' })

    expect(exits).toEqual([0])
  })

  it('maps an unknown command to a bounded code', async () => {
    const { worker, events } = harness()

    await worker.handleMessage({ type: 'run_arbitrary_model', modelPath: 'C:\\evil.onnx' })

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'unknown_command' })
  })

  it('maps a malformed message to a bounded code', async () => {
    const { worker, events } = harness()

    await worker.handleMessage('not an object')

    expect(lastEvent(events)).toEqual({ type: 'bounded_error', code: 'invalid_message' })
  })

  it('never leaks a path, DLL name, ONNX filename, or stack in an emitted error', async () => {
    const { worker, events } = harness({ createThrows: true })
    await worker.handleMessage(loadImage())
    await worker.handleMessage({ type: 'unknown' })

    const serialized = JSON.stringify(events)
    expect(serialized).not.toMatch(/[a-zA-Z]:\\/)
    expect(serialized).not.toMatch(/\.onnx|\.dll/i)
    expect(serialized).not.toMatch(/LoadLibrary|Error:|at \w+/)
    // Every emitted error is a bare code, nothing more.
    for (const event of events) {
      if (event.type === 'bounded_error') {
        expect(Object.keys(event).sort()).toEqual(['code', 'type'])
      }
    }
  })
})
