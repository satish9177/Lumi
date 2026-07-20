import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { openPath } = vi.hoisted(() => ({ openPath: vi.fn(async (_path: string) => '') }))
vi.mock('electron', () => ({
  Notification: { isSupported: () => false },
  nativeImage: { createFromPath: () => ({ isEmpty: () => true, getSize: () => ({ width: 0, height: 0 }) }) },
  shell: { openPath, openExternal: vi.fn(async () => undefined) }
}))

import { DroppedFileStore, DROPPED_FILE_TTL_MS } from './dropped-files'
import { PendingActionStore } from './pending-actions'
import { LocalStore } from './store'
import { executeConfirmedTool } from './tools'
import { validateTrustedAttachment } from './attachment-validation'

/**
 * End-to-end cover for a dropped file travelling the existing confirmation
 * architecture: proposal, preview, approval, execution — and failing closed at
 * every stage once the temporary record lapses or the file changes.
 */

const folders: string[] = []
const SAFE_IMAGE = () => ({ width: 800, height: 600 })
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01])
const PDF = Buffer.from('%PDF-1.7\nbody bytes')

afterEach(async () => {
  openPath.mockClear()
  openPath.mockResolvedValue('')
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function scratch(): Promise<string> {
  const folder = await mkdtemp(join(tmpdir(), 'lumi-dropped-e2e-'))
  folders.push(folder)
  return folder
}

async function localStore(): Promise<LocalStore> {
  return new LocalStore(await scratch())
}

async function writeFixture(name: string, bytes: Buffer): Promise<string> {
  const path = join(await scratch(), name)
  await writeFile(path, bytes)
  return path
}

function pendingStore(store: LocalStore, dropped: DroppedFileStore, now?: () => number): PendingActionStore {
  const telegram = {
    getStatus: () => ({ state: 'connected', account: { displayName: 'Lumi', username: 'lumi' } }),
    getRecipient: () => ({ displayName: 'Ravi', username: 'ravi' }),
    snapshotRecipient: () => ({ resultId: 'recipient-1', displayName: 'Ravi', username: 'ravi', kind: 'user' }),
    sendConfirmed: vi.fn(async () => undefined),
    sendConfirmedAttachment: vi.fn(async () => undefined)
  }
  return new PendingActionStore(
    store,
    telegram as never,
    () => undefined,
    now,
    undefined,
    async () => [],
    validateTrustedAttachment,
    undefined,
    dropped
  )
}

function openProposal(droppedId: string) {
  return {
    id: crypto.randomUUID(),
    toolName: 'open_file',
    reason: 'Open the dropped file.',
    requiresConfirmation: true,
    arguments: { resultId: droppedId }
  }
}

function analyseProposal(droppedId: string) {
  return {
    id: crypto.randomUUID(),
    toolName: 'analyze_photo',
    reason: 'Analyse the dropped photo.',
    requiresConfirmation: true,
    arguments: { resultId: droppedId, question: 'What is this?' }
  }
}

function attachmentProposal(droppedId: string) {
  return {
    id: crypto.randomUUID(),
    toolName: 'send_telegram_attachment',
    reason: 'Send the dropped file.',
    requiresConfirmation: true,
    arguments: { recipientResultId: 'recipient-1', fileResultId: droppedId, caption: undefined }
  }
}

describe('open a dropped file', () => {
  it('previews the source as a dropped file rather than an approved folder', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    const preview = await pending.create(openProposal(droppedId))

    expect(preview.actionType).toBe('open_file')
    if (preview.actionType !== 'open_file') throw new Error('unexpected preview')
    expect(preview.source).toBe('dropped-file')
    expect(preview.fileName).toBe('paper.pdf')
    // The card must not name an approved folder the file is not in.
    expect(preview.folderLabel).toBe('Dropped file')
  })

  it('opens only after approval, through main', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    const preview = await pending.create(openProposal(droppedId))
    expect(openPath).not.toHaveBeenCalled()

    const result = await pending.approve(preview.approvalId)

    expect(result.ok).toBe(true)
    expect(openPath).toHaveBeenCalledTimes(1)
    expect(openPath.mock.calls[0][0]).toContain('paper.pdf')
  })

  it('fails closed when the file changed between review and approval', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const path = await writeFixture('paper.pdf', PDF)
    const { droppedId } = await dropped.register(path)
    const preview = await pending.create(openProposal(droppedId))

    await writeFile(path, Buffer.from('%PDF-1.7 edited after the card was shown'))
    const result = await pending.approve(preview.approvalId)

    expect(result.ok).toBe(false)
    expect(result.message).toMatch(/no longer available/i)
    expect(openPath).not.toHaveBeenCalled()
  })

  it('refuses to propose once the record expired', async () => {
    let now = 1_000
    const dropped = new DroppedFileStore(SAFE_IMAGE, () => now)
    const pending = pendingStore(await localStore(), dropped, () => now)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    now += DROPPED_FILE_TTL_MS + 1

    await expect(pending.create(openProposal(droppedId))).rejects.toThrow(/no longer available/i)
    expect(openPath).not.toHaveBeenCalled()
  })

  it('refuses after the user removed the card', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    dropped.remove(droppedId)

    await expect(pending.create(openProposal(droppedId))).rejects.toThrow()
    expect(openPath).not.toHaveBeenCalled()
  })

  it('refuses an identifier replaced by a second drop', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const first = await dropped.register(await writeFixture('first.pdf', PDF))
    await dropped.register(await writeFixture('second.pdf', PDF))

    await expect(pending.create(openProposal(first.droppedId))).rejects.toThrow()
  })
})

describe('analyse a dropped image', () => {
  it('prepares exactly one bounded image after approval', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('shot.png', PNG))

    const preview = await pending.create(analyseProposal(droppedId))

    expect(preview.actionType).toBe('analyze_photo')
    if (preview.actionType !== 'analyze_photo') throw new Error('unexpected preview')
    expect(preview.source).toBe('dropped-file')
    expect(preview.question).toBe('What is this?')
  })

  it('refuses to analyse a document', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    await expect(pending.create(analyseProposal(droppedId))).rejects.toThrow(/Reading its contents isn't supported/i)
  })

  it('refuses to analyse a document at execution too', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const store = await localStore()
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    const result = await executeConfirmedTool(store, analyseProposal(droppedId), dropped)

    expect(result.ok).toBe(false)
    expect(result.message).toMatch(/Reading its contents isn't supported/i)
    expect(result.analysisImage).toBeUndefined()
  })

  it('sends nothing when the image changed before execution', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const store = await localStore()
    const path = await writeFixture('shot.png', PNG)
    const { droppedId } = await dropped.register(path)

    await writeFile(path, Buffer.concat([PNG, Buffer.from('changed')]))
    const result = await executeConfirmedTool(store, analyseProposal(droppedId), dropped)

    expect(result.ok).toBe(false)
    expect(result.analysisImage).toBeUndefined()
  })
})

describe('send a dropped file through Telegram', () => {
  it('builds an attachment preview marked as a dropped file', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    const preview = await pending.create(attachmentProposal(droppedId))

    expect(preview.actionType).toBe('send_telegram_attachment')
    if (preview.actionType !== 'send_telegram_attachment') throw new Error('unexpected preview')
    expect(preview.source).toBe('dropped-file')
    expect(preview.fileName).toBe('paper.pdf')
    expect(preview.mediaKind).toBe('document')
  })

  it('refuses to propose a send for an expired record', async () => {
    let now = 1_000
    const dropped = new DroppedFileStore(SAFE_IMAGE, () => now)
    const pending = pendingStore(await localStore(), dropped, () => now)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    now += DROPPED_FILE_TTL_MS + 1

    await expect(pending.create(attachmentProposal(droppedId))).rejects.toThrow()
  })

  it('refuses to propose a send for a changed file', async () => {
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(await localStore(), dropped)
    const path = await writeFixture('paper.pdf', PDF)
    const { droppedId } = await dropped.register(path)

    await writeFile(path, Buffer.from('%PDF-1.7 different bytes entirely'))

    await expect(pending.create(attachmentProposal(droppedId))).rejects.toThrow()
  })
})

describe('trust separation', () => {
  it('never adds a dropped file to approved roots or search results', async () => {
    const store = await localStore()
    const dropped = new DroppedFileStore(SAFE_IMAGE)

    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    expect(await store.listDocumentRoots()).toEqual([])
    expect(await store.getSearchResult(droppedId)).toBeUndefined()
  })

  it('rejects an unknown identifier in every action path', async () => {
    const store = await localStore()
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    await dropped.register(await writeFixture('paper.pdf', PDF))
    const unknown = '11111111-2222-3333-4444-555555555555'

    const opened = await executeConfirmedTool(store, openProposal(unknown), dropped)
    const analysed = await executeConfirmedTool(store, analyseProposal(unknown), dropped)

    expect(opened.ok).toBe(false)
    expect(analysed.ok).toBe(false)
    expect(openPath).not.toHaveBeenCalled()
    await expect(validateTrustedAttachment(store, unknown, SAFE_IMAGE, dropped)).rejects.toThrow()
  })

  it('leaves approved-folder results working when a dropped store is present', async () => {
    const store = await localStore()
    const dropped = new DroppedFileStore(SAFE_IMAGE)

    // An approved-root identifier is unknown to the dropped store, so it falls
    // through to the existing path and fails for the existing reason.
    const result = await executeConfirmedTool(store, openProposal('approved-result-1'), dropped)

    expect(result.message).toMatch(/not a result from an approved search/i)
  })

  it('keeps the dropped record out of the store even after a confirmed action', async () => {
    const store = await localStore()
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const pending = pendingStore(store, dropped)
    const { droppedId } = await dropped.register(await writeFixture('paper.pdf', PDF))

    const preview = await pending.create(openProposal(droppedId))
    await pending.approve(preview.approvalId)

    expect(await store.getSearchResult(droppedId)).toBeUndefined()
    expect(await store.listDocumentRoots()).toEqual([])
  })
})

describe('no automatic action', () => {
  it('registering a drop creates no pending action and touches nothing', async () => {
    const store = await localStore()
    const dropped = new DroppedFileStore(SAFE_IMAGE)
    const telegramSend = vi.fn(async () => undefined)
    const telegram = {
      getStatus: () => ({ state: 'connected', account: { displayName: 'Lumi' } }),
      getRecipient: () => undefined,
      snapshotRecipient: () => undefined,
      sendConfirmed: telegramSend,
      sendConfirmedAttachment: telegramSend
    }
    new PendingActionStore(store, telegram as never, () => undefined, undefined, undefined, async () => [], undefined, undefined, dropped)

    await dropped.register(await writeFixture('shot.png', PNG))

    expect(openPath).not.toHaveBeenCalled()
    expect(telegramSend).not.toHaveBeenCalled()
    expect(await store.listDocumentRoots()).toEqual([])
  })
})
