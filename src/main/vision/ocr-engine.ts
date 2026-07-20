/**
 * Owns the lifetime of the local OCR engine.
 *
 * Mirrors `engine.ts` in shape and in guarantees, but stays a separate object
 * because it wraps a different runtime. Tesseract runs in its own worker with
 * its own WASM heap; putting it behind the ONNX worker's single-inference queue
 * would mean a slow page of text blocking an interactive search embedding,
 * which is the opposite of the priority this phase calls for.
 *
 * What this class is responsible for:
 *
 *  - **Nothing loads until an image needs reading.** Ordinary startup, and a
 *    Lumi that never enables text search, pay nothing.
 *  - **Exactly one recognition at a time**, so OCR cannot saturate the machine.
 *  - **The worker is terminated when idle**, not merely left parked. Tesseract's
 *    heap is tens of megabytes and this is what keeps it from sitting resident
 *    alongside the CLIP image tower.
 *  - **Every job is time-bounded and cancellable.** A pathological image must
 *    not be able to wedge indexing, and revoking a folder must stop work now.
 *  - **Offline, always.** The engine is handed a local directory holding
 *    verified training data and is never permitted to fetch. If that file is
 *    missing the job fails closed rather than reaching for a CDN.
 *
 * Recognized text is returned to the caller and otherwise goes nowhere. It is
 * never logged, never included in an error, and never crosses into Realtime.
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import type { OcrFailureCode } from './index-store'
import { prepareOcrText } from '../../shared/ocr-text'

/** The filename Tesseract resolves for English. Fixed by the engine's convention. */
export const OCR_LANGUAGE = 'eng'
export const OCR_TRAINED_DATA_FILE = `${OCR_LANGUAGE}.traineddata`

/**
 * A page of dense text is the legitimate worst case. Beyond this the image is
 * either pathological or not the kind of thing this feature serves, and the
 * budget is better spent on the next photo.
 */
export const DEFAULT_OCR_TIMEOUT_MS = 25_000

/** How long the worker may sit unused before its heap is handed back. */
export const DEFAULT_OCR_IDLE_MS = 30_000

export class OcrEngineError extends Error {
  constructor(readonly code: OcrFailureCode) {
    super(OCR_ERROR_MESSAGES[code])
    this.name = 'OcrEngineError'
  }
}

/** App-authored, path-free, and free of any recognized text. */
const OCR_ERROR_MESSAGES: Record<OcrFailureCode, string> = {
  decode_failed: 'That image could not be prepared for reading.',
  unsupported_format: 'That image is not in a format Lumi can read text from.',
  too_many_pixels: 'That image is too large to read text from.',
  file_locked: 'That image was in use by another program.',
  ocr_failed: 'Lumi could not read text from that image.',
  ocr_timeout: 'Reading text from that image took too long and was stopped.',
  ocr_unavailable: 'Local text reading is not available yet.'
}

/** The narrow slice of the Tesseract worker this module depends on. */
export interface OcrWorkerHandle {
  recognize: (image: Buffer) => Promise<{ text: string }>
  terminate: () => Promise<void>
}

export interface OcrTimer {
  cancel: () => void
}

export interface LocalOcrEngineDependencies {
  /** Directory holding the verified `eng.traineddata`. Main-owned; never from a caller. */
  languageDirectory: string
  /** Injected so tests never load a real WASM engine. */
  createWorker?: (languageDirectory: string) => Promise<OcrWorkerHandle>
  now?: () => number
  schedule?: (callback: () => void, delayMs: number) => OcrTimer
  timeoutMs?: number
  idleMs?: number
  fileExists?: (path: string) => boolean
}

export interface OcrResult {
  text: string
  tokens: string[]
}

export class LocalOcrEngine {
  private worker: OcrWorkerHandle | undefined
  private starting: Promise<OcrWorkerHandle> | undefined
  private queue: Promise<unknown> = Promise.resolve()
  private idleTimer: OcrTimer | undefined
  private disposed = false

  private readonly schedule: (callback: () => void, delayMs: number) => OcrTimer
  private readonly timeoutMs: number
  private readonly idleMs: number
  private readonly fileExists: (path: string) => boolean

  constructor(private readonly dependencies: LocalOcrEngineDependencies) {
    this.schedule = dependencies.schedule ?? defaultSchedule
    this.timeoutMs = dependencies.timeoutMs ?? DEFAULT_OCR_TIMEOUT_MS
    this.idleMs = dependencies.idleMs ?? DEFAULT_OCR_IDLE_MS
    this.fileExists = dependencies.fileExists ?? existsSync
  }

  isRunning(): boolean {
    return this.worker !== undefined
  }

  /**
   * Reads one image. Serialized against every other call on this engine, so the
   * caller does not have to coordinate.
   *
   * `signal` is checked before the job starts and again after it finishes: a
   * revoked root or a cancelled index must not commit a result computed from a
   * file it no longer has authority over.
   */
  async recognize(png: Buffer, signal?: AbortSignal): Promise<OcrResult> {
    return this.enqueue(async () => {
      this.assertLive(signal)

      const worker = await this.ensureWorker()
      this.assertLive(signal)

      let raw: string
      try {
        raw = await this.withTimeout(worker.recognize(png), signal)
      } catch (error) {
        // A worker that failed or timed out may be in an unusable state, and its
        // heap is the thing we most want back. Replace it rather than reuse it.
        await this.discardWorker()
        throw error instanceof OcrEngineError ? error : new OcrEngineError('ocr_failed')
      }

      this.assertLive(signal)
      this.restartIdleTimer()

      // Normalization happens here so no caller can store un-normalized text.
      return prepareOcrText(raw)
    })
  }

  /** Releases the worker heap now rather than waiting for the idle timer. */
  async release(): Promise<void> {
    this.idleTimer?.cancel()
    this.idleTimer = undefined
    await this.discardWorker()
  }

  async dispose(): Promise<void> {
    this.disposed = true
    await this.release()
  }

  // --- serialization -------------------------------------------------------

  /** One recognition at a time, regardless of what the caller promises. */
  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.then(operation, operation)
    this.queue = result.then(
      () => undefined,
      () => undefined
    )
    return result
  }

  // --- worker lifecycle ----------------------------------------------------

  private async ensureWorker(): Promise<OcrWorkerHandle> {
    if (this.worker) {
      return this.worker
    }
    // A concurrent start would create two WASM heaps; the first caller owns it.
    this.starting ??= this.startWorker()
    try {
      this.worker = await this.starting
      return this.worker
    } finally {
      this.starting = undefined
    }
  }

  private async startWorker(): Promise<OcrWorkerHandle> {
    // Fail closed. Without this check the engine would treat a missing language
    // file as a cue to fetch one, which would be an unverified model download
    // over the network — exactly what the frozen manifest exists to prevent.
    if (!this.fileExists(join(this.dependencies.languageDirectory, OCR_TRAINED_DATA_FILE))) {
      throw new OcrEngineError('ocr_unavailable')
    }

    const create = this.dependencies.createWorker ?? createTesseractWorker
    try {
      return await create(this.dependencies.languageDirectory)
    } catch {
      throw new OcrEngineError('ocr_unavailable')
    }
  }

  private async discardWorker(): Promise<void> {
    const worker = this.worker
    this.worker = undefined
    if (worker) {
      await worker.terminate().catch(() => undefined)
    }
  }

  private restartIdleTimer(): void {
    this.idleTimer?.cancel()
    if (this.disposed) {
      return
    }
    this.idleTimer = this.schedule(() => {
      this.idleTimer = undefined
      void this.release()
    }, this.idleMs)
  }

  private withTimeout(work: Promise<{ text: string }>, signal: AbortSignal | undefined): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      let settled = false
      const timer = this.schedule(() => {
        if (settled) {
          return
        }
        settled = true
        signal?.removeEventListener('abort', onAbort)
        reject(new OcrEngineError('ocr_timeout'))
      }, this.timeoutMs)

      function onAbort(): void {
        if (settled) {
          return
        }
        settled = true
        timer.cancel()
        reject(new OcrEngineError('ocr_failed'))
      }

      signal?.addEventListener('abort', onAbort, { once: true })

      work.then(
        (value) => {
          if (settled) {
            return
          }
          settled = true
          timer.cancel()
          signal?.removeEventListener('abort', onAbort)
          resolve(typeof value?.text === 'string' ? value.text : '')
        },
        () => {
          if (settled) {
            return
          }
          settled = true
          timer.cancel()
          signal?.removeEventListener('abort', onAbort)
          // The underlying error is deliberately discarded rather than wrapped:
          // engine exception text can carry native paths and, in principle,
          // fragments of the page it was reading.
          reject(new OcrEngineError('ocr_failed'))
        }
      )
    })
  }

  private assertLive(signal: AbortSignal | undefined): void {
    if (this.disposed || signal?.aborted) {
      throw new OcrEngineError('ocr_failed')
    }
  }
}

function defaultSchedule(callback: () => void, delayMs: number): OcrTimer {
  const timer = setTimeout(callback, delayMs)
  timer.unref?.()
  return { cancel: () => clearTimeout(timer) }
}

/**
 * The production worker.
 *
 * Every option here exists to keep the engine local and quiet:
 *  - `langPath` is the verified pack directory, so the training data it loads
 *    is the exact file whose digest this application checked.
 *  - `gzip: false` because the manifest pins the uncompressed file.
 *  - `cacheMethod: 'none'` so it neither reads nor writes a cache of its own;
 *    the pack directory is the single source.
 *  - the logger is omitted entirely, so per-page progress — which includes
 *    recognized text — never reaches a console or a log file.
 */
async function createTesseractWorker(languageDirectory: string): Promise<OcrWorkerHandle> {
  const { createWorker } = await import('tesseract.js')
  const worker = await createWorker(OCR_LANGUAGE, 1, {
    langPath: languageDirectory,
    cachePath: languageDirectory,
    gzip: false,
    cacheMethod: 'none'
  })

  return {
    recognize: async (image: Buffer) => {
      const { data } = await worker.recognize(image)
      return { text: data.text }
    },
    terminate: async () => {
      await worker.terminate()
    }
  }
}
