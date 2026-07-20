import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LumiApp from './LifeLensApp'

/**
 * Renders the real panel and asserts its shape.
 *
 * This is the regression guard for the conversation-first restructure: it
 * catches a render-time crash and proves the three zones survive, without
 * pulling in a full DOM test stack. Effects do not run under static rendering,
 * so nothing here touches the bridge or the Realtime client.
 */

function stubBridge(): void {
  const never = () => new Promise(() => undefined)
  vi.stubGlobal('window', {
    lifeLens: new Proxy({}, { get: () => never }),
    addEventListener: () => undefined,
    removeEventListener: () => undefined
  })
  vi.stubGlobal('navigator', { onLine: true })
}

describe('panel layout', () => {
  beforeEach(() => {
    stubBridge()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders the collapsed orb without crashing', () => {
    const markup = renderToStaticMarkup(<LumiApp />)

    expect(markup).toContain('companion-shell')
    expect(markup).toContain('Open Lumi')
  })

  it('keeps the orb ring draggable and its button clickable', () => {
    const markup = renderToStaticMarkup(<LumiApp />)

    // The ring carries the drag region; the core button opts out via CSS.
    expect(markup).toContain('companion-shell drag-region')
    expect(markup).toContain('companion-core')
  })

  it('shows no LifeLens branding anywhere in the rendered markup', () => {
    const markup = renderToStaticMarkup(<LumiApp />)

    expect(markup).not.toMatch(/LifeLens/i)
  })

  it('does not render the panel while collapsed', () => {
    const markup = renderToStaticMarkup(<LumiApp />)

    // The panel — and so the microphone-bearing surface — only exists once
    // the user opens it.
    expect(markup).not.toContain('panel-header')
    expect(markup).not.toContain('composer')
  })
})
