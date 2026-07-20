import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  anchorOf,
  boundsForAnchor,
  clampToDisplays,
  defaultAnchor,
  defaultBounds,
  displayMatching,
  isReachable,
  WindowStateStore,
  WINDOW_MARGIN,
  type DisplayLike
} from './window-state'

const PRIMARY: DisplayLike = { workArea: { x: 0, y: 0, width: 1920, height: 1040 } }
const SECONDARY: DisplayLike = { workArea: { x: 1920, y: 0, width: 1280, height: 1000 } }
const ORB = { width: 88, height: 88 }
const PANEL = { width: 390, height: 640 }

describe('bottom-right anchoring', () => {
  it('derives the bottom-right corner of a rectangle', () => {
    expect(anchorOf({ x: 100, y: 200, width: 88, height: 88 })).toEqual({ x: 188, y: 288 })
  })

  it('keeps the anchor fixed while the window grows up and to the left', () => {
    const anchor = { x: 1900, y: 1020 }

    const orb = boundsForAnchor(anchor, ORB)
    const panel = boundsForAnchor(anchor, PANEL)

    expect(anchorOf(orb)).toEqual(anchor)
    expect(anchorOf(panel)).toEqual(anchor)
    // The panel extends further up and left from the same corner.
    expect(panel.x).toBeLessThan(orb.x)
    expect(panel.y).toBeLessThan(orb.y)
  })

  it('places the default anchor at the bottom-right of the work area', () => {
    expect(defaultAnchor(PRIMARY, ORB)).toEqual({ x: 1920 - WINDOW_MARGIN, y: 1040 - WINDOW_MARGIN })
  })
})

describe('reachability', () => {
  it('accepts a window sitting fully on a display', () => {
    expect(isReachable({ x: 100, y: 100, width: 390, height: 640 }, [PRIMARY])).toBe(true)
  })

  it('accepts a window whose header strip is only partly visible', () => {
    // Mostly off the right edge, but 60 px of header remains grabbable.
    expect(isReachable({ x: 1860, y: 500, width: 390, height: 640 }, [PRIMARY])).toBe(true)
  })

  it('rejects a window pushed off the bottom even though its body overlaps', () => {
    // The header strip is below the work area, so there is nothing to grab.
    expect(isReachable({ x: 400, y: 1035, width: 390, height: 640 }, [PRIMARY])).toBe(false)
  })

  it('rejects a window with only a sliver of header on screen', () => {
    expect(isReachable({ x: 1900, y: 500, width: 390, height: 640 }, [PRIMARY])).toBe(false)
  })

  it('rejects a window on a display that is gone', () => {
    const onSecondary = { x: 2200, y: 300, width: 390, height: 640 }

    expect(isReachable(onSecondary, [PRIMARY, SECONDARY])).toBe(true)
    expect(isReachable(onSecondary, [PRIMARY])).toBe(false)
  })
})

describe('clampToDisplays', () => {
  it('honours a reachable stored position', () => {
    const anchor = { x: 1900, y: 1020 }

    expect(clampToDisplays(anchor, PANEL, [PRIMARY], PRIMARY)).toEqual({
      x: 1510,
      y: 380,
      width: 390,
      height: 640
    })
  })

  it('nudges a partly off-screen window fully into its own work area', () => {
    const bounds = clampToDisplays({ x: 2000, y: 1020 }, PANEL, [PRIMARY], PRIMARY)

    expect(bounds.x + bounds.width).toBeLessThanOrEqual(PRIMARY.workArea.width)
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(PRIMARY.workArea.height)
  })

  it('clamps against the secondary display a window is actually over', () => {
    const bounds = clampToDisplays({ x: 3190, y: 980 }, PANEL, [PRIMARY, SECONDARY], PRIMARY)

    // Stays on the secondary monitor rather than being dragged to the primary.
    expect(bounds.x).toBeGreaterThanOrEqual(SECONDARY.workArea.x)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(SECONDARY.workArea.x + SECONDARY.workArea.width)
  })

  it('falls back to the primary display when the stored monitor is gone', () => {
    const strandedOnSecondary = { x: 3190, y: 980 }

    const bounds = clampToDisplays(strandedOnSecondary, PANEL, [PRIMARY], PRIMARY)

    expect(bounds).toEqual(boundsForAnchor(defaultAnchor(PRIMARY, PANEL), PANEL))
  })

  it('falls back when the work area shrinks below the stored position', () => {
    const shrunk: DisplayLike = { workArea: { x: 0, y: 0, width: 800, height: 600 } }

    const bounds = clampToDisplays({ x: 1900, y: 1020 }, PANEL, [shrunk], shrunk)

    expect(bounds).toEqual(defaultBounds(shrunk, PANEL))
  })

  it('keeps the fallback reachable when the window is taller than the display', () => {
    // A panel taller than the work area must not have its header pushed off the
    // top by the bottom-right margin.
    const shortDisplay: DisplayLike = { workArea: { x: 0, y: 0, width: 800, height: 600 } }

    const bounds = clampToDisplays({ x: 9999, y: 9999 }, PANEL, [shortDisplay], shortDisplay)

    expect(bounds.y).toBeGreaterThanOrEqual(0)
    expect(isReachable(bounds, [shortDisplay])).toBe(true)
  })

  it('shrinks a window larger than the display it lands on', () => {
    const tiny: DisplayLike = { workArea: { x: 0, y: 0, width: 300, height: 400 } }

    const bounds = clampToDisplays(defaultAnchor(tiny, PANEL), PANEL, [tiny], tiny)

    expect(bounds.width).toBe(300)
    expect(bounds.height).toBe(400)
  })

  it('falls back when no display is reported at all', () => {
    const bounds = clampToDisplays({ x: 1900, y: 1020 }, ORB, [], PRIMARY)

    expect(bounds).toEqual(boundsForAnchor(defaultAnchor(PRIMARY, ORB), ORB))
  })
})

describe('displayMatching', () => {
  it('picks the display covering most of the window', () => {
    expect(displayMatching({ x: 2000, y: 100, width: 390, height: 640 }, [PRIMARY, SECONDARY])).toBe(SECONDARY)
    expect(displayMatching({ x: 100, y: 100, width: 390, height: 640 }, [PRIMARY, SECONDARY])).toBe(PRIMARY)
  })
})

describe('WindowStateStore', () => {
  let dir: string

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), 'lumi-window-state-'))
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns nothing when no file has been written', async () => {
    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })

  it('round-trips a debounced save', async () => {
    const store = new WindowStateStore(dir)
    const state = { version: 1, anchorX: 1900, anchorY: 1020, open: true, alwaysOnTop: true } as const

    store.save(state)
    await vi.advanceTimersByTimeAsync(400)
    // The debounce timer starts the write; flush awaits the one already in flight.
    await store.flush()

    expect(await new WindowStateStore(dir).load()).toEqual(state)
  })

  it('does not touch the disk while a drag is still moving', async () => {
    const store = new WindowStateStore(dir)

    for (let i = 0; i < 20; i += 1) {
      store.save({ version: 1, anchorX: 1000 + i, anchorY: 900, open: false, alwaysOnTop: true })
      await vi.advanceTimersByTimeAsync(10)
    }

    // 200 ms of continuous movement, all inside the 400 ms debounce.
    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })

  it('writes only the final anchor once a drag settles', async () => {
    const store = new WindowStateStore(dir)

    for (let i = 0; i < 20; i += 1) {
      store.save({ version: 1, anchorX: 1000 + i, anchorY: 900, open: false, alwaysOnTop: true })
      await vi.advanceTimersByTimeAsync(10)
    }
    await store.flush()

    expect((await new WindowStateStore(dir).load())?.anchorX).toBe(1019)
  })

  it('ignores a corrupt file', async () => {
    await writeFile(join(dir, 'window-state.json'), '{ not json', 'utf8')

    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })

  it('ignores a file from an unknown version', async () => {
    await writeFile(
      join(dir, 'window-state.json'),
      JSON.stringify({ version: 99, anchorX: 10, anchorY: 10, open: false, alwaysOnTop: true }),
      'utf8'
    )

    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })

  it('ignores a file missing required fields', async () => {
    await writeFile(join(dir, 'window-state.json'), JSON.stringify({ version: 1, anchorX: 10 }), 'utf8')

    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })

  it('flush writes immediately without waiting for the debounce', async () => {
    const store = new WindowStateStore(dir)

    store.save({ version: 1, anchorX: 5, anchorY: 6, open: false, alwaysOnTop: true })
    await store.flush()

    const raw = await readFile(join(dir, 'window-state.json'), 'utf8')
    expect(JSON.parse(raw).anchorX).toBe(5)
  })

  it('clear cancels a queued write', async () => {
    const store = new WindowStateStore(dir)

    store.save({ version: 1, anchorX: 5, anchorY: 6, open: false, alwaysOnTop: true })
    store.clear()
    await vi.advanceTimersByTimeAsync(400)

    expect(await new WindowStateStore(dir).load()).toBeUndefined()
  })
})
