import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { DroppedFileDescriptor, PendingActionPreview } from '../../shared/contracts'
import { DroppedFileCard, ToolConfirmationCard } from './components'
import { focusableWithin, nextTrappedFocus } from './focus-trap'
import { deriveStatus, statusAnnouncement } from './status'

const styles = readFileSync(join(process.cwd(), 'src/renderer/src/styles.css'), 'utf8')
const componentStyles = readFileSync(join(process.cwd(), 'src/renderer/src/components/components.css'), 'utf8')
const app = readFileSync(join(process.cwd(), 'src/renderer/src/LifeLensApp.tsx'), 'utf8')

/* ------------------------------------------------------------ live regions */

describe('screen-reader semantics', () => {
  it('marks the conversation as a log', () => {
    expect(app).toMatch(/role="log"/)
  })

  it('announces status politely and errors assertively', () => {
    expect(app).toMatch(/aria-live="polite"[^>]*>\{statusAnnouncement/)
    // An actionable failure interrupts; a status change does not.
    expect(app).toMatch(/role="alert"/)
  })

  it('labels every icon-only header control', () => {
    for (const label of ['COPY.labels.settings', 'Collapse to orb', 'Open Lumi', 'Close settings']) {
      expect(app).toContain(label)
    }
  })

  it('labels the composer and explains why send is disabled', () => {
    expect(app).toMatch(/aria-label=\{COPY\.labels\.ask\}/)
    expect(app).toMatch(/aria-describedby=\{sendDisabledReason/)
  })

  it('exposes progress values rather than a bare bar', () => {
    expect(app).toMatch(/aria-valuenow=/)
    expect(app).toMatch(/aria-valuemax=/)
    expect(app).toMatch(/aria-valuetext=/)
  })

  it('marks real loading states with aria-busy', () => {
    expect(app).toMatch(/aria-busy=/)
  })
})

/* --------------------------------------------------- throttled announcements */

describe('status announcements', () => {
  const idle = {
    companionState: 'idle',
    isConnecting: false,
    hasConnectedBefore: false,
    online: true,
    isSending: false,
    isSearching: false
  } as const

  it('announces ordinary states verbatim', () => {
    expect(statusAnnouncement(deriveStatus({ ...idle, companionState: 'listening' }))).toBe('Listening')
  })

  it('quantises indexing to ten-percent milestones', () => {
    const at = (indexed: number) => {
      const photo = { state: 'indexing', indexed, total: 1000 } as const
      return statusAnnouncement(deriveStatus({ ...idle, photoSearchStatus: photo }), photo)
    }

    // Nearby counts must produce an identical string so the live region is
    // not re-announced every second during a long index.
    expect(at(201)).toBe(at(299))
    expect(at(201)).toBe('Indexing photos, 20% done')
    expect(at(301)).toBe('Indexing photos, 30% done')
  })

  it('never announces a raw file count', () => {
    const photo = { state: 'indexing', indexed: 1240, total: 3000 } as const

    const announcement = statusAnnouncement(deriveStatus({ ...idle, photoSearchStatus: photo }), photo)

    expect(announcement).not.toContain('1,240')
    expect(announcement).not.toContain('3,000')
  })
})

/* ------------------------------------------------------------- focus trap */

describe('settings focus trap', () => {
  const element = (name: string) => ({ name }) as unknown as HTMLElement

  it('wraps forward from the last control to the first', () => {
    const items = [element('a'), element('b'), element('c')]

    expect(nextTrappedFocus(items, items[2]!, false)).toBe(items[0])
    expect(nextTrappedFocus(items, items[0]!, false)).toBeUndefined()
  })

  it('wraps backward from the first control to the last', () => {
    const items = [element('a'), element('b'), element('c')]

    expect(nextTrappedFocus(items, items[0]!, true)).toBe(items[2])
    expect(nextTrappedFocus(items, items[2]!, true)).toBeUndefined()
  })

  it('pulls focus back in when it has escaped the layer', () => {
    const items = [element('a'), element('b')]

    expect(nextTrappedFocus(items, element('outside'), false)).toBe(items[0])
    expect(nextTrappedFocus(items, null, true)).toBe(items[1])
  })

  it('does nothing when the layer holds no controls', () => {
    expect(nextTrappedFocus([], null, false)).toBeUndefined()
  })

  it('finds the controls a user can actually reach', () => {
    let captured = ''
    const container = {
      querySelectorAll: (selector: string) => {
        captured = selector
        return [element('one'), element('two')]
      }
    }

    expect(focusableWithin(container)).toHaveLength(2)
    // Disabled controls and explicitly unreachable ones are excluded.
    expect(captured).toContain('button:not([disabled])')
    expect(captured).toContain('textarea:not([disabled])')
    expect(captured).toContain('[tabindex]:not([tabindex="-1"])')
  })

  it('restores focus to the settings button on close', () => {
    // The cleanup of the trap effect focuses the opener.
    expect(app).toMatch(/opener\?\.focus\(\)/)
  })

  it('skips controls hidden by display:none', () => {
    const hidden = { tagName: 'BUTTON', offsetParent: null } as unknown as HTMLElement
    const visible = { tagName: 'BUTTON', offsetParent: {} } as unknown as HTMLElement
    const container = { querySelectorAll: () => [hidden, visible] }

    expect(focusableWithin(container)).toEqual([visible])
  })
})

describe('hidden layers are not reachable', () => {
  it('unmounts settings entirely rather than hiding it', () => {
    // A hidden-but-present overlay would still be tabbable.
    expect(app).toMatch(/\{settingsOpen && \(/)
  })

  it('unmounts the drop overlay rather than hiding it', () => {
    expect(app).toMatch(/\{dragFileCount > 0 && <DropOverlay/)
  })

  it('keeps the drop overlay out of the pointer and focus path', () => {
    const overlay = styles.slice(styles.indexOf('.drop-overlay {'))

    expect(overlay).toContain('pointer-events: none')
  })

  it('closes settings when the panel collapses', () => {
    expect(app).toMatch(/Settings must never stay open behind the orb[\s\S]{0,120}setSettingsOpen\(false\)/)
  })
})

/* ------------------------------------------------------------ keyboard */

describe('composer keyboard behaviour', () => {
  it('sends on Enter and adds a newline on Shift+Enter', () => {
    expect(app).toMatch(/event\.key === 'Enter' && !event\.shiftKey/)
    expect(app).toMatch(/<textarea/)
  })

  it('does not send while the send control is disabled', () => {
    expect(app).toMatch(/if \(canSend\)/)
  })
})

describe('escape layering', () => {
  it('closes the drop overlay, then settings, then the picker, then the panel', () => {
    const order = app.slice(app.indexOf('event.key !== \'Escape\''))
    const dragIndex = order.indexOf('dragFileCount > 0')
    const settingsIndex = order.indexOf('settingsOpen')
    const pickerIndex = order.indexOf('capturePickerOpen')
    const collapseIndex = order.indexOf('setExpanded(false)')

    expect(dragIndex).toBeGreaterThan(-1)
    expect(dragIndex).toBeLessThan(settingsIndex)
    expect(settingsIndex).toBeLessThan(pickerIndex)
    expect(pickerIndex).toBeLessThan(collapseIndex)
  })

  it('does not clear a pending confirmation', () => {
    const handler = app.slice(app.indexOf('event.key !== \'Escape\''), app.indexOf('window.addEventListener(\'keydown\', onKeyDown)'))

    // Escape closes transient layers only; a confirmation must be answered.
    expect(handler).not.toContain('setPendingAction')
    expect(handler).not.toContain('setSearchConfirmation')
  })
})

/* -------------------------------------------------------- rendered cards */

const DOCUMENT: DroppedFileDescriptor = {
  droppedId: 'dropped-1',
  fileName: 'contract.pdf',
  fileTypeLabel: 'PDF document',
  sizeBytes: 1_048_576,
  mediaKind: 'document',
  expiresAt: '2026-07-20T13:00:00.000Z'
}

describe('dropped-file card accessibility', () => {
  const markup = renderToStaticMarkup(
    <DroppedFileCard file={DOCUMENT} onOpen={vi.fn()} onAnalyse={vi.fn()} onSend={vi.fn()} onRemove={vi.fn()} />
  )

  it('announces the file, its type, its size and that nothing happened', () => {
    expect(markup).toContain('aria-label="File added: contract.pdf, PDF document, 1.0 MB. No action taken."')
  })

  it('names the file in every action label, including Remove', () => {
    for (const label of ['Open contract.pdf', 'Send contract.pdf on Telegram', 'Remove contract.pdf']) {
      expect(markup).toContain(`aria-label="${label}"`)
    }
  })

  it('uses real buttons rather than clickable divs', () => {
    expect(markup).not.toMatch(/<div[^>]*onclick/i)
    expect((markup.match(/<button/g) ?? []).length).toBe(3)
  })
})

describe('confirmation card accessibility', () => {
  const preview = {
    approvalId: 'approval-1',
    createdAt: '2026-07-20T12:00:00.000Z',
    expiresAt: '2026-07-20T12:02:00.000Z',
    actionType: 'open_file',
    fileName: 'resume.pdf',
    relativePath: 'resume.pdf',
    folderLabel: 'Dropped file',
    source: 'dropped-file'
  } as PendingActionPreview

  const markup = renderToStaticMarkup(
    <ToolConfirmationCard action={preview} onConfirm={vi.fn()} onDismiss={vi.fn()} />
  )

  it('is a labelled group describing the proposed action', () => {
    expect(markup).toContain('role="group"')
    expect(markup).toContain('aria-labelledby')
    expect(markup).toContain('aria-describedby')
  })

  it('states that confirmation is required, inside the description', () => {
    expect(markup).toContain('Lumi only acts when you confirm. Nothing happens if you cancel.')
  })

  it('is fully keyboard operable through real buttons', () => {
    expect((markup.match(/<button/g) ?? []).length).toBe(2)
  })

  it('does not imply approved-folder trust for a dropped file', () => {
    expect(markup).toContain('Dropped file — temporary, not an approved folder')
    expect(markup).not.toContain('Approved folder')
  })
})

/* ------------------------------------------------ styling and reduced motion */

describe('reduced motion', () => {
  it('gates the orb pulse behind a motion preference', () => {
    const gated = styles.slice(styles.indexOf('@media (prefers-reduced-motion: no-preference)'))

    expect(gated).toContain('animation: pulse')
  })

  it('gates the hover lift behind a motion preference', () => {
    expect(styles).toMatch(/@media \(prefers-reduced-motion: no-preference\)[\s\S]*?transform: translateY\(-1px\)/)
  })

  it('reduces every animation and transition when reduced motion is requested', () => {
    const reduce = styles.slice(styles.indexOf('@media (prefers-reduced-motion: reduce)'))

    expect(reduce).toContain('animation-duration')
    expect(reduce).toContain('transition-duration')
    expect(reduce).toContain('animation-iteration-count: 1')
  })

  it('keeps the glow itself, because it carries state', () => {
    // Only the motion is optional; the state indication is not.
    expect(styles).toMatch(/\.state-listening \.companion-glow[\s\S]{0,220}opacity: 1;/)
  })
})

describe('typography and contrast', () => {
  it('has no user-facing text below 11px', () => {
    const offenders = [...styles.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)]
      .concat([...componentStyles.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)])
      .map((match) => Number(match[1]))
      .filter((size) => size < 11)

    expect(offenders).toEqual([])
  })

  it('keeps a visible focus ring on every new interactive surface', () => {
    for (const selector of ['.chip:focus-visible', 'textarea:focus-visible', 'summary:focus-visible']) {
      expect(styles).toContain(selector)
    }
  })

  it('wraps long filenames and errors rather than widening the panel', () => {
    expect(styles).toMatch(/overflow-wrap: anywhere/)
    expect(styles).toMatch(/overflow-x: hidden/)
  })

  it('keeps every control bounded under Windows High Contrast', () => {
    const forced = styles.slice(styles.indexOf('@media (forced-colors: active)'))

    expect(forced).toContain('border: 1px solid CanvasText')
    expect(forced).toContain('outline: 2px solid Highlight')
  })

  it('never conveys status by colour alone', () => {
    // The dot is decorative; the label always carries the state in words.
    const pill = readFileSync(join(process.cwd(), 'src/renderer/src/components/StatusPill.tsx'), 'utf8')

    expect(pill).toContain('aria-hidden="true"')
    expect(pill).toContain('status.label')
  })
})
