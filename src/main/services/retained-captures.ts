import type { CaptureResult } from '../../shared/contracts'

const DEFAULT_CAPTURE_TTL_MS = 10 * 60 * 1_000

export interface RetainedCapture {
  readonly id: string
  readonly capturedAt: string
  readonly dataUrl: string
}

export class RetainedCaptureStore {
  private capture: RetainedCapture | undefined
  private expiresAt = 0
  private expiryTimer: ReturnType<typeof setTimeout> | undefined

  constructor(
    private readonly ttlMs = DEFAULT_CAPTURE_TTL_MS,
    private readonly now: () => number = () => Date.now()
  ) {}

  replace(capture: CaptureResult): void {
    this.clear()
    this.capture = { id: capture.id, capturedAt: capture.capturedAt, dataUrl: capture.dataUrl }
    this.expiresAt = this.now() + this.ttlMs
    this.expiryTimer = setTimeout(() => this.clear(capture.id), this.ttlMs)
  }

  get(captureId: string): RetainedCapture | undefined {
    if (!this.capture || this.capture.id !== captureId) {
      return undefined
    }
    if (this.now() >= this.expiresAt) {
      this.clear(captureId)
      return undefined
    }
    return this.capture
  }

  clear(captureId?: string): void {
    if (captureId && this.capture?.id !== captureId) {
      return
    }
    if (this.expiryTimer) {
      clearTimeout(this.expiryTimer)
      this.expiryTimer = undefined
    }
    this.capture = undefined
    this.expiresAt = 0
  }
}
