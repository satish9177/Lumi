import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * Remembers where the user put the window, and refuses to restore a position
 * they could not reach.
 *
 * Stored as a bottom-right anchor rather than a rectangle: one point stays
 * valid for both the orb and the panel size, and it matches the way the window
 * grows up-and-left when the panel opens.
 *
 * This file is separate from the main application state so the two writers
 * cannot race.
 */

export const WINDOW_STATE_FILE = 'window-state.json'

/** Gap between the window and the edge of the work area. */
export const WINDOW_MARGIN = 20

/**
 * A stored position is honoured only if at least this much of the window's
 * header strip stays on a display. A window whose header is reachable can
 * always be dragged back by hand; one whose header is off-screen cannot.
 */
export const MIN_VISIBLE = 40
/** Height of the strip that has to remain reachable — the draggable header. */
export const HEADER_STRIP = 40

export interface Size {
  readonly width: number
  readonly height: number
}

export interface Rectangle extends Size {
  readonly x: number
  readonly y: number
}

/** The bottom-right corner the window is pinned to. */
export interface Anchor {
  readonly x: number
  readonly y: number
}

export interface DisplayLike {
  readonly workArea: Rectangle
}

export interface WindowState {
  readonly version: 1
  readonly anchorX: number
  readonly anchorY: number
  readonly open: boolean
  readonly alwaysOnTop: boolean
}

const CURRENT_VERSION = 1

export function defaultWindowState(): WindowState {
  return { version: CURRENT_VERSION, anchorX: 0, anchorY: 0, open: false, alwaysOnTop: true }
}

/** Derives the bottom-right anchor of a rectangle. */
export function anchorOf(bounds: Rectangle): Anchor {
  return { x: bounds.x + bounds.width, y: bounds.y + bounds.height }
}

/**
 * Places a window of `size` so its bottom-right corner sits at `anchor`.
 *
 * This is the arithmetic behind bottom-right expand/collapse: the orb appears
 * to stay exactly where the user left it while the panel grows up and to the
 * left from it.
 */
export function boundsForAnchor(anchor: Anchor, size: Size): Rectangle {
  return { x: anchor.x - size.width, y: anchor.y - size.height, width: size.width, height: size.height }
}

/** Bottom-right of a display's work area — the placement used on first run. */
export function defaultAnchor(display: DisplayLike, size: Size): Anchor {
  return anchorOf(defaultBounds(display, size))
}

/**
 * The default bottom-right placement, guaranteed to sit fully inside the work
 * area.
 *
 * The margin is dropped rather than honoured when the window is nearly as large
 * as the display: pinning the bottom-right corner of an over-tall window would
 * push its header off the top of the screen, which is exactly the unreachable
 * state this module exists to prevent.
 */
export function defaultBounds(display: DisplayLike, size: Size): Rectangle {
  const area = display.workArea
  const fitted = fitTo(size, display)
  return {
    x: clampValue(area.x + area.width - fitted.width - WINDOW_MARGIN, area.x, area.x + area.width - fitted.width),
    y: clampValue(area.y + area.height - fitted.height - WINDOW_MARGIN, area.y, area.y + area.height - fitted.height),
    width: fitted.width,
    height: fitted.height
  }
}

function intersects(a: Rectangle, b: Rectangle): { width: number; height: number } {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  return { width: Math.max(0, width), height: Math.max(0, height) }
}

/**
 * True when enough of the window's header strip overlaps some display that the
 * user could still grab it. This single predicate covers monitor removal,
 * resolution change, and scale change.
 */
export function isReachable(bounds: Rectangle, displays: readonly DisplayLike[]): boolean {
  const header: Rectangle = {
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: Math.min(HEADER_STRIP, bounds.height)
  }

  return displays.some((display) => {
    const overlap = intersects(header, display.workArea)
    return overlap.width >= MIN_VISIBLE && overlap.height >= Math.min(MIN_VISIBLE, header.height)
  })
}

function clampValue(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

/**
 * Resolves a stored anchor into on-screen bounds for the current window size.
 *
 * A position whose header strip is still reachable is nudged fully inside its
 * own display's work area. One that is not reachable — the monitor was
 * unplugged, the resolution shrank — is discarded in favour of `fallback`.
 */
export function clampToDisplays(
  anchor: Anchor,
  size: Size,
  displays: readonly DisplayLike[],
  fallback: DisplayLike
): Rectangle {
  if (displays.length === 0) {
    return defaultBounds(fallback, size)
  }

  const requested = boundsForAnchor(anchor, size)
  if (!isReachable(requested, displays)) {
    return defaultBounds(fallback, size)
  }

  // Keep the window on the display it is actually over, not the primary one.
  const host = displayMatching(requested, displays) ?? fallback
  const fitted = fitTo(size, host)
  const area = host.workArea
  return {
    x: clampValue(requested.x, area.x, area.x + area.width - fitted.width),
    y: clampValue(requested.y, area.y, area.y + area.height - fitted.height),
    width: fitted.width,
    height: fitted.height
  }
}

/** Shrinks a window that cannot fit on the display it landed on. */
function fitTo(size: Size, display: DisplayLike): Size {
  return {
    width: Math.min(size.width, display.workArea.width),
    height: Math.min(size.height, display.workArea.height)
  }
}

/** The display covering the most of `bounds`, mirroring screen.getDisplayMatching. */
export function displayMatching(bounds: Rectangle, displays: readonly DisplayLike[]): DisplayLike | undefined {
  let best: DisplayLike | undefined
  let bestArea = -1
  for (const display of displays) {
    const overlap = intersects(bounds, display.workArea)
    const area = overlap.width * overlap.height
    if (area > bestArea) {
      bestArea = area
      best = display
    }
  }
  return best
}

function isWindowState(value: unknown): value is WindowState {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as Partial<WindowState>
  return (
    candidate.version === CURRENT_VERSION &&
    Number.isFinite(candidate.anchorX) &&
    Number.isFinite(candidate.anchorY) &&
    typeof candidate.open === 'boolean' &&
    typeof candidate.alwaysOnTop === 'boolean'
  )
}

/**
 * Persists the anchor. Writes are debounced by the caller so a drag does not
 * touch the disk on every frame.
 */
export class WindowStateStore {
  private readonly file: string
  private state: WindowState | undefined
  private timer: ReturnType<typeof setTimeout> | undefined
  /** The write currently in flight, so callers can await it settling. */
  private writing: Promise<void> = Promise.resolve()

  constructor(userDataDir: string, private readonly debounceMs = 400) {
    this.file = join(userDataDir, WINDOW_STATE_FILE)
  }

  /** A missing or corrupt file is not an error — it just means "no memory yet". */
  async load(): Promise<WindowState | undefined> {
    try {
      const raw = await readFile(this.file, 'utf8')
      const parsed: unknown = JSON.parse(raw)
      if (!isWindowState(parsed)) {
        return undefined
      }
      this.state = parsed
      return parsed
    } catch {
      return undefined
    }
  }

  current(): WindowState | undefined {
    return this.state
  }

  /** Queues a write, collapsing rapid updates during a drag into one. */
  save(state: WindowState): void {
    this.state = state
    if (this.timer) {
      clearTimeout(this.timer)
    }
    this.timer = setTimeout(() => {
      this.timer = undefined
      this.writing = this.write()
    }, this.debounceMs)
  }

  /**
   * Writes any queued state and waits for it — including a write the debounce
   * timer already started, so callers such as `before-quit` never race it.
   */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = undefined
      this.writing = this.write()
    }
    await this.writing
  }

  private async write(): Promise<void> {
    if (!this.state) {
      return
    }
    try {
      await writeFile(this.file, JSON.stringify(this.state), 'utf8')
    } catch {
      // Losing a window position is not worth surfacing to the user.
    }
  }

  /** Forgets the stored position so the next placement uses the default. */
  clear(): void {
    this.state = undefined
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = undefined
    }
  }
}
