import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { LocalStore } from './store'

const folders: string[] = []

afterEach(async () => {
  await Promise.all(folders.splice(0).map(async (folder) => {
    const { rm } = await import('node:fs/promises')
    await rm(folder, { recursive: true, force: true })
  }))
})

describe('LocalStore', () => {
  it('recovers to an empty state when its JSON file is corrupted', async () => {
    const folder = await mkdtemp(join(tmpdir(), 'lifelens-store-'))
    folders.push(folder)
    await writeFile(join(folder, 'lifelens-state.json'), '{not valid JSON', 'utf8')

    const store = new LocalStore(folder)

    await expect(store.listReminders()).resolves.toEqual([])
    await expect(store.listDocumentRoots()).resolves.toEqual([])
  })

  it('does not resolve an unknown file-result identifier', async () => {
    const folder = await mkdtemp(join(tmpdir(), 'lifelens-store-'))
    folders.push(folder)
    const store = new LocalStore(folder)

    await expect(store.getSearchResult('unknown-result-id')).resolves.toBeUndefined()
  })
})
