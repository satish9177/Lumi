import { beforeEach, describe, expect, it } from 'vitest'
import {
  VisionEngine,
  VisionEngineError,
  type EngineTimer,
  type VisionWorkerHandle
} from './engine'
import { CLIP_CONTEXT_LENGTH, CLIP_EMBEDDING_LENGTH, type VisionModelKind } from './protocol'

/** A worker stand-in that records commands and lets a test drive the replies. */
class FakeWorker implements VisionWorkerHandle {
  readonly sent: Array<Record<string, unknown>> = []
  killed = false
  private messageListener: ((message: unknown) => void) | undefined
  private exitListener: ((code: number) => void) | undefined
  /** When set, the worker answers load and embed commands automatically. */
  autoRespond = true

  postMessage(message: unknown): void {
    const command = message as Record<string, unknown>
    this.sent.push(command)
    if (!this.autoRespond) {
      return
    }

    if (command.type === 'load_model') {
      this.emit({ type: 'model_loaded', kind: command.kind, sessionLoadMs: 5 })
    } else if (command.type === 'unload_model') {
      this.emit({ type: 'model_unloaded', kind: command.kind })
    } else if (command.type === 'embed_image' || command.type === 'embed_text') {
      this.emitEmbedding(String(command.requestId), command.type === 'embed_image' ? 'image' : 'text')
    }
  }

  onMessage(listener: (message: unknown) => void): void {
    this.messageListener = listener
  }

  onExit(listener: (code: number) => void): void {
    this.exitListener = listener
  }

  kill(): void {
    this.killed = true
  }

  emit(event: Record<string, unknown>): void {
    this.messageListener?.(event)
  }

  emitEmbedding(requestId: string, kind: VisionModelKind, fill = 0.5): void {
    this.emit({
      type: 'embedding_result',
      requestId,
      kind,
      vector: new Float32Array(CLIP_EMBEDDING_LENGTH).fill(fill).buffer,
      elapsedMs: 3,
      workerRssBytes: 1_000
    })
  }

  exit(code = 1): void {
    this.exitListener?.(code)
  }

  commandTypes(): string[] {
    return this.sent.map((command) => String(command.type))
  }
}

/** Timers a test advances by hand, so idle behaviour is deterministic. */
class ManualClock {
  private pending: Array<{ id: number; at: number; callback: () => void }> = []
  private nextId = 1
  current = 0

  now = (): number => this.current

  schedule = (callback: () => void, delayMs: number): EngineTimer => {
    const id = this.nextId++
    this.pending.push({ id, at: this.current + delayMs, callback })
    return { cancel: () => (this.pending = this.pending.filter((entry) => entry.id !== id)) }
  }

  advance(ms: number): void {
    this.current += ms
    const due = this.pending.filter((entry) => entry.at <= this.current)
    this.pending = this.pending.filter((entry) => entry.at > this.current)
    for (const entry of due) {
      entry.callback()
    }
  }
}

const PATHS = { image: 'C:\\pack\\vision.onnx', text: 'C:\\pack\\text.onnx' }

function makeEngine(overrides: Partial<Parameters<typeof buildDependencies>[0]> = {}) {
  const clock = new ManualClock()
  let worker = new FakeWorker()
  const spawned: FakeWorker[] = [worker]
  const options = buildDependencies({ paths: PATHS, ...overrides })

  const engine = new VisionEngine({
    spawn: () => {
      if (options.spawnThrows) {
        throw new Error('spawn failed')
      }
      worker = new FakeWorker()
      spawned.push(worker)
      return worker
    },
    resolveModelPaths: async () => options.paths,
    now: clock.now,
    schedule: clock.schedule,
    imageIdleMs: 1_000,
    requestTimeoutMs: 5_000,
    loadTimeoutMs: 5_000
  })

  // The first spawn happens lazily; drop the placeholder.
  spawned.shift()
  return { engine, clock, spawned, current: () => spawned[spawned.length - 1]! }
}

function buildDependencies(options: {
  paths?: { image: string; text: string }
  spawnThrows?: boolean
}): { paths: { image: string; text: string } | undefined; spawnThrows: boolean } {
  return { paths: options.paths, spawnThrows: options.spawnThrows ?? false }
}

function bitmap(): ArrayBuffer {
  return new ArrayBuffer(224 * 224 * 4)
}

function tokenIds(): Int32Array {
  return new Int32Array(CLIP_CONTEXT_LENGTH)
}

describe('VisionEngine lifecycle', () => {
  let harness: ReturnType<typeof makeEngine>

  beforeEach(() => {
    harness = makeEngine()
  })

  it('starts nothing until an embedding is actually requested', () => {
    expect(harness.engine.isRunning()).toBe(false)
    expect(harness.spawned).toHaveLength(0)
  })

  it('spawns a worker and loads the image tower on the first image request', async () => {
    const vector = await harness.engine.embedImage(bitmap(), 224, 224)

    expect(vector).toHaveLength(CLIP_EMBEDDING_LENGTH)
    expect(harness.engine.isRunning()).toBe(true)
    expect(harness.current().commandTypes()).toEqual(['load_model', 'embed_image'])
    expect(harness.current().sent[0]).toMatchObject({ kind: 'image', modelPath: PATHS.image })
  })

  it('loads each tower once and reuses it', async () => {
    await harness.engine.embedImage(bitmap(), 224, 224)
    await harness.engine.embedImage(bitmap(), 224, 224)

    expect(harness.current().commandTypes()).toEqual(['load_model', 'embed_image', 'embed_image'])
  })

  it('keeps the image and text towers independent', async () => {
    await harness.engine.embedImage(bitmap(), 224, 224)
    await harness.engine.embedText(tokenIds(), 3)

    expect(harness.engine.loadedModels().sort()).toEqual(['image', 'text'])
    const loads = harness.current().sent.filter((command) => command.type === 'load_model')
    expect(loads.map((command) => command.kind)).toEqual(['image', 'text'])
  })

  it('sends the model path only on load, never with an embedding request', async () => {
    await harness.engine.embedImage(bitmap(), 224, 224)

    const embed = harness.current().sent.find((command) => command.type === 'embed_image')!
    expect(Object.keys(embed).sort()).toEqual(['bitmap', 'format', 'height', 'requestId', 'type', 'width'])
    expect(JSON.stringify(Object.keys(embed))).not.toContain('Path')
  })

  it('releases the image tower after the idle period but keeps the text tower', async () => {
    await harness.engine.embedImage(bitmap(), 224, 224)
    await harness.engine.embedText(tokenIds(), 3)
    expect(harness.engine.loadedModels().sort()).toEqual(['image', 'text'])

    harness.clock.advance(1_001)

    expect(harness.engine.loadedModels()).toEqual(['text'])
    expect(harness.current().commandTypes()).toContain('unload_model')
  })

  it('reloads the image tower after an idle release', async () => {
    await harness.engine.embedImage(bitmap(), 224, 224)
    harness.clock.advance(1_001)
    await harness.engine.embedImage(bitmap(), 224, 224)

    const loads = harness.current().sent.filter((command) => command.type === 'load_model' && command.kind === 'image')
    expect(loads).toHaveLength(2)
  })

  it('runs one inference at a time', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      imageIdleMs: 10_000,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const first = engine.embedImage(bitmap(), 224, 224)
    const second = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()

    // The second request must not reach the worker while the first is in flight.
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    expect(worker.sent.filter((command) => command.type === 'embed_image')).toHaveLength(1)

    worker.emitEmbedding('r1', 'image')
    await first
    await Promise.resolve()
    await Promise.resolve()
    expect(worker.sent.filter((command) => command.type === 'embed_image')).toHaveLength(2)

    worker.emitEmbedding('r2', 'image')
    await expect(second).resolves.toHaveLength(CLIP_EMBEDDING_LENGTH)
  })
})

describe('VisionEngine failure handling', () => {
  it('reports a bounded error when the pack is not installed', async () => {
    const { engine } = makeEngine({ paths: undefined })

    await expect(engine.embedImage(bitmap(), 224, 224)).rejects.toThrow(
      expect.objectContaining({ code: 'model_missing' })
    )
  })

  it('reports a bounded error when the worker cannot be spawned', async () => {
    const { engine } = makeEngine({ spawnThrows: true })

    await expect(engine.embedImage(bitmap(), 224, 224)).rejects.toThrow(
      expect.objectContaining({ code: 'worker_start_failed' })
    )
  })

  it('surfaces the worker\'s own bounded code rather than a generic failure', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.emit({ type: 'bounded_error', code: 'model_load_failed' })

    await expect(pending).rejects.toThrow(expect.objectContaining({ code: 'model_load_failed' }))
  })

  it('fails the in-flight request when the worker exits', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.exit(3)

    await expect(pending).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))
    expect(engine.isRunning()).toBe(false)
  })

  it('restarts after a crash and completes the next request', async () => {
    const workers: FakeWorker[] = []
    const engine = new VisionEngine({
      spawn: () => {
        const worker = new FakeWorker()
        worker.autoRespond = workers.length > 0
        workers.push(worker)
        return worker
      },
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const doomed = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    workers[0]!.exit(1)
    await expect(doomed).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))

    await expect(engine.embedImage(bitmap(), 224, 224)).resolves.toHaveLength(CLIP_EMBEDDING_LENGTH)
    expect(workers).toHaveLength(2)
  })

  it('stops restarting a worker that keeps dying', async () => {
    const workers: FakeWorker[] = []
    const engine = new VisionEngine({
      spawn: () => {
        const worker = new FakeWorker()
        worker.autoRespond = false
        workers.push(worker)
        return worker
      },
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    for (let attempt = 0; attempt < 5; attempt += 1) {
      const pending = engine.embedImage(bitmap(), 224, 224)
      await Promise.resolve()
      workers[workers.length - 1]?.exit(1)
      await pending.catch(() => undefined)
    }

    // The restart budget stops the loop well before five fresh workers.
    expect(workers.length).toBeLessThanOrEqual(4)
    await expect(engine.embedImage(bitmap(), 224, 224)).rejects.toThrow(VisionEngineError)
  })

  it('times out a request the worker never answers', async () => {
    const clock = new ManualClock()
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      now: clock.now,
      schedule: clock.schedule,
      requestTimeoutMs: 1_000,
      loadTimeoutMs: 1_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    clock.advance(1_001)

    await expect(pending).rejects.toThrow(expect.objectContaining({ code: 'worker_timeout' }))
  })

  it('ignores an answer whose request id does not match', async () => {
    const clock = new ManualClock()
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      now: clock.now,
      schedule: clock.schedule,
      requestTimeoutMs: 1_000,
      loadTimeoutMs: 1_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    worker.emitEmbedding('some-other-request', 'image')
    clock.advance(1_001)

    await expect(pending).rejects.toThrow(expect.objectContaining({ code: 'worker_timeout' }))
  })

  it('rejects a malformed event with the parser\'s specific reason', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    worker.emit({
      type: 'embedding_result',
      requestId: 'r1',
      kind: 'image',
      vector: new Float32Array(7).buffer,
      elapsedMs: 1,
      workerRssBytes: 1
    })

    await expect(pending).rejects.toThrow(expect.objectContaining({ code: 'unexpected_embedding_length' }))
  })

  it('shuts the worker down and refuses further work', async () => {
    const { engine, current } = makeEngine()
    await engine.embedImage(bitmap(), 224, 224)

    engine.dispose()

    expect(current().killed).toBe(true)
    expect(current().commandTypes()).toContain('shutdown')
    await expect(engine.embedImage(bitmap(), 224, 224)).rejects.toThrow(VisionEngineError)
  })

  it('ignores a bounded error from a worker it has already replaced', async () => {
    const workers: FakeWorker[] = []
    const engine = new VisionEngine({
      spawn: () => {
        const worker = new FakeWorker()
        worker.autoRespond = false
        workers.push(worker)
        return worker
      },
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 10_000,
      loadTimeoutMs: 10_000
    })

    const first = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    workers[0]!.exit(1)
    await expect(first).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))

    const second = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    // A late, id-less error from the dead first worker must not touch the new
    // request on the replacement worker.
    workers[0]!.emit({ type: 'bounded_error', code: 'model_load_failed' })
    workers[1]!.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    // The first request died at load without ever reaching the request stage, so
    // the id counter is still at its first value.
    workers[1]!.emitEmbedding('r1', 'image')

    await expect(second).resolves.toHaveLength(CLIP_EMBEDDING_LENGTH)
  })
})

describe('VisionEngine disposal', () => {
  it('rejects the active request and every queued operation without executing them', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 100_000,
      loadTimeoutMs: 100_000
    })

    const active = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    // Let the active request load and post its embed, so it is genuinely the one
    // in flight while the others sit behind it in the queue.
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    const queuedImage = engine.embedImage(bitmap(), 224, 224)
    const queuedText = engine.embedText(tokenIds(), 3)
    await Promise.resolve()

    engine.dispose()

    await expect(active).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))
    await expect(queuedImage).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))
    await expect(queuedText).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))

    // Only the active request ever reached the worker; the queued ones never ran.
    const embedCommands = worker.sent.filter(
      (command) => command.type === 'embed_image' || command.type === 'embed_text'
    )
    expect(embedCommands).toHaveLength(1)
    expect(worker.killed).toBe(true)
  })

  it('rejects a request issued after disposal', async () => {
    const { engine } = makeEngine()
    await engine.embedImage(bitmap(), 224, 224)
    engine.dispose()

    await expect(engine.embedImage(bitmap(), 224, 224)).rejects.toThrow(
      expect.objectContaining({ code: 'worker_exited' })
    )
    await expect(engine.embedText(tokenIds(), 3)).rejects.toThrow(VisionEngineError)
  })

  it('is safe to dispose more than once', async () => {
    const { engine, current } = makeEngine()
    await engine.embedImage(bitmap(), 224, 224)

    engine.dispose()
    expect(() => engine.dispose()).not.toThrow()
    expect(current().killed).toBe(true)
  })

  it('does not resolve a disposed request even if a late result arrives', async () => {
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      requestTimeoutMs: 100_000,
      loadTimeoutMs: 100_000
    })

    const active = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()

    engine.dispose()
    // A late answer from the now-killed worker must be dropped, not used to
    // settle the request a second time.
    worker.emitEmbedding('r1', 'image')

    await expect(active).rejects.toThrow(expect.objectContaining({ code: 'worker_exited' }))
  })

  it('re-arms the idle release when the deadline lands mid-inference', async () => {
    const clock = new ManualClock()
    const worker = new FakeWorker()
    worker.autoRespond = false
    const engine = new VisionEngine({
      spawn: () => worker,
      resolveModelPaths: async () => PATHS,
      now: clock.now,
      schedule: clock.schedule,
      imageIdleMs: 1_000,
      requestTimeoutMs: 100_000,
      loadTimeoutMs: 100_000
    })

    const pending = engine.embedImage(bitmap(), 224, 224)
    await Promise.resolve()
    worker.emit({ type: 'model_loaded', kind: 'image', sessionLoadMs: 1 })
    await Promise.resolve()
    await Promise.resolve()
    expect(engine.loadedModels()).toEqual(['image'])

    // The idle deadline passes while the embed is still in flight: the tower must
    // stay loaded and the timer must re-arm rather than silently give up.
    clock.advance(1_001)
    expect(engine.loadedModels()).toEqual(['image'])

    worker.emitEmbedding('r1', 'image')
    await pending
    clock.advance(1_001)

    expect(engine.loadedModels()).toEqual([])
    expect(worker.commandTypes()).toContain('unload_model')
  })
})
