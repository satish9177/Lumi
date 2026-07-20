import { describe, expect, it, vi } from 'vitest'
import { countDraggedFiles, decideDrop, preventFileNavigation, TOO_MANY_FILES_MESSAGE } from './drop-intake'

/** A minimal DataTransfer stand-in; jsdom is not part of this test stack. */
function transfer(options: { items?: Array<{ kind: string }>; files?: unknown[] }) {
  return {
    items: (options.items ?? []) as unknown as DataTransferItemList,
    files: (options.files ?? []) as unknown as FileList
  }
}

const FILE = { name: 'photo.png' } as unknown as File

describe('counting a drag', () => {
  it('counts file items during dragover, when files is still empty', () => {
    expect(countDraggedFiles(transfer({ items: [{ kind: 'file' }] }))).toBe(1)
    expect(countDraggedFiles(transfer({ items: [{ kind: 'file' }, { kind: 'file' }] }))).toBe(2)
  })

  it('ignores dragged text and other non-file items', () => {
    expect(countDraggedFiles(transfer({ items: [{ kind: 'string' }, { kind: 'string' }] }))).toBe(0)
  })

  it('falls back to the file list on drop', () => {
    expect(countDraggedFiles(transfer({ files: [FILE] }))).toBe(1)
  })

  it('treats a missing transfer as no files', () => {
    expect(countDraggedFiles(null)).toBe(0)
    expect(countDraggedFiles(undefined)).toBe(0)
  })
})

describe('deciding a drop', () => {
  it('accepts exactly one file', () => {
    const decision = decideDrop(transfer({ items: [{ kind: 'file' }], files: [FILE] }))

    expect(decision).toEqual({ kind: 'accept', file: FILE })
  })

  it('refuses more than one file before main is ever asked', () => {
    const decision = decideDrop(transfer({ items: [{ kind: 'file' }, { kind: 'file' }], files: [FILE, FILE] }))

    expect(decision.kind).toBe('too-many')
    expect(TOO_MANY_FILES_MESSAGE).toMatch(/one file at a time/i)
  })

  it('ignores a drag carrying no files', () => {
    expect(decideDrop(transfer({ items: [{ kind: 'string' }] })).kind).toBe('none')
  })

  it('ignores a drag whose single item has no backing file', () => {
    // A virtual file — an Outlook attachment — reports an item but no File.
    expect(decideDrop(transfer({ items: [{ kind: 'file' }], files: [] })).kind).toBe('none')
  })
})

describe('preventing file navigation', () => {
  it('swallows dragover and drop at the document level', () => {
    const listeners = new Map<string, EventListener>()
    const doc = {
      addEventListener: vi.fn((type: string, listener: EventListener) => listeners.set(type, listener)),
      removeEventListener: vi.fn((type: string) => listeners.delete(type))
    }

    preventFileNavigation(doc as unknown as Document)

    expect(listeners.has('dragover')).toBe(true)
    expect(listeners.has('drop')).toBe(true)

    // Without this the window would navigate to the dropped file.
    for (const type of ['dragover', 'drop']) {
      const preventDefault = vi.fn()
      listeners.get(type)?.({ preventDefault } as unknown as Event)
      expect(preventDefault).toHaveBeenCalledTimes(1)
    }
  })

  it('detaches both listeners on cleanup', () => {
    const doc = { addEventListener: vi.fn(), removeEventListener: vi.fn() }

    preventFileNavigation(doc as unknown as Document)()

    expect(doc.removeEventListener).toHaveBeenCalledTimes(2)
  })
})
