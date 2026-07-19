import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  Notification: { isSupported: () => false },
  nativeImage: { createFromPath: () => undefined },
  shell: { openExternal: vi.fn(), openPath: vi.fn() }
}))

import { MAX_CAPTURE_BYTES } from './capture'

import { mkdir, writeFile } from 'node:fs/promises'
import { LocalStore } from './store'
import { executeConfirmedTool, executeToolAfterConfirmation, setAnalysisImageLoader } from './tools'

/** Compresses predictably so the byte ladder is exercised deterministically. */
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
    return new FakeImage(width, Math.max(1, Math.round(this.height * (width / this.width))), this.bytesAtFullSize)
  }

  toJPEG(quality: number): Buffer {
    const areaRatio = (this.width * this.height) / (1_600 * 1_200)
    return Buffer.alloc(Math.max(1, Math.round(this.bytesAtFullSize * areaRatio * (quality / 72))))
  }

  isEmpty(): boolean {
    return false
  }
}

const defaultPhotoLoader = () => new FakeImage(1_600, 1_200, 400_000)

async function createPhotoWorkspace(loadImage: () => FakeImage | undefined = defaultPhotoLoader): Promise<{
  store: LocalStore
  photoResultId: string
  documentResultId: string
  outsideResultId: string
  root: string
}> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-photo-'))
  folders.push(folder)
  const root = join(folder, 'pictures')
  const outside = join(folder, 'private')
  await Promise.all([mkdir(root), mkdir(outside)])
  await Promise.all([
    writeFile(join(root, 'beach.jpg'), 'photo'),
    writeFile(join(root, 'resume.pdf'), 'document'),
    writeFile(join(outside, 'secret.jpg'), 'secret')
  ])

  const store = new LocalStore(join(folder, 'state'))
  const approvedRoot = await store.addDocumentRoot(root, 'Pictures')
  const saved = await store.saveSearchResults([
    { rootId: approvedRoot.id, name: 'beach.jpg', relativePath: 'beach.jpg', kind: 'photo', modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: join(root, 'beach.jpg') },
    { rootId: approvedRoot.id, name: 'resume.pdf', relativePath: 'resume.pdf', kind: 'document', modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: join(root, 'resume.pdf') },
    { rootId: approvedRoot.id, name: 'secret.jpg', relativePath: 'secret.jpg', kind: 'photo', modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: join(outside, 'secret.jpg') }
  ])

  setAnalysisImageLoader(loadImage)
  return {
    store,
    root,
    photoResultId: saved[0]!.id,
    documentResultId: saved[1]!.id,
    outsideResultId: saved[2]!.id
  }
}

function analyzeProposal(resultId: string, question = 'What is in this photo?') {
  return {
    id: 'proposal-analyze',
    toolName: 'analyze_photo',
    reason: 'Send only this photo.',
    requiresConfirmation: true,
    arguments: { resultId, question }
  }
}

const folders: string[] = []

afterEach(async () => {
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createStore(): Promise<LocalStore> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-tools-'))
  folders.push(folder)
  return new LocalStore(folder)
}

describe('executeConfirmedTool', () => {
  it('does not write a rejected reminder', async () => {
    const store = await createStore()
    const result = await executeToolAfterConfirmation(store, {
      id: 'proposal-rejected-reminder',
      toolName: 'create_reminder',
      reason: 'Prepare for the interview.',
      requiresConfirmation: true,
      arguments: {
        title: 'Prepare for interview',
        dueAt: '2026-07-20T09:00:00+05:30',
        sourceContext: {
          captureId: 'capture-1',
          summary: 'Interview email with a preparation request.',
          capturedAt: '2026-07-18T09:00:00.000Z',
          signals: []
        }
      }
    }, false)

    expect(result).toMatchObject({ ok: false, message: expect.stringMatching(/nothing was changed/i) })
    await expect(store.listReminders()).resolves.toEqual([])
  })

  it('does not search a folder for a rejected search proposal', async () => {
    const store = await createStore()
    const result = await executeToolAfterConfirmation(store, {
      id: 'proposal-rejected-search',
      toolName: 'search_documents',
      reason: 'Find the stored resume.',
      requiresConfirmation: true,
      arguments: { queryTerms: 'resume', recency: 'latest' }
    }, false)

    expect(result).toMatchObject({ ok: false, message: expect.stringMatching(/nothing was changed/i) })
    expect(result.searchResults).toBeUndefined()
  })

  it('refuses a confirmed search when no folder is approved', async () => {
    const result = await executeConfirmedTool(await createStore(), {
      id: 'proposal-search-no-folder',
      toolName: 'search_documents',
      reason: 'Find the stored resume.',
      requiresConfirmation: true,
      arguments: { queryTerms: 'resume' }
    })

    expect(result).toMatchObject({ ok: false, message: expect.stringMatching(/approve a folder/i) })
  })

  it('rejects an unknown file result without opening anything', async () => {
    const result = await executeConfirmedTool(await createStore(), {
      id: 'proposal-open',
      toolName: 'open_file',
      reason: 'Open the selected file.',
      requiresConfirmation: true,
      arguments: { resultId: 'unknown-result-id' }
    })

    expect(result).toMatchObject({ ok: false, message: expect.stringMatching(/not a result from an approved search/i) })
  })

  it('prepares exactly one downscaled image for an approved photo analysis', async () => {
    const workspace = await createPhotoWorkspace()

    const result = await executeConfirmedTool(workspace.store, analyzeProposal(workspace.photoResultId))

    expect(result.ok).toBe(true)
    expect(result.analysisImage).toMatchObject({
      resultId: workspace.photoResultId,
      name: 'beach.jpg',
      mimeType: 'image/jpeg'
    })
    // Photo analysis is downscaled before using the bounded JPEG ladder.
    const bytes = Buffer.from(result.analysisImage!.dataUrl.split(',')[1] ?? '', 'base64').byteLength
    expect(bytes).toBeLessThanOrEqual(MAX_CAPTURE_BYTES)
    expect(result.analysisImage!.width).toBeLessThanOrEqual(1_024)
    expect(JSON.stringify({ ...result, analysisImage: { ...result.analysisImage, dataUrl: '' } })).not.toContain(workspace.root)
  })

  it('uploads nothing for a rejected photo analysis', async () => {
    const workspace = await createPhotoWorkspace()

    const result = await executeToolAfterConfirmation(workspace.store, analyzeProposal(workspace.photoResultId), false)

    expect(result).toMatchObject({ ok: false, message: expect.stringMatching(/nothing was changed/i) })
    expect(result.analysisImage).toBeUndefined()
  })

  it('refuses to analyse an unknown result, a non-image, or a file outside its approved root', async () => {
    const workspace = await createPhotoWorkspace()

    const unknown = await executeConfirmedTool(workspace.store, analyzeProposal('unknown-result-id'))
    const document = await executeConfirmedTool(workspace.store, analyzeProposal(workspace.documentResultId))
    const outside = await executeConfirmedTool(workspace.store, analyzeProposal(workspace.outsideResultId))

    expect(unknown).toMatchObject({ ok: false, message: expect.stringMatching(/not a result from an approved search/i) })
    expect(document).toMatchObject({ ok: false, message: expect.stringMatching(/not an image/i) })
    expect(outside).toMatchObject({ ok: false, message: expect.stringMatching(/no longer available/i) })
    for (const result of [unknown, document, outside]) {
      expect(result.analysisImage).toBeUndefined()
    }
  })

  it('reports a corrupt image and an oversized image without throwing', async () => {
    const corruptWorkspace = await createPhotoWorkspace(() => undefined)
    const corrupt = await executeConfirmedTool(corruptWorkspace.store, analyzeProposal(corruptWorkspace.photoResultId))

    const hugeWorkspace = await createPhotoWorkspace(() => new FakeImage(1_600, 1_200, 40_000_000))
    const huge = await executeConfirmedTool(hugeWorkspace.store, analyzeProposal(hugeWorkspace.photoResultId))

    expect(corrupt).toMatchObject({ ok: false, message: expect.stringMatching(/could not read/i) })
    expect(huge).toMatchObject({ ok: false, message: expect.stringMatching(/too large/i) })
    expect(corrupt.analysisImage).toBeUndefined()
    expect(huge.analysisImage).toBeUndefined()
  })

  it('persists approved reminders with their capture source context', async () => {
    const store = await createStore()
    const result = await executeConfirmedTool(store, {
      id: 'proposal-reminder',
      toolName: 'create_reminder',
      reason: 'Prepare for the interview.',
      requiresConfirmation: true,
      arguments: {
        title: 'Prepare for interview',
        dueAt: '2026-07-20T09:00:00+05:30',
        sourceContext: {
          captureId: 'capture-1',
          summary: 'Interview email with a preparation request.',
          capturedAt: '2026-07-18T09:00:00.000Z',
          signals: [{ kind: 'date', label: 'Date', value: 'July 20, 2026' }]
        }
      }
    })

    expect(result.ok).toBe(true)
    await expect(store.listReminders()).resolves.toMatchObject([
      { dueAt: '2026-07-20T03:30:00.000Z', sourceContext: { captureId: 'capture-1' } }
    ])
  })
})
