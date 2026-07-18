import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { openExternal } = vi.hoisted(() => ({ openExternal: vi.fn(async () => undefined) }))
vi.mock('electron', () => ({
  Notification: { isSupported: () => false },
  shell: { openExternal, openPath: vi.fn() }
}))

import { LocalStore } from './store'
import { PendingActionStore } from './pending-actions'
import type { createResultThumbnails } from './thumbnails'

const folders: string[] = []
const sourceContext = {
  captureId: 'capture-1',
  summary: 'Interview email asks for preparation before the meeting.',
  capturedAt: '2026-07-18T09:00:00.000Z',
  signals: []
}

afterEach(async () => {
  openExternal.mockClear()
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createStore(): Promise<LocalStore> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-pending-actions-'))
  folders.push(folder)
  return new LocalStore(folder)
}

function createPendingStore(
  store: LocalStore,
  options: { now?: () => number; ttlMs?: number; telegram?: object; thumbnails?: typeof createResultThumbnails } = {}
): PendingActionStore {
  const telegram = options.telegram ?? {
    getStatus: () => ({ state: 'connected', account: { displayName: 'Lumi', username: 'lumi' } }),
    getRecipient: () => ({ displayName: 'Ravi', username: 'ravi' }),
    sendConfirmed: vi.fn(async () => undefined)
  }
  const thumbnails = options.thumbnails ?? (async () => [])
  return new PendingActionStore(store, telegram as never, () => undefined, options.now, options.ttlMs, thumbnails)
}

describe('PendingActionStore', () => {
  it('executes one in-app approval exactly once and retains the trusted reminder payload', async () => {
    const store = await createStore()
    const pending = createPendingStore(store)
    const raw = {
      id: 'reminder-proposal', toolName: 'create_reminder', reason: 'Prepare.', requiresConfirmation: true,
      arguments: { title: 'Prepare for interview', dueAt: '2026-07-20T09:00:00+05:30', sourceContext }
    }
    const preview = await pending.create(raw)
    raw.arguments.title = 'Altered after proposal'
    expect(preview).toMatchObject({ actionType: 'create_reminder', title: 'Prepare for interview', sourceContextSummary: sourceContext.summary })

    await expect(pending.approve(preview.approvalId)).resolves.toMatchObject({ ok: true })
    await expect(pending.approve(preview.approvalId)).rejects.toThrow(/already handled/i)
    await expect(store.listReminders()).resolves.toMatchObject([{ title: 'Prepare for interview' }])
  })

  it('cancels without writing and rejects unknown or expired approval IDs', async () => {
    let now = Date.parse('2026-07-18T10:00:00.000Z')
    const store = await createStore()
    const pending = createPendingStore(store, { now: () => now, ttlMs: 100 })
    const preview = await pending.create({
      id: 'cancel-reminder', toolName: 'create_reminder', reason: 'Prepare.', requiresConfirmation: true,
      arguments: { title: 'Do not create', dueAt: '2026-07-20T09:00:00.000Z', sourceContext }
    })
    pending.cancel(preview.approvalId)
    await expect(pending.approve(preview.approvalId)).rejects.toThrow(/already handled/i)
    await expect(store.listReminders()).resolves.toEqual([])
    await expect(pending.approve('unknown-approval')).rejects.toThrow(/invalid/i)

    const expiring = await pending.create({
      id: 'expiring-reminder', toolName: 'create_reminder', reason: 'Prepare.', requiresConfirmation: true,
      arguments: { title: 'Expires', dueAt: '2026-07-20T09:00:00.000Z', sourceContext }
    })
    now += 101
    await expect(pending.approve(expiring.approvalId)).rejects.toThrow(/expired/i)
  })

  it('uses trusted folder and file records instead of renderer-supplied labels or paths', async () => {
    const store = await createStore()
    const root = await store.addDocumentRoot('C:\\approved', 'Approved resumes')
    const [result] = await store.saveSearchResults([{
      rootId: root.id, name: 'resume.pdf', relativePath: '2026/resume.pdf', kind: 'document',
      modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: 'C:\\approved\\2026\\resume.pdf'
    }])
    const pending = createPendingStore(store)
    const preview = await pending.create({
      id: 'file-proposal', toolName: 'open_file', reason: 'Open it.', requiresConfirmation: true,
      arguments: { resultId: result!.id }
    })
    expect(preview).toMatchObject({ actionType: 'open_file', fileName: 'resume.pdf', relativePath: '2026/resume.pdf', folderLabel: 'Approved resumes' })
    expect(JSON.stringify(preview)).not.toContain('C:\\approved')
  })

  it('shows trusted photo details and a main-built preview, ignoring renderer-supplied fields', async () => {
    const store = await createStore()
    const root = await store.addDocumentRoot('C:\\approved', 'Approved photos')
    const [result] = await store.saveSearchResults([{
      rootId: root.id, name: 'beach.jpg', relativePath: 'trips/beach.jpg', kind: 'photo',
      modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: 'C:\\approved\\trips\\beach.jpg'
    }])
    const pending = createPendingStore(store, {
      thumbnails: async () => [{ resultId: result!.id, status: 'ok', dataUrl: 'data:image/jpeg;base64,QUJD', width: 240, height: 180 }]
    })

    const preview = await pending.create({
      id: 'photo-proposal', toolName: 'analyze_photo', reason: 'Look at it.', requiresConfirmation: true,
      arguments: {
        resultId: result!.id,
        question: 'Who is in this photo?',
        // A renderer cannot smuggle its own filename, path, or image bytes.
        fileName: 'attacker.jpg',
        absolutePath: 'C:\\Windows\\secret.png',
        dataUrl: 'data:image/jpeg;base64,ZZZZ'
      }
    })

    expect(preview).toMatchObject({
      actionType: 'analyze_photo',
      fileName: 'beach.jpg',
      relativePath: 'trips/beach.jpg',
      folderLabel: 'Approved photos',
      question: 'Who is in this photo?',
      previewDataUrl: 'data:image/jpeg;base64,QUJD'
    })
    const serialized = JSON.stringify(preview)
    expect(serialized).not.toContain('attacker.jpg')
    expect(serialized).not.toContain('C:\\Windows')
    expect(serialized).not.toContain('C:\\approved')
  })

  it('uploads one photo exactly once and refuses a duplicate or cancelled approval', async () => {
    const store = await createStore()
    const root = await store.addDocumentRoot('C:\\approved', 'Approved photos')
    const [result] = await store.saveSearchResults([{
      rootId: root.id, name: 'beach.jpg', relativePath: 'beach.jpg', kind: 'photo',
      modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: 'C:\\approved\\beach.jpg'
    }])
    const pending = createPendingStore(store)
    const proposal = {
      id: 'photo-approve', toolName: 'analyze_photo', reason: 'Look at it.', requiresConfirmation: true,
      arguments: { resultId: result!.id, question: 'What is this?' }
    }

    const preview = await pending.create(proposal)
    // The path does not exist in this fixture, so execution stops safely; what
    // matters is that a second approval can never run at all.
    await pending.approve(preview.approvalId)
    await expect(pending.approve(preview.approvalId)).rejects.toThrow(/already handled/i)

    const cancelled = await pending.create(proposal)
    pending.cancel(cancelled.approvalId)
    await expect(pending.approve(cancelled.approvalId)).rejects.toThrow(/already handled/i)
  })

  it('clears an approved-but-unsent photo when the session ends', async () => {
    const store = await createStore()
    const root = await store.addDocumentRoot('C:\\approved', 'Approved photos')
    const [result] = await store.saveSearchResults([{
      rootId: root.id, name: 'beach.jpg', relativePath: 'beach.jpg', kind: 'photo',
      modifiedAt: '2026-07-18T09:00:00.000Z', absolutePath: 'C:\\approved\\beach.jpg'
    }])
    const pending = createPendingStore(store)
    const preview = await pending.create({
      id: 'photo-session', toolName: 'analyze_photo', reason: 'Look at it.', requiresConfirmation: true,
      arguments: { resultId: result!.id, question: 'What is this?' }
    })

    pending.clearPhotoAnalysis()

    await expect(pending.approve(preview.approvalId)).rejects.toThrow(/invalid/i)
  })

  it('sends no Telegram message before the one stored approval and clears relevant pending actions', async () => {
    const sendConfirmed = vi.fn(async () => undefined)
    const telegram = {
      getStatus: () => ({ state: 'connected', account: { displayName: 'Lumi', username: 'lumi' } }),
      getRecipient: () => ({ displayName: 'Ravi', username: 'ravi' }),
      sendConfirmed
    }
    const pending = createPendingStore(await createStore(), { telegram })
    const raw = {
      id: 'telegram-proposal', toolName: 'send_telegram_message', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'trusted-result', message: 'Interview is tomorrow.' }
    }
    const preview = await pending.create(raw)
    raw.arguments.recipientResultId = 'altered-result'
    raw.arguments.message = 'Altered after proposal.'
    expect(preview).toMatchObject({ actionType: 'send_telegram_message', account: { displayName: 'Lumi' }, recipient: { displayName: 'Ravi' }, message: 'Interview is tomorrow.' })
    expect(sendConfirmed).not.toHaveBeenCalled()
    await pending.approve(preview.approvalId)
    expect(sendConfirmed).toHaveBeenCalledWith(undefined, 'trusted-result', 'Interview is tomorrow.')

    const pendingLogout = await pending.create({
      id: 'telegram-pending', toolName: 'send_telegram_message', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'trusted-result', message: 'Do not send.' }
    })
    pending.clearTelegram()
    await expect(pending.approve(pendingLogout.approvalId)).rejects.toThrow(/invalid/i)
    expect(sendConfirmed).toHaveBeenCalledTimes(1)
  })

  it('uses the validated URL for the sole eventual external open', async () => {
    const pending = createPendingStore(await createStore())
    const raw = {
      id: 'url-proposal', toolName: 'open_url', reason: 'Open it.', requiresConfirmation: true,
      arguments: { url: 'https://example.com/interview' }
    }
    const preview = await pending.create(raw)
    raw.arguments.url = 'https://attacker.invalid/'
    expect(preview).toMatchObject({ actionType: 'open_url', domain: 'example.com' })
    await pending.approve(preview.approvalId)
    expect(openExternal).toHaveBeenCalledWith('https://example.com/interview')
  })
})
