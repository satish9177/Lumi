export type PendingApprovalOutcome<TPending, TResult> =
  | { status: 'missing' }
  | { status: 'succeeded'; pending: TPending; result: TResult }
  | { status: 'failed'; pending: TPending; error: unknown }

export type PendingDismissalOutcome<TPending> =
  | { status: 'missing' }
  | { status: 'dismissed'; pending: TPending }
  | { status: 'terminal'; pending: TPending; error: unknown }
  | { status: 'failed'; pending: TPending; error: unknown }

interface LocalPendingState<TPending> {
  pendingByApprovalId: Map<string, TPending>
  clearCard: (approvalId: string) => void
}

/**
 * Owns the renderer-side single-use guard. Main remains authoritative, but a
 * settled main action must never leave a clickable stale card in the UI.
 */
export async function approvePendingRendererAction<TPending, TResult>(
  approvalId: string,
  local: LocalPendingState<TPending>,
  approve: (approvalId: string) => Promise<TResult>,
  setConfirming: (confirming: boolean) => void
): Promise<PendingApprovalOutcome<TPending, TResult>> {
  const pending = local.pendingByApprovalId.get(approvalId)
  if (!pending) {
    local.clearCard(approvalId)
    return { status: 'missing' }
  }

  setConfirming(true)
  try {
    const result = await approve(approvalId)
    clearLocalPendingAction(approvalId, local)
    return { status: 'succeeded', pending, result }
  } catch (error) {
    clearLocalPendingAction(approvalId, local)
    return { status: 'failed', pending, error }
  } finally {
    setConfirming(false)
  }
}

export async function dismissPendingRendererAction<TPending>(
  approvalId: string,
  local: LocalPendingState<TPending>,
  cancel: (approvalId: string) => Promise<void>
): Promise<PendingDismissalOutcome<TPending>> {
  const pending = local.pendingByApprovalId.get(approvalId)
  if (!pending) {
    local.clearCard(approvalId)
    return { status: 'missing' }
  }

  try {
    await cancel(approvalId)
    clearLocalPendingAction(approvalId, local)
    return { status: 'dismissed', pending }
  } catch (error) {
    if (isAlreadyTerminalApprovalError(error)) {
      clearLocalPendingAction(approvalId, local)
      return { status: 'terminal', pending, error }
    }
    return { status: 'failed', pending, error }
  }
}

export function isAlreadyTerminalApprovalError(error: unknown): boolean {
  const message = messageFrom(error)
  return /already handled|invalid or no longer available|approval expired|cannot be used again/i.test(message)
}

function clearLocalPendingAction<TPending>(approvalId: string, local: LocalPendingState<TPending>): void {
  local.pendingByApprovalId.delete(approvalId)
  local.clearCard(approvalId)
}
import { messageFrom } from './error-message'
