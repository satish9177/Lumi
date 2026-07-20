import { describe, expect, it, vi } from 'vitest'
import {
  LocalOcrEngine,
  OcrEngineError,
  OCR_TRAINED_DATA_FILE,
  type OcrWorkerHandle
} from './ocr-engine'

interface Harness {
  engine: LocalOcrEngine
  created: number
  terminated: number
  running: () => boolean
  fire: (afterMs: number) => void
}

/**
 * A fake worker plus a controllable clock. No WASM, no filesystem, no network:
 * every lifecycle branch is exercised in-process.
 */
function harness(options: {
  recognize?: (image: Buffer) => Promise<{ text: string }>
  exists?: boolean
  timeoutMs?: number
  idleMs?: number
  createWorker?: () => Promise<OcrWorkerHandle>
} = {}): Harness {
  const timers: { at: number; callback: () => void; cancelled: boolean }[] = []
  let clock = 0
  const state = { created: 0, terminated: 0, live: false }

  const engine = new LocalOcrEngine({
    languageDirectory: 'C:\\packs\\extras',
    fileExists: () => options.exists ?? true,
    timeoutMs: options.timeoutMs ?? 1_000,
    idleMs: options.idleMs ?? 500,
    schedule: (callback, delayMs) => {
      const entry = { at: clock + delayMs, callback, cancelled: false }
      timers.push(entry)
      return { cancel: () => { entry.cancelled = true } }
    },
    createWorker:
      options.createWorker ??
      (async () => {
        state.created += 1
        state.live = true
        return {
          recognize: options.recognize ?? (async () => ({ text: 'hello world' })),
          terminate: async () => {
            state.terminated += 1
            state.live = false
          }
        }
      })
  })

  return {
    engine,
    get created() { return state.created },
    get terminated() { return state.terminated },
    running: () => state.live,
    fire: (afterMs: number) => {
      clock += afterMs
      for (const entry of [...timers]) {
        if (!entry.cancelled && entry.at <= clock) {
          entry.cancelled = true
          entry.callback()
        }
      }
    }
  } as Harness
}

const IMAGE = Buffer.from([0x89, 0x50, 0x4e, 0x47])

/**
 * Lets the engine's internal queue and worker start settle so its timers are
 * actually scheduled before the fake clock is advanced.
 */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) {
    await Promise.resolve()
  }
}

describe('nothing loads until an image needs reading', () => {
  it('starts no worker on construction', () => {
    const h = harness()
    expect(h.engine.isRunning()).toBe(false)
    expect(h.created).toBe(0)
  })

  it('starts exactly one worker on first use and reuses it', async () => {
    const h = harness()
    await h.engine.recognize(IMAGE)
    await h.engine.recognize(IMAGE)
    expect(h.created).toBe(1)
  })

  it('does not create a second heap when two calls race', async () => {
    const h = harness()
    await Promise.all([h.engine.recognize(IMAGE), h.engine.recognize(IMAGE), h.engine.recognize(IMAGE)])
    expect(h.created).toBe(1)
  })
})

describe('one recognition at a time', () => {
  it('serializes overlapping calls', async () => {
    let concurrent = 0
    let peak = 0
    const h = harness({
      recognize: async () => {
        concurrent += 1
        peak = Math.max(peak, concurrent)
        await new Promise((resolve) => setTimeout(resolve, 5))
        concurrent -= 1
        return { text: 'ok' }
      }
    })

    await Promise.all([1, 2, 3, 4].map(() => h.engine.recognize(IMAGE)))
    expect(peak).toBe(1)
  })

  it('keeps serving later jobs after one fails', async () => {
    let call = 0
    const h = harness({
      recognize: async () => {
        call += 1
        if (call === 1) throw new Error('native failure')
        return { text: 'second' }
      }
    })

    await expect(h.engine.recognize(IMAGE)).rejects.toBeInstanceOf(OcrEngineError)
    await expect(h.engine.recognize(IMAGE)).resolves.toMatchObject({ text: 'second' })
  })
})

describe('the worker heap is handed back', () => {
  it('terminates the worker after the idle period', async () => {
    const h = harness({ idleMs: 500 })
    await h.engine.recognize(IMAGE)
    expect(h.running()).toBe(true)

    h.fire(500)
    await Promise.resolve()
    await Promise.resolve()
    expect(h.terminated).toBe(1)
  })

  it('releases on demand, so it never sits resident beside the CLIP tower', async () => {
    const h = harness()
    await h.engine.recognize(IMAGE)
    await h.engine.release()
    expect(h.engine.isRunning()).toBe(false)
    expect(h.terminated).toBe(1)
  })

  it('replaces rather than reuses a worker that failed', async () => {
    const h = harness({
      recognize: async () => {
        throw new Error('boom')
      }
    })
    await expect(h.engine.recognize(IMAGE)).rejects.toBeInstanceOf(OcrEngineError)
    expect(h.terminated).toBe(1)
  })

  it('is safe to dispose twice', async () => {
    const h = harness()
    await h.engine.recognize(IMAGE)
    await h.engine.dispose()
    await h.engine.dispose()
    expect(h.terminated).toBe(1)
  })
})

describe('every job is bounded and cancellable', () => {
  it('stops a job that runs past its budget', async () => {
    const h = harness({ timeoutMs: 100, recognize: () => new Promise(() => {}) })
    const pending = h.engine.recognize(IMAGE)
    await flush()
    h.fire(100)
    await expect(pending).rejects.toMatchObject({ code: 'ocr_timeout' })
  })

  it('discards a worker that timed out rather than trusting its state', async () => {
    const h = harness({ timeoutMs: 100, recognize: () => new Promise(() => {}) })
    const pending = h.engine.recognize(IMAGE)
    await flush()
    h.fire(100)
    await pending.catch(() => undefined)
    expect(h.terminated).toBe(1)
  })

  it('refuses to start once the signal is already aborted', async () => {
    const h = harness()
    const controller = new AbortController()
    controller.abort()
    await expect(h.engine.recognize(IMAGE, controller.signal)).rejects.toBeInstanceOf(OcrEngineError)
    // Revocation must not even spin up an engine.
    expect(h.created).toBe(0)
  })

  it('abandons a job in flight when the signal aborts', async () => {
    const h = harness({ recognize: () => new Promise(() => {}) })
    const controller = new AbortController()
    const pending = h.engine.recognize(IMAGE, controller.signal)
    controller.abort()
    await expect(pending).rejects.toBeInstanceOf(OcrEngineError)
  })

  it('rejects every call after disposal', async () => {
    const h = harness()
    await h.engine.dispose()
    await expect(h.engine.recognize(IMAGE)).rejects.toBeInstanceOf(OcrEngineError)
  })
})

describe('the engine fails closed rather than reaching for the network', () => {
  it('refuses to start when the verified training data is absent', async () => {
    const h = harness({ exists: false })
    await expect(h.engine.recognize(IMAGE)).rejects.toMatchObject({ code: 'ocr_unavailable' })
    // Crucially it never constructed a worker, which is what would have gone
    // looking for a language file on a CDN.
    expect(h.created).toBe(0)
  })

  it('looks for the language file the frozen manifest installs', () => {
    expect(OCR_TRAINED_DATA_FILE).toBe('eng.traineddata')
  })

  it('reports a bounded code when the worker cannot be constructed', async () => {
    const h = harness({
      createWorker: async () => {
        throw new Error('C:\\Users\\satis\\AppData\\wasm load failed at 0x7ffd')
      }
    })
    await expect(h.engine.recognize(IMAGE)).rejects.toMatchObject({ code: 'ocr_unavailable' })
  })
})

describe('results are normalized before any caller sees them', () => {
  it('returns normalized text and tokens, never the raw engine string', async () => {
    const h = harness({ recognize: async () => ({ text: '  Degree   CERTIFICATE\n\n' }) })
    const result = await h.engine.recognize(IMAGE)
    expect(result.text).toBe('degree certificate')
    expect(result.tokens).toEqual(['degree', 'certificate'])
  })

  it('handles an engine that returns nothing usable', async () => {
    const h = harness({ recognize: async () => ({ text: '' }) })
    await expect(h.engine.recognize(IMAGE)).resolves.toEqual({ text: '', tokens: [] })
  })

  it('handles a malformed engine result without throwing raw', async () => {
    const h = harness({ recognize: async () => ({ text: undefined as unknown as string }) })
    await expect(h.engine.recognize(IMAGE)).resolves.toEqual({ text: '', tokens: [] })
  })
})

describe('no recognized text and no native detail escapes in an error', () => {
  it('carries neither the page text nor a path in any bounded message', async () => {
    const secret = 'ACCOUNT 998877 PASSWORD hunter2'
    const h = harness({
      recognize: async () => {
        throw new Error(`failed while reading "${secret}" from C:\\Users\\satis\\Pictures\\scan.png`)
      }
    })

    const error = await h.engine.recognize(IMAGE).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(OcrEngineError)
    const message = (error as OcrEngineError).message
    expect(message).not.toContain('998877')
    expect(message).not.toContain('hunter2')
    expect(message).not.toMatch(/[A-Za-z]:\\/)
    expect(message).not.toMatch(/0x[0-9a-f]+/i)
  })

  it('writes no recognized text to the console', async () => {
    const spies = [
      vi.spyOn(console, 'log').mockImplementation(() => {}),
      vi.spyOn(console, 'info').mockImplementation(() => {}),
      vi.spyOn(console, 'warn').mockImplementation(() => {}),
      vi.spyOn(console, 'error').mockImplementation(() => {}),
      vi.spyOn(console, 'debug').mockImplementation(() => {})
    ]
    try {
      const h = harness({ recognize: async () => ({ text: 'CONFIDENTIAL SALARY 120000' }) })
      await h.engine.recognize(IMAGE)
      await h.engine.release()
      for (const spy of spies) {
        expect(spy).not.toHaveBeenCalled()
      }
    } finally {
      for (const spy of spies) spy.mockRestore()
    }
  })
})
