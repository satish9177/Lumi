import { setTakeoverActive, takeoverGuardState, type TakeoverGuardState } from './capture'
import type { AgentResult } from '../../shared/agent-contracts'
import type { TakeoverReconciliation } from './browser-profile-controller'

/**
 * Milestone 8a S2 closure: the fail-closed startup path for screen capture.
 *
 * `capture.ts`'s guard starts at `unreconciled`, which refuses every capture.
 * Something has to move it, and only two answers may: the runtime's own
 * durable list of open takeovers (`BrowserProfileController.reconcileTakeovers`),
 * or the fact that this installation has no agent runtime at all. This class
 * is the piece that keeps asking until it has one of them.
 *
 * Why the second answer is allowed to clear the guard, and why it is not a
 * loophole: the headed sign-in browser only ever exists as a descendant of a
 * runtime process, and the only thing that starts a runtime process is
 * `AgentRuntimeSupervisor` in this same main process. When main has
 * conclusively established that it has no supervisor -- packaged with no or
 * invalid `agent-runtime.json`, or a development checkout whose runtime
 * configuration is invalid -- there is no runtime to query, there never was
 * one this process could have started, and there is consequently no takeover
 * to protect. Without this, an install that simply does not use browser
 * profiles would lose screen capture permanently, which is a worse answer
 * than the honest one. Main decides that, not this class and never the
 * renderer: `agentRuntimeAbsent` is only true once startup has settled it.
 *
 * Everything else -- a runtime that is starting, restarting, unreachable,
 * refusing, or answering with something unparseable -- leaves the guard
 * refused and schedules another attempt. A runtime outage never becomes
 * permission to capture, and no timeout ever expires into one.
 */
export interface TakeoverCaptureGuardOptions {
  /** Reads durable takeover state and sets the capture guard from it. */
  reconcile: () => Promise<AgentResult<TakeoverReconciliation>>
  /**
   * True only once main has established that this installation has no agent
   * runtime at all. Never true while runtime startup is still in progress.
   */
  agentRuntimeAbsent: () => boolean
  retryDelayMs?: number
  setTimer?: (callback: () => void, delayMs: number) => unknown
  clearTimer?: (handle: unknown) => void
}

const DEFAULT_RETRY_DELAY_MS = 3_000

export class TakeoverCaptureGuard {
  private readonly options: TakeoverCaptureGuardOptions
  private readonly retryDelayMs: number
  private readonly setTimer: (callback: () => void, delayMs: number) => unknown
  private readonly clearTimer: (handle: unknown) => void
  private retry: unknown
  private running = false
  private stopped = false

  constructor(options: TakeoverCaptureGuardOptions) {
    this.options = options
    this.retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS
    this.setTimer = options.setTimer ?? ((callback, delayMs) => setTimeout(callback, delayMs))
    this.clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle as NodeJS.Timeout))
  }

  /** The state capture is currently being held in. */
  state(): TakeoverGuardState {
    return takeoverGuardState()
  }

  /**
   * One reconciliation attempt. Resolves to `true` when durable takeover
   * state is now known (open or not), `false` when it still is not -- in
   * which case capture stays refused and another attempt is scheduled.
   *
   * Safe to call repeatedly and concurrently: a second call while one is in
   * flight is a no-op that reports the current state, and two sequential
   * calls reach the same place as one.
   */
  async reconcileNow(): Promise<boolean> {
    if (this.stopped || this.running) return takeoverGuardState() !== 'unreconciled'
    this.running = true
    try {
      if (this.options.agentRuntimeAbsent()) {
        setTakeoverActive(false)
        return true
      }
      const result = await this.options.reconcile()
      if (result.ok) return true
    } catch {
      // A reconciliation that throws is a reconciliation that did not happen.
    } finally {
      this.running = false
    }
    this.scheduleRetry()
    return false
  }

  /** Begin reconciling, and keep retrying until an answer arrives. */
  start(): void {
    this.stopped = false
    void this.reconcileNow()
  }

  /** Stop retrying. The guard keeps whatever state it is in; it is never
   * cleared on the way out, because quitting is not an answer either. */
  stop(): void {
    this.stopped = true
    this.cancelRetry()
  }

  private scheduleRetry(): void {
    if (this.stopped || this.retry !== undefined) return
    this.retry = this.setTimer(() => {
      this.retry = undefined
      void this.reconcileNow()
    }, this.retryDelayMs)
  }

  private cancelRetry(): void {
    if (this.retry === undefined) return
    this.clearTimer(this.retry)
    this.retry = undefined
  }
}
