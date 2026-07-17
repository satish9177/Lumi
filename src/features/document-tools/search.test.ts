import { mkdtemp, mkdir, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import {
  DocumentSearchValidationError,
  canonicalizeApprovedRoots,
  isPathWithinRoot,
  resolveApprovedDocumentPath,
  searchApprovedDocuments
} from './search'

const temporaryFolders: string[] = []

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
  it('returns deterministic, case-insensitive filename matches from only approved folders', async () => {
    const { root, outside } = await createFixture()
    await mkdir(join(root, 'materials'))
    await Promise.all([
      writeFile(join(root, 'Resume.pdf'), 'first'),
      writeFile(join(root, 'materials', 'resume-notes.txt'), 'second'),
      writeFile(join(root, 'materials', 'agenda.txt'), 'third'),
      writeFile(join(outside, 'resume-private.txt'), 'outside')
    ])

    const roots = await canonicalizeApprovedRoots([root])
    const search = await searchApprovedDocuments(roots, 'ReSuMe', { maxDepth: 2, maxResults: 10 })
    const repeatedSearch = await searchApprovedDocuments(roots, 'resume', { maxDepth: 2, maxResults: 10 })

    expect(search.results.map((result) => result.relativePath)).toEqual(['materials/resume-notes.txt', 'Resume.pdf'])
    expect(search.results.every((result) => isPathWithinRoot(result.path, search.approvedRoots[0]!.canonicalPath))).toBe(true)
    expect(search.results.every((result) => !result.path.includes('outside'))).toBe(true)
    expect(search.results.map((result) => result.id)).toEqual(repeatedSearch.results.map((result) => result.id))
  })

  it('enforces query, depth, and result-count bounds', async () => {
    const { root } = await createFixture()
    await mkdir(join(root, 'z-one', 'two'), { recursive: true })
    await Promise.all([
      writeFile(join(root, 'resume-a.txt'), 'a'),
      writeFile(join(root, 'resume-b.txt'), 'b'),
      writeFile(join(root, 'z-one', 'resume-c.txt'), 'c'),
      writeFile(join(root, 'z-one', 'two', 'resume-d.txt'), 'd')
    ])

    const roots = await canonicalizeApprovedRoots([root])
    const rootOnly = await searchApprovedDocuments(roots, 'resume', { maxDepth: 0, maxResults: 10 })
    const capped = await searchApprovedDocuments(roots, 'resume', { maxDepth: 8, maxResults: 2 })

    expect(rootOnly.results.map((result) => result.name)).toEqual(['resume-a.txt', 'resume-b.txt'])
    expect(capped.results.map((result) => result.name)).toEqual(['resume-a.txt', 'resume-b.txt'])
    await expect(searchApprovedDocuments(roots, '   ')).rejects.toBeInstanceOf(DocumentSearchValidationError)
    await expect(searchApprovedDocuments(roots, 'resume', { maxDepth: 9 })).rejects.toBeInstanceOf(
      DocumentSearchValidationError
    )
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
  })
})
