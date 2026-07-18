import { mkdtemp, mkdir, realpath, rm, symlink, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { normalizeSearchQuery } from '../../shared/search-query'
import {
  DocumentSearchValidationError,
  canonicalizeApprovedRoots,
  isPathWithinRoot,
  resolveApprovedDocumentPath,
  searchApprovedDocuments
} from './search'

const temporaryFolders: string[] = []
const NOW = Date.parse('2026-07-18T12:00:00.000Z')
const now = () => NOW

afterEach(async () => {
  await Promise.all(temporaryFolders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createFixture(): Promise<{ root: string; outside: string }> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-document-tools-'))
  temporaryFolders.push(folder)

  const root = join(folder, 'approved')
  const outside = join(folder, 'outside')
  await Promise.all([mkdir(root), mkdir(outside)])
  return { root, outside }
}

/** Writes a file with an explicit age so recency ranking is deterministic. */
async function writeAged(path: string, contents: string, ageDays: number): Promise<void> {
  await writeFile(path, contents)
  const modified = new Date(NOW - ageDays * 24 * 60 * 60 * 1_000)
  await utimes(path, modified, modified)
}

async function search(root: string, request: string, options = {}) {
  const roots = await canonicalizeApprovedRoots([root])
  return searchApprovedDocuments(roots, normalizeSearchQuery({ queryTerms: request }), { now, ...options })
}

describe('canonicalizeApprovedRoots', () => {
  it('resolves folders, removes duplicate/nested roots, and rejects files', async () => {
    const { root } = await createFixture()
    const nested = join(root, 'nested')
    const file = join(root, 'not-a-folder.txt')
    await mkdir(nested)
    await writeFile(file, 'not a folder')

    const roots = await canonicalizeApprovedRoots([nested, root, root])

    expect(roots).toHaveLength(1)
    expect(roots[0]?.canonicalPath).toBe(await realpath(root))
    await expect(canonicalizeApprovedRoots([file])).rejects.toBeInstanceOf(DocumentSearchValidationError)
  })
})

describe('searchApprovedDocuments', () => {
  it('returns only files inside approved folders, case and separator insensitively', async () => {
    const { root, outside } = await createFixture()
    await mkdir(join(root, 'materials'))
    await Promise.all([
      writeAged(join(root, 'Resume.pdf'), 'first', 1),
      writeAged(join(root, 'materials', 'resume-notes.txt'), 'second', 2),
      writeAged(join(root, 'materials', 'agenda.txt'), 'third', 3),
      writeAged(join(outside, 'resume-private.txt'), 'outside', 1)
    ])

    const found = await search(root, 'ReSuMe')
    const repeated = await search(root, 'resume')

    expect(found.results.map((result) => result.relativePath).sort()).toEqual(['Resume.pdf', 'materials/resume-notes.txt'])
    expect(found.results.every((result) => isPathWithinRoot(result.path, found.approvedRoots[0]!.canonicalPath))).toBe(true)
    expect(found.results.every((result) => !result.path.includes('outside'))).toBe(true)
    expect(found.results.map((result) => result.id)).toEqual(repeated.results.map((result) => result.id))
  })

  it('matches a descriptive filename without an exact query', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'Resume_Final_2026.pdf'), 'a', 5),
      writeAged(join(root, 'notes.txt'), 'b', 1)
    ])

    const found = await search(root, 'resume')

    expect(found.fallback).toBe(false)
    expect(found.results.map((result) => result.name)).toEqual(['Resume_Final_2026.pdf'])
  })

  it('matches a CV filename for a resume query through category synonyms', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'satish-cv.docx'), 'a', 3),
      writeAged(join(root, 'unrelated.txt'), 'b', 1)
    ])

    const byResume = await search(root, 'resume')
    const byCv = await search(root, 'my CV')

    expect(byResume.results.map((result) => result.name)).toEqual(['satish-cv.docx'])
    expect(byCv.results.map((result) => result.name)).toEqual(['satish-cv.docx'])
  })

  it('keeps the newest match when far more than twenty files match', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'archive'))
    // Alphabetically last but modified most recently: an enumeration that
    // stopped at the first twenty matches would lose this file entirely.
    for (let index = 0; index < 40; index += 1) {
      await writeAged(join(root, 'archive', `resume-${String(index).padStart(2, '0')}.pdf`), 'old', 100 + index)
    }
    await writeAged(join(root, 'zz-newest-resume.pdf'), 'new', 0)

    const found = await search(root, 'latest resume')

    expect(found.totalMatches).toBe(41)
    expect(found.results).toHaveLength(10)
    expect(found.results[0]?.name).toBe('zz-newest-resume.pdf')
    expect(found.truncatedTraversal).toBe(false)
  })

  it('ranks plausible newest matches first for a latest request', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'resume-2024.pdf'), 'old', 400),
      writeAged(join(root, 'resume-2026.pdf'), 'new', 2),
      writeAged(join(root, 'resume-2025.pdf'), 'mid', 200)
    ])

    const found = await search(root, 'latest resume')

    expect(found.results.map((result) => result.name)).toEqual([
      'resume-2026.pdf',
      'resume-2025.pdf',
      'resume-2024.pdf'
    ])
  })

  it('offers recent documents as possibilities when no filename is plausible', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'scan_0231.pdf'), 'a', 1),
      writeAged(join(root, 'scan_0142.pdf'), 'b', 9),
      writeAged(join(root, 'holiday.jpg'), 'c', 0)
    ])

    const found = await search(root, 'certificate')

    expect(found.fallback).toBe(true)
    expect(found.totalMatches).toBe(0)
    // Newest documents first, and the photo is not offered for a document query.
    expect(found.results.map((result) => result.name)).toEqual(['scan_0231.pdf', 'scan_0142.pdf'])
  })

  it('filters by requested kind and treats screenshots as photos', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'Screenshots'))
    await Promise.all([
      writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-18.png'), 'a', 0),
      writeAged(join(root, 'beach.jpg'), 'b', 1),
      writeAged(join(root, 'screenshot-notes.txt'), 'c', 0)
    ])

    const roots = await canonicalizeApprovedRoots([root])
    const screenshots = await searchApprovedDocuments(roots, normalizeSearchQuery({ queryTerms: 'newest screenshot' }), { now })
    const photos = await searchApprovedDocuments(roots, normalizeSearchQuery({ queryTerms: 'photo', kind: 'photo' }), { now })

    expect(screenshots.results[0]?.name).toBe('Screenshot 2026-07-18.png')
    expect(screenshots.results[0]?.kind).toBe('screenshot')
    expect(photos.results.map((result) => result.name)).toEqual(
      expect.arrayContaining(['Screenshot 2026-07-18.png', 'beach.jpg'])
    )
  })

  it('puts the newest screenshot candidates first', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'Screenshots'))
    await Promise.all([
      writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-10.png'), 'old', 8),
      writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-17.png'), 'new', 1),
      writeAged(join(root, 'Screenshots', 'Screenshot 2026-07-14.png'), 'mid', 4),
      writeAged(join(root, 'beach.jpg'), 'photo', 0)
    ])

    const found = await search(root, 'newest screenshot')

    expect(found.fallback).toBe(false)
    expect(found.results.map((result) => result.name)).toEqual([
      'Screenshot 2026-07-17.png',
      'Screenshot 2026-07-14.png',
      'Screenshot 2026-07-10.png'
    ])
    // The newer ordinary photo is not a screenshot and must not lead the list.
    expect(found.results.every((result) => result.kind === 'screenshot')).toBe(true)
  })

  it('ranks photo matches by name and folder without inspecting image contents', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'Goa 2026'))
    await Promise.all([
      writeAged(join(root, 'Goa 2026', 'ravi-beach.jpg'), 'a', 10),
      writeAged(join(root, 'IMG_2031.jpg'), 'b', 1),
      writeAged(join(root, 'notes.txt'), 'c', 0)
    ])

    const roots = await canonicalizeApprovedRoots([root])
    const found = await searchApprovedDocuments(roots, normalizeSearchQuery({ queryTerms: 'ravi beach', kind: 'photo' }), { now })

    expect(found.results[0]?.name).toBe('ravi-beach.jpg')
    expect(found.results.every((result) => result.kind !== 'document')).toBe(true)
  })

  it('offers recent photos as possibilities when a photo query matches no filename', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'IMG_2031.jpg'), 'a', 1),
      writeAged(join(root, 'IMG_1980.jpg'), 'b', 30),
      writeAged(join(root, 'resume.pdf'), 'c', 0)
    ])

    const roots = await canonicalizeApprovedRoots([root])
    const found = await searchApprovedDocuments(roots, normalizeSearchQuery({ queryTerms: 'dog', kind: 'photo' }), { now })

    expect(found.fallback).toBe(true)
    // Newest photos only: the document is never offered as a photo possibility.
    expect(found.results.map((result) => result.name)).toEqual(['IMG_2031.jpg', 'IMG_1980.jpg'])
  })

  it('skips temporary, lock, zero-byte, and hidden-folder junk', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, '.hidden'))
    await Promise.all([
      writeAged(join(root, 'resume.pdf.crdownload'), 'partial', 0),
      writeAged(join(root, '~$resume.docx'), 'lock', 0),
      writeAged(join(root, 'resume.tmp'), 'temp', 0),
      writeFile(join(root, 'resume-empty.pdf'), ''),
      writeAged(join(root, '.hidden', 'resume-hidden.pdf'), 'hidden', 0),
      writeAged(join(root, 'resume-real.pdf'), 'real', 1)
    ])

    const found = await search(root, 'resume')

    expect(found.results.map((result) => result.name)).toEqual(['resume-real.pdf'])
  })

  it('enumerates before ranking, so a depth cap and not a match cap bounds the walk', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'one', 'two'), { recursive: true })
    await Promise.all([
      writeAged(join(root, 'resume-a.txt'), 'a', 3),
      writeAged(join(root, 'one', 'resume-b.txt'), 'b', 2),
      writeAged(join(root, 'one', 'two', 'resume-c.txt'), 'c', 1)
    ])

    const rootOnly = await search(root, 'resume', { maxDepth: 0 })
    const deep = await search(root, 'resume', { maxDepth: 6 })
    const cappedResults = await search(root, 'resume', { maxResults: 1 })

    expect(rootOnly.results.map((result) => result.name)).toEqual(['resume-a.txt'])
    expect(deep.totalMatches).toBe(3)
    // Truncation happens after ranking: the single result is the newest match.
    expect(cappedResults.totalMatches).toBe(3)
    expect(cappedResults.results.map((result) => result.name)).toEqual(['resume-c.txt'])
  })

  it('reports an early stop when the entry budget is exhausted', async () => {
    const { root } = await createFixture()
    await Promise.all([
      writeAged(join(root, 'resume-a.txt'), 'a', 1),
      writeAged(join(root, 'resume-b.txt'), 'b', 1),
      writeAged(join(root, 'resume-c.txt'), 'c', 1)
    ])

    const found = await search(root, 'resume', { maxEntries: 2 })

    expect(found.truncatedTraversal).toBe(true)
    expect(found.results.length).toBeLessThanOrEqual(2)
  })

  it('enforces option bounds and requires at least one approved root', async () => {
    const { root } = await createFixture()
    const roots = await canonicalizeApprovedRoots([root])
    const query = normalizeSearchQuery({ queryTerms: 'resume' })

    await expect(searchApprovedDocuments(roots, query, { maxDepth: 9 })).rejects.toBeInstanceOf(DocumentSearchValidationError)
    await expect(searchApprovedDocuments([], query)).rejects.toBeInstanceOf(DocumentSearchValidationError)
  })
})

describe('resolveApprovedDocumentPath', () => {
  it('rejects an outside file and a symlink escape while retaining an approved file', async () => {
    const { root, outside } = await createFixture()
    const approvedFile = join(root, 'resume.pdf')
    const outsideFile = join(outside, 'private-resume.pdf')
    await Promise.all([writeFile(approvedFile, 'approved'), writeFile(outsideFile, 'private')])

    const roots = await canonicalizeApprovedRoots([root])
    expect(await resolveApprovedDocumentPath(approvedFile, roots)).toBe(await realpath(approvedFile))
    expect(await resolveApprovedDocumentPath(outsideFile, roots)).toBeUndefined()

    const escape = join(root, 'escape-to-outside')
    try {
      await symlink(outsideFile, escape, 'file')
    } catch (error: unknown) {
      // Windows environments without Developer Mode can forbid symlink creation.
      if ((error as NodeJS.ErrnoException).code === 'EPERM') {
        return
      }
      throw error
    }

    expect(await resolveApprovedDocumentPath(escape, roots)).toBeUndefined()

    // A symlinked file is also never enumerated as a search candidate.
    const found = await search(root, 'escape')
    expect(found.results.map((result) => result.name)).not.toContain('escape-to-outside')
  })
})
