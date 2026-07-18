import { mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({ nativeImage: { createFromPath: () => undefined } }))

import { LocalStore } from './store'
import {
  MAX_THUMBNAILS,
  MAX_THUMBNAIL_BYTES,
  THUMBNAIL_MAX_HEIGHT,
  THUMBNAIL_MAX_WIDTH,
  createResultThumbnails,
  type ImageLoader
} from './thumbnails'

const folders: string[] = []

afterEach(async () => {
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

/** Compresses predictably: bytes scale with area and quality. */
class FakeImage {
  constructor(
    private readonly width: number,
    private readonly height: number,
    private readonly bytesAtFullSize: number
  ) {}

  getSize(): { width: number; height: number } {
    return { width: this.width, height: this.height }
  }

  resize({ width }: { width: number }): FakeImage {
    const scaled = Math.max(1, Math.round(this.height * (width / this.width)))
    return new FakeImage(width, scaled, this.bytesAtFullSize)
  }

  toJPEG(quality: number): Buffer {
    const areaRatio = (this.width * this.height) / (1_600 * 1_200)
    return Buffer.alloc(Math.max(1, Math.round(this.bytesAtFullSize * areaRatio * (quality / 70))))
  }

  isEmpty(): boolean {
    return false
  }
}

interface Workspace {
  store: LocalStore
  root: string
  outside: string
  addResult: (name: string, options?: { absolutePath?: string; kind?: 'photo' | 'document' }) => Promise<string>
}

async function createWorkspace(): Promise<Workspace> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-thumbnails-'))
  folders.push(folder)
  const root = join(folder, 'pictures')
  const outside = join(folder, 'private')
  await Promise.all([mkdir(root), mkdir(outside)])
  const store = new LocalStore(join(folder, 'state'))
  const approvedRoot = await store.addDocumentRoot(root, 'Pictures')
  const staged: Array<{ rootId: string; name: string; relativePath: string; modifiedAt: string; kind: 'photo' | 'document'; absolutePath: string }> = []

  return {
    store,
    root,
    outside,
    async addResult(name, options = {}) {
      const absolutePath = options.absolutePath ?? join(root, name)
      await writeFile(absolutePath, 'image-bytes').catch(() => undefined)
      staged.push({
        rootId: approvedRoot.id,
        name,
        relativePath: name,
        modifiedAt: '2026-07-18T09:00:00.000Z',
        kind: options.kind ?? 'photo',
        absolutePath
      })
      const saved = await store.saveSearchResults(staged)
      return saved[staged.length - 1]!.id
    }
  }
}

const loadFake = (image: FakeImage = new FakeImage(1_600, 1_200, 400_000)): ImageLoader => () => image

describe('createResultThumbnails', () => {
  it('builds a bounded thumbnail for a stored image result', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('beach.jpg')

    const [thumbnail] = await createResultThumbnails(workspace.store, [resultId], loadFake())

    expect(thumbnail).toMatchObject({ resultId, status: 'ok' })
    expect(thumbnail!.width).toBeLessThanOrEqual(THUMBNAIL_MAX_WIDTH)
    expect(thumbnail!.height).toBeLessThanOrEqual(THUMBNAIL_MAX_HEIGHT)
    // Aspect ratio is preserved rather than cropped.
    expect(thumbnail!.width! / thumbnail!.height!).toBeCloseTo(4 / 3, 2)
    expect(thumbnail!.dataUrl).toMatch(/^data:image\/jpeg;base64,/)
    expect(byteLengthOf(thumbnail!.dataUrl)).toBeLessThanOrEqual(MAX_THUMBNAIL_BYTES)
  })

  it('accepts only identifiers produced by an approved search', async () => {
    const workspace = await createWorkspace()
    const loader = vi.fn(loadFake())

    const thumbnails = await createResultThumbnails(workspace.store, ['not-a-real-result-id'], loader)

    expect(thumbnails).toEqual([{ resultId: 'not-a-real-result-id', status: 'unavailable' }])
    expect(loader).not.toHaveBeenCalled()
  })

  it('rejects a stored result whose path escapes its approved root', async () => {
    const workspace = await createWorkspace()
    const outsideFile = join(workspace.outside, 'private.jpg')
    const resultId = await workspace.addResult('private.jpg', { absolutePath: outsideFile })
    const loader = vi.fn(loadFake())

    const thumbnails = await createResultThumbnails(workspace.store, [resultId], loader)

    expect(thumbnails).toEqual([{ resultId, status: 'unavailable' }])
    expect(loader).not.toHaveBeenCalled()
  })

  it('rejects a symlink that points outside the approved root', async () => {
    const workspace = await createWorkspace()
    const outsideFile = join(workspace.outside, 'secret.jpg')
    await writeFile(outsideFile, 'secret')
    const link = join(workspace.root, 'link.jpg')
    try {
      await symlink(outsideFile, link, 'file')
    } catch (error: unknown) {
      // Windows without Developer Mode forbids symlink creation.
      if ((error as NodeJS.ErrnoException).code === 'EPERM') {
        return
      }
      throw error
    }

    const resultId = await workspace.addResult('link.jpg', { absolutePath: link })
    const thumbnails = await createResultThumbnails(workspace.store, [resultId], loadFake())

    expect(thumbnails).toEqual([{ resultId, status: 'unavailable' }])
  })

  it('refuses to render a non-image result', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('resume.pdf', { kind: 'document' })
    const loader = vi.fn(loadFake())

    const thumbnails = await createResultThumbnails(workspace.store, [resultId], loader)

    expect(thumbnails).toEqual([{ resultId, status: 'unsupported' }])
    expect(loader).not.toHaveBeenCalled()
  })

  it('reports a placeholder instead of crashing on a corrupt image', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('corrupt.png')

    const thrown = await createResultThumbnails(workspace.store, [resultId], () => { throw new Error('decode failed') })
    const empty = await createResultThumbnails(workspace.store, [resultId], () => undefined)

    expect(thrown).toEqual([{ resultId, status: 'unsupported' }])
    expect(empty).toEqual([{ resultId, status: 'unsupported' }])
  })

  it('never returns more than the display cap', async () => {
    const workspace = await createWorkspace()
    const resultIds: string[] = []
    for (let index = 0; index < MAX_THUMBNAILS + 8; index += 1) {
      resultIds.push(await workspace.addResult(`photo-${index}.jpg`))
    }

    const thumbnails = await createResultThumbnails(workspace.store, resultIds, loadFake())

    expect(thumbnails).toHaveLength(MAX_THUMBNAILS)
  })

  it('keeps a very large image inside the per-thumbnail byte cap', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('huge.jpg')

    const [thumbnail] = await createResultThumbnails(
      workspace.store,
      [resultId],
      loadFake(new FakeImage(6_000, 4_500, 8_000_000))
    )

    expect(thumbnail!.status).toBe('ok')
    expect(byteLengthOf(thumbnail!.dataUrl)).toBeLessThanOrEqual(MAX_THUMBNAIL_BYTES)
  })

  it('stops spending bytes once the aggregate cap is reached', async () => {
    const workspace = await createWorkspace()
    const resultIds: string[] = []
    for (let index = 0; index < MAX_THUMBNAILS; index += 1) {
      resultIds.push(await workspace.addResult(`photo-${index}.jpg`))
    }

    // Each thumbnail lands near the per-image cap, so the set cap bites.
    const thumbnails = await createResultThumbnails(
      workspace.store,
      resultIds,
      loadFake(new FakeImage(1_600, 1_200, 3_000_000))
    )
    const totalBytes = thumbnails.reduce((sum, thumbnail) => sum + byteLengthOf(thumbnail.dataUrl), 0)

    expect(totalBytes).toBeLessThanOrEqual(MAX_THUMBNAILS * MAX_THUMBNAIL_BYTES)
    expect(thumbnails.every((thumbnail) => byteLengthOf(thumbnail.dataUrl) <= MAX_THUMBNAIL_BYTES)).toBe(true)
  })

  it('reports the failure when an image cannot be shrunk into the cap', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('stubborn.jpg')

    // Bytes that never fall with size: the encoder must give up cleanly.
    const stubborn = {
      getSize: () => ({ width: 1_600, height: 1_200 }),
      resize: () => stubborn,
      toJPEG: () => Buffer.alloc(MAX_THUMBNAIL_BYTES + 1),
      isEmpty: () => false
    }
    const [thumbnail] = await createResultThumbnails(workspace.store, [resultId], () => stubborn)

    expect(thumbnail).toEqual({ resultId, status: 'too_large' })
  })

  it('exposes no absolute path in its returned payload', async () => {
    const workspace = await createWorkspace()
    const resultId = await workspace.addResult('beach.jpg')

    const thumbnails = await createResultThumbnails(workspace.store, [resultId], loadFake())

    const serialized = JSON.stringify(thumbnails)
    expect(serialized).not.toContain(workspace.root)
    expect(serialized).not.toContain('beach.jpg')
  })
})

function byteLengthOf(dataUrl: string | undefined): number {
  if (!dataUrl) {
    return 0
  }
  return Buffer.from(dataUrl.split(',')[1] ?? '', 'base64').byteLength
}
