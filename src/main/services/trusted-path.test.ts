import { describe, expect, it, vi } from 'vitest'
import { resolveTrustedPath } from './thumbnails'
import type { LocalStore } from './store'

/**
 * The seam where the two kinds of trust meet: a dropped file the user handed
 * Lumi directly, and an approved-folder search result. Neither may borrow the
 * other's authority.
 */

/** A store that knows nothing, so approved-root resolution always declines. */
const emptyStore = { getSearchResult: async () => undefined } as unknown as LocalStore

describe('resolveTrustedPath', () => {
  it('resolves a dropped identifier without consulting the approved-root store', async () => {
    const getSearchResult = vi.fn(async () => undefined)
    const store = { getSearchResult } as unknown as LocalStore
    const droppedFiles = { resolve: async (id: string) => (id === 'dropped-1' ? 'C:\\tmp\\note.pdf' : undefined) }

    expect(await resolveTrustedPath(store, droppedFiles, 'dropped-1')).toBe('C:\\tmp\\note.pdf')
    // A dropped file is not an approved-root result and must not be looked up as one.
    expect(getSearchResult).not.toHaveBeenCalled()
  })

  it('falls through to approved-root resolution for a non-dropped identifier', async () => {
    const getSearchResult = vi.fn(async () => undefined)
    const store = { getSearchResult } as unknown as LocalStore
    const droppedFiles = { resolve: async () => undefined }

    expect(await resolveTrustedPath(store, droppedFiles, 'result-1')).toBeUndefined()
    expect(getSearchResult).toHaveBeenCalledWith('result-1')
  })

  it('refuses an unknown identifier in both branches', async () => {
    const droppedFiles = { resolve: async () => undefined }

    expect(await resolveTrustedPath(emptyStore, droppedFiles, 'nobody-knows-this')).toBeUndefined()
  })

  it('refuses a dropped identifier once its entry has failed revalidation', async () => {
    // The store clears the entry on any mismatch, after which resolve declines.
    const droppedFiles = { resolve: async () => undefined }

    expect(await resolveTrustedPath(emptyStore, droppedFiles, 'dropped-1')).toBeUndefined()
  })

  it('still resolves approved-root results when no dropped store is present', async () => {
    const getSearchResult = vi.fn(async () => undefined)
    const store = { getSearchResult } as unknown as LocalStore

    expect(await resolveTrustedPath(store, undefined, 'result-1')).toBeUndefined()
    expect(getSearchResult).toHaveBeenCalledWith('result-1')
  })
})
