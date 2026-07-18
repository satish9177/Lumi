import { mkdir, mkdtemp, rm, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { normalizeSearchQuery } from '../../shared/search-query'
import { runDocumentSearch } from './document-search'
import { LocalStore } from './store'

const folders: string[] = []
const NOW = Date.parse('2026-07-18T12:00:00.000Z')
const now = () => NOW

afterEach(async () => {
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createWorkspace(): Promise<{ store: LocalStore; root: string; second: string }> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-document-search-'))
  folders.push(folder)
  const root = join(folder, 'documents')
  const second = join(folder, 'archive')
  await Promise.all([mkdir(root), mkdir(second)])
  return { store: new LocalStore(join(folder, 'state')), root, second }
}

async function writeAged(path: string, ageDays: number): Promise<void> {
  await writeFile(path, 'content')
  const modified = new Date(NOW - ageDays * 24 * 60 * 60 * 1_000)
  await utimes(path, modified, modified)
}

describe('runDocumentSearch', () => {
  it('searches every approved root and returns newest-first results', async () => {
    const { store, root, second } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await store.addDocumentRoot(second, 'Archive')
    await writeAged(join(root, 'Resume_2025.pdf'), 300)
    await writeAged(join(second, 'Resume_2026.pdf'), 2)

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'latest resume' }), now)

    expect(search.fallback).toBe(false)
    expect(search.results.map((result) => result.name)).toEqual(['Resume_2026.pdf', 'Resume_2025.pdf'])
    expect(search.compactResults).toEqual([
      { ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '2 days ago' },
      { ordinal: 2, name: 'Resume_2025.pdf', modifiedAgo: expect.stringMatching(/month|year/) }
    ])
  })

  it('keeps identifiers and paths out of the compact model view', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await mkdir(join(root, 'career'))
    await writeAged(join(root, 'career', 'Resume_2026.pdf'), 1)

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'resume' }), now)

    expect(search.results[0]).toMatchObject({ relativePath: 'career/Resume_2026.pdf', kind: 'document' })
    const serializedCompact = JSON.stringify(search.compactResults)
    expect(serializedCompact).not.toContain(search.results[0]!.id)
    expect(serializedCompact).not.toContain(search.results[0]!.rootId)
    expect(serializedCompact).not.toContain('career/')
    expect(serializedCompact).not.toContain(root)
  })

  it('stores results so a later open_file can resolve them, replacing the previous set', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await writeAged(join(root, 'Resume_2026.pdf'), 1)
    await writeAged(join(root, 'Certificate.pdf'), 1)

    const first = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'resume' }), now)
    await expect(store.getSearchResult(first.results[0]!.id)).resolves.toMatchObject({ name: 'Resume_2026.pdf' })

    const second = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'certificate' }), now)

    expect(second.results.map((result) => result.name)).toEqual(['Certificate.pdf'])
    // A stale ordinal from the previous search can no longer be opened.
    await expect(store.getSearchResult(first.results[0]!.id)).resolves.toBeUndefined()
  })

  it('offers recent possibilities and says so when nothing matches by name', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await writeAged(join(root, 'scan_0231.pdf'), 1)

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'certificate' }), now)

    expect(search.fallback).toBe(true)
    expect(search.results.map((result) => result.name)).toEqual(['scan_0231.pdf'])
    expect(search.message).toMatch(/do not ask the user for an exact filename/i)
  })

  it('tells the model plainly that a photo search never inspected image contents', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Pictures')
    await mkdir(join(root, 'Screenshots'))
    await writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-18.png'), 1)
    await writeAged(join(root, 'ravi-beach.jpg'), 3)

    const matched = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'ravi beach', kind: 'photo' }), now)
    const unmatched = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'dog', kind: 'photo' }), now)

    for (const search of [matched, unmatched]) {
      expect(search.message).toMatch(/only file names, folders, and dates/i)
      expect(search.message).toMatch(/cannot recognise people, faces, or objects/i)
      expect(search.message).toMatch(/pick one photo/i)
    }
  })

  it('does not attach the photo caveat to an ordinary document search', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await writeAged(join(root, 'Resume_2026.pdf'), 1)

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'resume' }), now)

    expect(search.message).not.toMatch(/recognise/i)
  })

  it('returns image results with a photo or screenshot kind for the grid', async () => {
    const { store, root } = await createWorkspace()
    await store.addDocumentRoot(root, 'Pictures')
    await mkdir(join(root, 'Screenshots'))
    await writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-18.png'), 1)

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'newest screenshot' }), now)

    expect(search.results[0]).toMatchObject({ name: 'Screenshot 2026-07-18.png', kind: 'screenshot' })
    // The trusted result keeps a safe relative location for the card label.
    expect(search.results[0]!.relativePath).toBe('Screenshots/Screenshot 2026-07-18.png')
  })

  it('reports plainly when no folder is approved', async () => {
    const { store } = await createWorkspace()

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'resume' }), now)

    expect(search.results).toEqual([])
    expect(search.message).toMatch(/approve a folder/i)
  })

  it('skips an approved folder that has disappeared instead of failing', async () => {
    const { store, root, second } = await createWorkspace()
    await store.addDocumentRoot(root, 'Documents')
    await store.addDocumentRoot(second, 'Archive')
    await writeAged(join(root, 'Resume_2026.pdf'), 1)
    await rm(second, { recursive: true, force: true })

    const search = await runDocumentSearch(store, normalizeSearchQuery({ queryTerms: 'resume' }), now)

    expect(search.results.map((result) => result.name)).toEqual(['Resume_2026.pdf'])
  })
})
