import { describe, expect, it, vi } from 'vitest'
import { messageFrom } from './error-message'
import { approvePendingRendererAction, dismissPendingRendererAction } from './pending-action-coordinator'

describe('renderer pending-action coordination', () => {
  it('dismisses failed approval locally, stops loading, and prevents a second approval', async () => {
    const approve = vi.fn(async () => {
      throw new Error("Error invoking remote method 'lifelens:approve-pending-action': Error: Telegram could not complete that request.")
    })
    const harness = createHarness()

    const first = await approvePendingRendererAction('approval-1', harness.local, approve, harness.setConfirming)
    const second = await approvePendingRendererAction('approval-1', harness.local, approve, harness.setConfirming)

    expect(first).toMatchObject({ status: 'failed', error: expect.objectContaining({ message: expect.stringMatching(/could not complete/i) }) })
    if (first.status === 'failed') expect(messageFrom(first.error)).toBe('Telegram could not complete that request.')
    expect(second).toEqual({ status: 'missing' })
    expect(approve).toHaveBeenCalledTimes(1)
    expect(harness.pendingByApprovalId.size).toBe(0)
    expect(harness.visibleApprovalId).toBeUndefined()
    expect(harness.confirmingStates).toEqual([true, false])
  })

  it('dismisses uncertain approval while preserving the exact final notice for the caller', async () => {
    const notice = 'I can’t confirm whether this reached Telegram. Check the chat before trying again.'
    const harness = createHarness()
    const outcome = await approvePendingRendererAction(
      'approval-1',
      harness.local,
      async () => { throw new Error(`Error invoking remote method 'lifelens:approve-pending-action': Error: ${notice}`) },
      harness.setConfirming
    )

    expect(outcome.status).toBe('failed')
    if (outcome.status === 'failed') expect(messageFrom(outcome.error)).toBe(notice)
    expect(harness.visibleApprovalId).toBeUndefined()
    expect(harness.pendingByApprovalId.size).toBe(0)
  })

  it('closes a terminal action locally when main refuses cancellation', async () => {
    const harness = createHarness()
    const outcome = await dismissPendingRendererAction(
      'approval-1',
      harness.local,
      async () => { throw new Error("Error invoking remote method 'lifelens:cancel-pending-action': Error: That approval was already handled and cannot be used again.") }
    )

    expect(outcome.status).toBe('terminal')
    expect(harness.visibleApprovalId).toBeUndefined()
    expect(harness.pendingByApprovalId.size).toBe(0)
  })

  it('retains the card for unexpected cancellation IPC errors', async () => {
    const harness = createHarness()
    const outcome = await dismissPendingRendererAction(
      'approval-1',
      harness.local,
      async () => { throw new Error("Error invoking remote method 'lifelens:cancel-pending-action': Error: IPC transport unavailable.") }
    )

    expect(outcome.status).toBe('failed')
    if (outcome.status === 'failed') expect(messageFrom(outcome.error)).toBe('IPC transport unavailable.')
    expect(harness.visibleApprovalId).toBe('approval-1')
    expect(harness.pendingByApprovalId.size).toBe(1)
  })

  it('keeps successful approval and ready-state cancellation behavior', async () => {
    const approved = createHarness()
    const approval = await approvePendingRendererAction(
      'approval-1', approved.local, async () => ({ ok: true }), approved.setConfirming
    )
    expect(approval).toMatchObject({ status: 'succeeded', result: { ok: true } })
    expect(approved.visibleApprovalId).toBeUndefined()

    const cancelled = createHarness()
    const cancel = vi.fn(async () => undefined)
    const dismissal = await dismissPendingRendererAction('approval-1', cancelled.local, cancel)
    expect(dismissal.status).toBe('dismissed')
    expect(cancel).toHaveBeenCalledOnce()
    expect(cancelled.visibleApprovalId).toBeUndefined()
  })
})

function createHarness() {
  const pendingByApprovalId = new Map([['approval-1', { proposal: 'trusted' }]])
  let visibleApprovalId: string | undefined = 'approval-1'
  const confirmingStates: boolean[] = []
  return {
    pendingByApprovalId,
    get visibleApprovalId() { return visibleApprovalId },
    confirmingStates,
    setConfirming: (confirming: boolean) => { confirmingStates.push(confirming) },
    local: {
      pendingByApprovalId,
      clearCard: (approvalId: string) => {
        if (visibleApprovalId === approvalId) visibleApprovalId = undefined
      }
    }
  }
}
