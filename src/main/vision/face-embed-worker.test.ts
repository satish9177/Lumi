import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { createVisionWorker, normalizedFaceEmbedding, type InferenceSession, type OrtModule } from '../vision-worker'
import {
  FACE_BITMAP_BYTES,
  FACE_EMBED_LENGTH,
  FACE_EMBED_SIZE,
  FACE_EMBED_TENSOR_FLOATS,
  MAX_EMBED_FACES,
  parseVisionCommand,
  parseVisionEvent,
  VisionProtocolError,
  type VisionEvent
} from './protocol'

/**
 * The Phase-3 half of the worker protocol: SFace embedding and the detection
 * command that returns geometry. The Phase-2 counting path is covered in
 * `vision-worker.test.ts` and is deliberately re-asserted here only where this
 * phase could have widened it.
 */

/** An SFace stand-in returning a fixed, deliberately un-normalized vector. */
function faceEmbedSession(options: { output?: Float32Array; outputNames?: string[] } = {}): InferenceSession {
  return {
    inputNames: ['data'],
    outputNames: options.outputNames ?? ['fc1'],
    // Norm ≈ 4.8, like the real model. If the worker forgot to normalize, the
    // parser downstream would reject it — which is the point.
    run: async () => ({ fc1: { data: options.output ?? new Float32Array(FACE_EMBED_LENGTH).fill(0.4243) } }),
    release: async () => undefined
  }
}

function harness(session: (path: string) => InferenceSession = () => faceEmbedSession()) {
  const events: VisionEvent[] = []
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
        return session(path)
      })
    },
    Tensor,
    env: { versions: { common: '1.27.0' } }
  }

  const worker = createVisionWorker({
    loadRuntime: async () => runtime,
    emit: (event) => events.push(event),
    exit: () => undefined,
    now: () => 1_000,
    rssBytes: () => 42_000,
    fileExists: () => true
  })

  return { worker, events, created, Tensor }
}

const loadFaceEmbed = { type: 'load_model', kind: 'faceEmbed', modelPath: 'C:\\pack\\sface.onnx' }

function embedCommand(count = 1, fill = 128): Record<string, unknown> {
  return {
    type: 'embed_faces',
    requestId: 'r1',
    count,
    tensors: new Float32Array(count * FACE_EMBED_TENSOR_FLOATS).fill(fill).buffer
  }
}

describe('the embed command accepts only bounded aligned tensors', () => {
  it('accepts exactly one complete 112x112x3 tensor per declared face', () => {
    const parsed = parseVisionCommand(embedCommand(2))
    expect(parsed.type).toBe('embed_faces')
    expect((parsed as { count: number }).count).toBe(2)
  })

  it('rejects a buffer that does not match the declared face count', () => {
    // One tensor's worth of bytes but two faces declared, and vice versa.
    expect(() =>
      parseVisionCommand({ ...embedCommand(1), count: 2 })
    ).toThrow(VisionProtocolError)
    expect(() =>
      parseVisionCommand({ ...embedCommand(2), count: 1 })
    ).toThrow(VisionProtocolError)
  })

  it('rejects a partial tensor', () => {
    expect(() =>
      parseVisionCommand({
        type: 'embed_faces',
        requestId: 'r1',
        count: 1,
        tensors: new Float32Array(FACE_EMBED_TENSOR_FLOATS - 3).buffer
      })
    ).toThrow(VisionProtocolError)
  })

  it('rejects zero faces and more than the ceiling', () => {
    expect(() => parseVisionCommand({ ...embedCommand(1), count: 0 })).toThrow(VisionProtocolError)
    expect(() => parseVisionCommand(embedCommand(MAX_EMBED_FACES + 1))).toThrow(VisionProtocolError)
  })

  it('rejects non-finite and out-of-range pixel values', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, -1, 256]) {
      const tensors = new Float32Array(FACE_EMBED_TENSOR_FLOATS).fill(100)
      tensors[17] = bad
      expect(() =>
        parseVisionCommand({ type: 'embed_faces', requestId: 'r1', count: 1, tensors: tensors.buffer })
      ).toThrow(VisionProtocolError)
    }
  })

  it('has no field for a path, a model identifier, or a profile', () => {
    // The renderer cannot steer which model runs, because the command has
    // nowhere to express it. Unknown keys are refused outright.
    for (const extra of ['modelPath', 'model', 'path', 'profileId', 'label', 'file']) {
      expect(() => parseVisionCommand({ ...embedCommand(1), [extra]: 'anything' })).toThrow(VisionProtocolError)
    }
  })
})

describe('the worker normalizes every embedding it returns', () => {
  it('returns exactly 128 values per face', async () => {
    const { worker, events } = harness()
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(3))

    const result = events.find((event) => event.type === 'face_embeddings_result')
    expect(result).toBeDefined()
    const embeddings = new Float32Array((result as { embeddings: ArrayBuffer }).embeddings)
    expect(embeddings.length).toBe(3 * FACE_EMBED_LENGTH)
  })

  it('emits unit vectors even though SFace does not produce them', async () => {
    const { worker, events } = harness()
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(2))

    const result = events.find((event) => event.type === 'face_embeddings_result')!
    const embeddings = new Float32Array((result as { embeddings: ArrayBuffer }).embeddings)
    for (let face = 0; face < 2; face += 1) {
      let sumOfSquares = 0
      for (let index = 0; index < FACE_EMBED_LENGTH; index += 1) {
        const value = embeddings[face * FACE_EMBED_LENGTH + index]!
        sumOfSquares += value * value
      }
      expect(Math.sqrt(sumOfSquares)).toBeCloseTo(1, 5)
    }
  })

  it('feeds the model a [1,3,112,112] tensor', async () => {
    const { worker, Tensor } = harness()
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(1))

    const call = (Tensor as unknown as { mock: { calls: unknown[][] } }).mock.calls.at(-1)!
    expect(call[0]).toBe('float32')
    expect(call[2]).toEqual([1, 3, FACE_EMBED_SIZE, FACE_EMBED_SIZE])
  })

  it('runs one inference per face rather than batching blindly', async () => {
    let runs = 0
    const { worker } = harness(() => ({
      inputNames: ['data'],
      outputNames: ['fc1'],
      run: async () => {
        runs += 1
        return { fc1: { data: new Float32Array(FACE_EMBED_LENGTH).fill(0.4243) } }
      },
      release: async () => undefined
    }))
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(4))
    expect(runs).toBe(4)
  })
})

describe('bad model output fails closed', () => {
  it('rejects a vector of the wrong width', async () => {
    const { worker, events } = harness(() => faceEmbedSession({ output: new Float32Array(512).fill(0.1) }))
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(1))

    const error = events.find((event) => event.type === 'bounded_error')
    expect(error).toMatchObject({ code: 'unexpected_embedding_length' })
    expect(events.some((event) => event.type === 'face_embeddings_result')).toBe(false)
  })

  it('rejects a zero vector, which has no direction to normalize', async () => {
    const { worker, events } = harness(() => faceEmbedSession({ output: new Float32Array(FACE_EMBED_LENGTH) }))
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(1))
    expect(events.find((event) => event.type === 'bounded_error')).toMatchObject({ code: 'invalid_output' })
  })

  it('rejects non-finite output', async () => {
    const bad = new Float32Array(FACE_EMBED_LENGTH).fill(0.4)
    bad[3] = Number.NaN
    const { worker, events } = harness(() => faceEmbedSession({ output: bad }))
    await worker.handleMessage(loadFaceEmbed)
    await worker.handleMessage(embedCommand(1))
    expect(events.find((event) => event.type === 'bounded_error')).toMatchObject({ code: 'non_finite_embedding' })
  })

  it('refuses to embed before the model is loaded', async () => {
    const { worker, events } = harness()
    await worker.handleMessage(embedCommand(1))
    expect(events.find((event) => event.type === 'bounded_error')).toMatchObject({ code: 'not_initialised' })
  })

  it('applies the same rules to the standalone normalizer', () => {
    expect(() => normalizedFaceEmbedding(new Float32Array(64))).toThrow(VisionProtocolError)
    expect(() => normalizedFaceEmbedding(new Float32Array(FACE_EMBED_LENGTH))).toThrow(VisionProtocolError)
    expect(() => normalizedFaceEmbedding('not a vector')).toThrow(VisionProtocolError)

    const unit = normalizedFaceEmbedding(new Float32Array(FACE_EMBED_LENGTH).fill(3))
    const norm = Math.sqrt(unit.reduce((total, value) => total + value * value, 0))
    expect(norm).toBeCloseTo(1, 6)
  })
})

describe('the receiving side re-checks what the worker sent', () => {
  it('rejects an un-normalized embedding event', () => {
    // Belt and braces: even if the worker were changed to skip normalization,
    // an un-normalized vector cannot reach comparison.
    const embeddings = new Float32Array(FACE_EMBED_LENGTH).fill(0.5)
    expect(() =>
      parseVisionEvent({
        type: 'face_embeddings_result',
        requestId: 'r1',
        count: 1,
        embeddings: embeddings.buffer,
        elapsedMs: 5,
        workerRssBytes: 10
      })
    ).toThrow(VisionProtocolError)
  })

  it('accepts a properly normalized one', () => {
    const embeddings = new Float32Array(FACE_EMBED_LENGTH).fill(1 / Math.sqrt(FACE_EMBED_LENGTH))
    const event = parseVisionEvent({
      type: 'face_embeddings_result',
      requestId: 'r1',
      count: 1,
      embeddings: embeddings.buffer,
      elapsedMs: 5,
      workerRssBytes: 10
    })
    expect(event.type).toBe('face_embeddings_result')
  })

  it('rejects a width that is not 128', () => {
    expect(() =>
      parseVisionEvent({
        type: 'face_embeddings_result',
        requestId: 'r1',
        count: 1,
        embeddings: new Float32Array(256).buffer,
        elapsedMs: 5,
        workerRssBytes: 10
      })
    ).toThrow(VisionProtocolError)
  })
})

describe('the counting path cannot reach embeddings or geometry', () => {
  it('gives the counting result nowhere to put a box, a landmark, or a vector', () => {
    const scores = Float32Array.from([0.95, 0.91])
    const event = parseVisionEvent({
      type: 'face_result',
      requestId: 'r1',
      scores: scores.buffer,
      elapsedMs: 1,
      workerRssBytes: 1
    })
    expect(Object.keys(event).sort()).toEqual([
      'elapsedMs',
      'requestId',
      'scores',
      'type',
      'workerRssBytes'
    ])
  })

  it('refuses a counting result that tries to carry extra fields', () => {
    const scores = Float32Array.from([0.95])
    for (const extra of ['boxes', 'landmarks', 'embeddings', 'crops']) {
      expect(() =>
        parseVisionEvent({
          type: 'face_result',
          requestId: 'r1',
          scores: scores.buffer,
          elapsedMs: 1,
          workerRssBytes: 1,
          [extra]: new Float32Array(4).buffer
        })
      ).toThrow(VisionProtocolError)
    }
  })

  it('refuses a counting command that asks for embeddings', () => {
    for (const extra of ['embed', 'withEmbeddings', 'landmarks']) {
      expect(() =>
        parseVisionCommand({
          type: 'detect_faces',
          requestId: 'r1',
          width: 640,
          height: 640,
          format: 'bgra',
          bitmap: new ArrayBuffer(FACE_BITMAP_BYTES),
          [extra]: true
        })
      ).toThrow(VisionProtocolError)
    }
  })

  it('keeps the two collectors separate in the worker source', () => {
    // The counting collector must remain unable to return landmarks. A single
    // parameterised collector would put that one argument away.
    const source = readFileSync(join(__dirname, '..', 'vision-worker.ts'), 'utf8')
    const counting = source.slice(source.indexOf('function collectYunetOutputs'))
    const body = counting.slice(0, counting.indexOf('\n}'))
    expect(body).not.toContain('kps')
  })
})

describe('the embedding result carries no geometry', () => {
  it('gives the embedding event nowhere to put a box, landmark, or crop', () => {
    const embeddings = new Float32Array(FACE_EMBED_LENGTH).fill(1 / Math.sqrt(FACE_EMBED_LENGTH))
    for (const extra of ['boxes', 'landmarks', 'crop', 'bitmap']) {
      expect(() =>
        parseVisionEvent({
          type: 'face_embeddings_result',
          requestId: 'r1',
          count: 1,
          embeddings: embeddings.buffer,
          elapsedMs: 1,
          workerRssBytes: 1,
          [extra]: new ArrayBuffer(8)
        })
      ).toThrow(VisionProtocolError)
    }
  })
})

describe('only the verified local model is ever loaded', () => {
  it('requests CPU execution and never a remote provider', async () => {
    const { worker, created } = harness()
    await worker.handleMessage(loadFaceEmbed)
    expect(created).toHaveLength(1)
    expect((created[0]!.opts as { executionProviders: string[] }).executionProviders).toEqual(['cpu'])
  })

  it('loads from the path main supplied and makes no network request', async () => {
    // `fetch` is not reachable from the worker at all; asserting the absence of
    // a global here documents that the embedding path has no fallback.
    const { worker, created } = harness()
    await worker.handleMessage(loadFaceEmbed)
    expect(created[0]!.path).toBe('C:\\pack\\sface.onnx')
    const source = readFileSync(join(__dirname, '..', 'vision-worker.ts'), 'utf8')
    expect(source).not.toContain('fetch(')
    expect(source).not.toContain('https://')
  })

  it('refuses to load a model kind it does not define', async () => {
    const { worker, events, created } = harness()
    await worker.handleMessage({ type: 'load_model', kind: 'whatever', modelPath: 'C:\\x.onnx' })
    expect(created).toHaveLength(0)
    expect(events.find((event) => event.type === 'bounded_error')).toBeDefined()
  })
})
