/**
 * Owns the lifetime of the local vision worker.
 *
 * Responsibilities:
 *  - Nothing starts until something actually needs an embedding, so ordinary
 *    Lumi startup never loads ONNX Runtime or a CLIP model.
 *  - Exactly one inference is in flight at a time; requests queue behind it.
 *    This is what keeps indexing from saturating the machine.
 *  - The image tower is released after an idle period. The text tower stays so
 *    a spoken query does not pay a cold session load.
 *  - A crashed worker is restarted a bounded number of times; beyond that the
 *    feature degrades and filename search carries on.
 *
 * Electron is injected, so the whole lifecycle is unit-testable.
 */

import {
  boundedMessageFor,
  parseVisionEvent,
  VisionProtocolError,
  type VisionErrorCode,
  type VisionEvent,
  type VisionModelKind
} from './protocol'

export interface VisionWorkerHandle {
  postMessage: (message: unknown) => void
  onMessage: (listener: (message: unknown) => void) => void
  onExit: (listener: (code: number) => void) => void
  kill: () => void
}

export interface VisionModelPaths {
  image: string
  text: string
}

export interface EngineTimer {
  cancel: () => void
}

export interface VisionEngineDependencies {
  spawn: () => VisionWorkerHandle
  /** Resolves the verified pack's paths, or undefined when it is not installed. */
  resolveModelPaths: () => Promise<VisionModelPaths | undefined>
  now?: () => number
  schedule?: (callback: () => void, delayMs: number) => EngineTimer
  /** How long the image tower may sit unused before it is released. */
  imageIdleMs?: number
  requestTimeoutMs?: number
  loadTimeoutMs?: number
}

export const DEFAULT_IMAGE_IDLE_MS = 90_000
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
export const DEFAULT_LOAD_TIMEOUT_MS = 120_000

/** Restart budget. A worker that keeps dying is a broken install, not a blip. */
const MAX_RESTARTS = 3
const RESTART_WINDOW_MS = 5 * 60_000

export class VisionEngineError extends Error {
  constructor(readonly code: VisionErrorCode) {
    super(boundedMessageFor(code))
    this.name = 'VisionEngineError'
  }
}

interface PendingRequest {
  requestId: string
  resolve: (vector: Float32Array) => void
  reject: (error: VisionEngineError) => void
  timer: EngineTimer
}

interface PendingLoad {
  kind: VisionModelKind
  resolve: () => void
  reject: (error: VisionEngineError) => void
  timer: EngineTimer
}

/**
 * A queued operation that has not started yet. The reject handle is retained so
 * disposal can settle it, rather than dropping the closure and leaving its
 * caller awaiting a promise that never resolves.
 */
interface QueuedOperation {
  run: () => void
  reject: (error: VisionEngineError) => void
}

export class VisionEngine {
  private worker: VisionWorkerHandle | undefined
  private loaded = new Set<VisionModelKind>()
  private pendingRequest: PendingRequest | undefined
  private pendingLoad: PendingLoad | undefined
  private queue: QueuedOperation[] = []
  private busy = false
  private idleTimer: EngineTimer | undefined
  private crashes: number[] = []
  private nextRequestId = 1
  private disposed = false

  private readonly now: () => number
  private readonly schedule: (callback: () => void, delayMs: number) => EngineTimer
  private readonly imageIdleMs: number
  private readonly requestTimeoutMs: number
  private readonly loadTimeoutMs: number

  constructor(private readonly dependencies: VisionEngineDependencies) {
    this.now = dependencies.now ?? (() => Date.now())
    this.schedule = dependencies.schedule ?? defaultSchedule
    this.imageIdleMs = dependencies.imageIdleMs ?? DEFAULT_IMAGE_IDLE_MS
    this.requestTimeoutMs = dependencies.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS
    this.loadTimeoutMs = dependencies.loadTimeoutMs ?? DEFAULT_LOAD_TIMEOUT_MS
  }

  /** True once a worker exists. Used only for diagnostics and tests. */
  isRunning(): boolean {
    return this.worker !== undefined
  }

  loadedModels(): VisionModelKind[] {
    return [...this.loaded]
  }

  async embedImage(bitmap: ArrayBuffer, width: number, height: number): Promise<Float32Array> {
    return this.enqueue(async () => {
      await this.ensureModel('image')
      this.restartIdleTimer()
      return this.request((requestId) => ({
        type: 'embed_image',
        requestId,
        width,
        height,
        format: 'bgra',
        bitmap
      }))
    })
  }

  async embedText(tokenIds: Int32Array, tokenCount: number): Promise<Float32Array> {
    return this.enqueue(async () => {
      await this.ensureModel('text')
      return this.request((requestId) => ({
        type: 'embed_text',
        requestId,
        tokenIds: tokenIds.buffer.slice(
          tokenIds.byteOffset,
          tokenIds.byteOffset + tokenIds.byteLength
        ) as ArrayBuffer,
        tokenCount
      }))
    })
  }

  /** Releases the image tower now rather than waiting for the idle timer. */
  releaseImageModel(): void {
    this.idleTimer?.cancel()
    this.idleTimer = undefined
    if (this.worker && this.loaded.has('image')) {
      this.loaded.delete('image')
      this.post({ type: 'unload_model', kind: 'image' })
    }
  }

  /** Stops the worker and fails anything still waiting. Safe to call twice. */
  dispose(): void {
    if (this.disposed) {
      // Idempotent: a second call has nothing left to tear down, and its state
      // was already settled by the first.
      return
    }
    this.disposed = true
    this.idleTimer?.cancel()
    this.idleTimer = undefined
    const worker = this.worker
    // Cleared before teardown so a queued request cannot find a live handle and
    // keep talking to a worker that is on its way out.
    this.worker = undefined
    this.loaded.clear()

    // Every queued operation carries a promise its caller is awaiting. Detach
    // the queue first, then reject each one exactly once with a bounded code, so
    // disabling the feature or shutting down mid-index cannot hang a caller.
    const queued = this.queue
    this.queue = []
    for (const entry of queued) {
      entry.reject(new VisionEngineError('worker_exited'))
    }

    this.teardown('worker_exited')
    if (worker) {
      try {
        worker.postMessage({ type: 'shutdown' })
      } catch {
        // The worker may already be gone; kill() below is the backstop.
      }
      worker.kill()
    }
  }

  // --- serialization -------------------------------------------------------

  /**
   * One operation at a time. Indexing and a spoken query can both arrive at
   * once; queueing keeps a single ONNX session from being re-entered and bounds
   * peak memory to one inference.
   */
  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (this.disposed) {
        reject(new VisionEngineError('worker_exited'))
        return
      }

      const run = (): void => {
        // A dispose that lands between queueing and running settles the queue
        // itself; this guard only covers the already-dequeued case.
        if (this.disposed) {
          reject(new VisionEngineError('worker_exited'))
          return
        }
        this.busy = true
        operation()
          .then(resolve, reject)
          .finally(() => {
            this.busy = false
            if (this.disposed) {
              return
            }
            const next = this.queue.shift()
            if (next) {
              next.run()
            }
          })
      }

      if (this.busy) {
        this.queue.push({ run, reject })
      } else {
        run()
      }
    })
  }

  // --- worker lifecycle ----------------------------------------------------

  private ensureWorker(): VisionWorkerHandle {
    if (this.disposed) {
      throw new VisionEngineError('worker_exited')
    }
    if (this.worker) {
      return this.worker
    }

    // A worker that has already died is only replaced while the crash budget
    // holds; past that this is a broken install, not a transient fault, and
    // retrying forever would just burn CPU behind the user's back.
    if (this.crashes.length > 0 && !this.withinRestartBudget()) {
      throw new VisionEngineError('worker_start_failed')
    }

    let worker: VisionWorkerHandle
    try {
      worker = this.dependencies.spawn()
    } catch {
      throw new VisionEngineError('worker_start_failed')
    }

    // Bind both listeners to this specific worker instance. A message or exit
    // from a worker we have already replaced or disposed must not touch current
    // state — otherwise a late, id-less bounded_error could reject a newer
    // request, or a stale exit could miscount the restart budget.
    worker.onMessage((message) => {
      if (this.worker === worker) {
        this.handleEvent(message)
      }
    })
    worker.onExit(() => {
      if (this.worker === worker) {
        this.handleExit()
      }
    })
    this.worker = worker
    this.loaded.clear()
    return worker
  }

  private handleExit(): void {
    this.worker = undefined
    this.loaded.clear()
    this.idleTimer?.cancel()
    this.idleTimer = undefined
    if (!this.disposed) {
      this.crashes.push(this.now())
    }
    this.teardown('worker_exited')
  }

  private teardown(code: VisionErrorCode): void {
    const request = this.pendingRequest
    this.pendingRequest = undefined
    request?.timer.cancel()
    request?.reject(new VisionEngineError(code))

    const load = this.pendingLoad
    this.pendingLoad = undefined
    load?.timer.cancel()
    load?.reject(new VisionEngineError(code))
  }

  /** Crashes inside the rolling window; beyond the budget we stop respawning. */
  private withinRestartBudget(): boolean {
    const cutoff = this.now() - RESTART_WINDOW_MS
    this.crashes = this.crashes.filter((at) => at > cutoff)
    return this.crashes.length <= MAX_RESTARTS
  }

  private async ensureModel(kind: VisionModelKind): Promise<void> {
    if (this.worker && this.loaded.has(kind)) {
      return
    }

    const paths = await this.dependencies.resolveModelPaths()
    if (!paths) {
      throw new VisionEngineError('model_missing')
    }

    this.ensureWorker()

    await new Promise<void>((resolve, reject) => {
      const timer = this.schedule(() => {
        if (this.pendingLoad?.kind === kind) {
          this.pendingLoad = undefined
          reject(new VisionEngineError('worker_timeout'))
        }
      }, this.loadTimeoutMs)

      this.pendingLoad = { kind, resolve, reject, timer }
      try {
        this.post({ type: 'load_model', kind, modelPath: kind === 'image' ? paths.image : paths.text })
      } catch (error) {
        timer.cancel()
        this.pendingLoad = undefined
        reject(asEngineError(error, 'worker_start_failed'))
      }
    })
  }

  private request(build: (requestId: string) => Record<string, unknown>): Promise<Float32Array> {
    return new Promise<Float32Array>((resolve, reject) => {
      const requestId = `r${this.nextRequestId++}`
      const timer = this.schedule(() => {
        if (this.pendingRequest?.requestId === requestId) {
          this.pendingRequest = undefined
          reject(new VisionEngineError('worker_timeout'))
        }
      }, this.requestTimeoutMs)

      this.pendingRequest = { requestId, resolve, reject, timer }
      try {
        this.post(build(requestId))
      } catch (error) {
        timer.cancel()
        this.pendingRequest = undefined
        reject(asEngineError(error, 'worker_exited'))
      }
    })
  }

  private post(message: Record<string, unknown>): void {
    const worker = this.worker ?? this.ensureWorker()
    worker.postMessage(message)
  }

  private restartIdleTimer(): void {
    this.idleTimer?.cancel()
    this.idleTimer = this.schedule(() => {
      this.idleTimer = undefined
      if (this.disposed) {
        return
      }
      // Releasing is only safe while nothing is running. If work is in flight,
      // re-arm rather than skip, so an image tower followed only by text traffic
      // is still eventually released instead of staying resident until exit.
      if (this.busy) {
        this.restartIdleTimer()
        return
      }
      this.releaseImageModel()
    }, this.imageIdleMs)
  }

  // --- events --------------------------------------------------------------

  private handleEvent(raw: unknown): void {
    let event: VisionEvent
    try {
      event = parseVisionEvent(raw)
    } catch (error) {
      // Keep the parser's specific reason rather than flattening it.
      const code = error instanceof VisionProtocolError ? error.code : 'invalid_message'
      this.failPending(code)
      return
    }

    switch (event.type) {
      case 'ready':
        return
      case 'model_loaded': {
        const load = this.pendingLoad
        if (load?.kind !== event.kind) {
          return
        }
        this.pendingLoad = undefined
        load.timer.cancel()
        this.loaded.add(event.kind)
        load.resolve()
        return
      }
      case 'model_unloaded':
        this.loaded.delete(event.kind)
        return
      case 'embedding_result': {
        const request = this.pendingRequest
        if (request?.requestId !== event.requestId) {
          // A late answer to a timed-out request. Dropping it is correct.
          return
        }
        this.pendingRequest = undefined
        request.timer.cancel()
        request.resolve(new Float32Array(event.vector))
        return
      }
      case 'bounded_error':
        this.failPending(event.code, event.requestId)
    }
  }

  private failPending(code: VisionErrorCode, requestId?: string): void {
    const request = this.pendingRequest
    if (request && (requestId === undefined || request.requestId === requestId)) {
      this.pendingRequest = undefined
      request.timer.cancel()
      request.reject(new VisionEngineError(code))
      return
    }

    const load = this.pendingLoad
    if (load) {
      this.pendingLoad = undefined
      load.timer.cancel()
      load.reject(new VisionEngineError(code))
    }
  }
}

function asEngineError(error: unknown, fallback: VisionErrorCode): VisionEngineError {
  if (error instanceof VisionEngineError) {
    return error
  }
  return new VisionEngineError(error instanceof VisionProtocolError ? error.code : fallback)
}

function defaultSchedule(callback: () => void, delayMs: number): EngineTimer {
  const timer = setTimeout(callback, delayMs)
  timer.unref?.()
  return { cancel: () => clearTimeout(timer) }
}
