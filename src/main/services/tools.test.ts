import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  Notification: { isSupported: () => false },
  shell: { openExternal: vi.fn(), openPath: vi.fn() }
}))

import { LocalStore } from './store'
import { executeConfirmedTool, executeToolAfterConfirmation } from './tools'

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
