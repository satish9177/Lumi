import {
  parseToolProposal,
  type PendingActionPreview,
  type ToolExecutionResult,
  type ToolProposal
} from '../../shared/contracts'
import { LocalStore } from './store'
import { TelegramService } from './telegram'
import { createResultThumbnails } from './thumbnails'
import { executeConfirmedTool } from './tools'

const DEFAULT_TTL_MS = 2 * 60 * 1_000

type PendingState = 'ready' | 'executing' | 'executed' | 'cancelled' | 'expired' | 'failed'

interface PendingAction {
  proposal: ToolProposal
  preview: PendingActionPreview
  state: PendingState
}

export class PendingActionStore {
  private readonly actions = new Map<string, PendingAction>()

  constructor(
    private readonly store: LocalStore,
    private readonly telegram: TelegramService,
    private readonly validateCapture: (proposal: ToolProposal) => void,
    private readonly now: () => number = () => Date.now(),
    private readonly ttlMs = DEFAULT_TTL_MS,
    private readonly createThumbnails: typeof createResultThumbnails = createResultThumbnails
  ) {}

  async create(rawProposal: unknown): Promise<PendingActionPreview> {
    const proposal = parseToolProposal(rawProposal)
    this.validateCapture(proposal)
    this.pruneExpired()
    const createdAt = new Date(this.now()).toISOString()
    const expiresAt = new Date(this.now() + this.ttlMs).toISOString()
    const approvalId = crypto.randomUUID()
    const preview = await this.createTrustedPreview(proposal, approvalId, createdAt, expiresAt)
    this.actions.set(approvalId, { proposal, preview, state: 'ready' })
    return preview
  }

  async approve(approvalId: unknown): Promise<ToolExecutionResult> {
    const action = this.getReadyAction(approvalId)
    action.state = 'executing'
    try {
      const result = action.proposal.toolName === 'send_telegram_message'
        ? await this.executeTelegram(action.proposal)
        : await executeConfirmedTool(this.store, action.proposal)
      action.state = result.ok ? 'executed' : 'failed'
      return result
    } catch (error) {
      action.state = 'failed'
      throw error
    }
  }

  cancel(approvalId: unknown): void {
    const action = this.getReadyAction(approvalId)
    action.state = 'cancelled'
  }

  clearAll(): void {
    this.actions.clear()
  }

  clearTelegram(): void {
    this.clearByTool('send_telegram_message')
  }

  /** Called when the Realtime session ends: no image may outlive the session. */
  clearPhotoAnalysis(): void {
    this.clearByTool('analyze_photo')
  }

  private clearByTool(toolName: ToolProposal['toolName']): void {
    for (const [approvalId, action] of this.actions) {
      if (action.proposal.toolName === toolName) {
        this.actions.delete(approvalId)
      }
    }
  }

  private async createTrustedPreview(
    proposal: ToolProposal,
    approvalId: string,
    createdAt: string,
    expiresAt: string
  ): Promise<PendingActionPreview> {
    const common = { approvalId, createdAt, expiresAt }
    switch (proposal.toolName) {
      case 'create_reminder':
        return {
          ...common,
          actionType: proposal.toolName,
          title: proposal.arguments.title,
          dueAt: proposal.arguments.dueAt,
          sourceContextSummary: proposal.arguments.sourceContext.summary
        }
      case 'search_documents': {
        const roots = await this.store.listDocumentRoots()
        if (roots.length === 0) throw new Error('No folder is approved for search yet. Approve a folder first.')
        return {
          ...common,
          actionType: proposal.toolName,
          folderLabel: roots.map((root) => root.label).join(', '),
          query: proposal.arguments.queryTerms
        }
      }
      case 'open_file': {
        const result = await this.store.getSearchResult(proposal.arguments.resultId)
        if (!result) throw new Error('That file is not a result from an approved search. Search again first.')
        const root = await this.store.getDocumentRoot(result.rootId)
        if (!root) throw new Error('The folder that produced this result is no longer approved.')
        return {
          ...common,
          actionType: proposal.toolName,
          fileName: result.name,
          relativePath: result.relativePath,
          folderLabel: root.label
        }
      }
      case 'open_url': {
        const parsed = new URL(proposal.arguments.url)
        return { ...common, actionType: proposal.toolName, url: parsed.toString(), domain: parsed.hostname }
      }
      case 'save_context':
        return { ...common, actionType: proposal.toolName, label: proposal.arguments.label, summary: proposal.arguments.sourceContext.summary }
      case 'analyze_photo': {
        const result = await this.store.getSearchResult(proposal.arguments.resultId)
        if (!result) throw new Error('That photo is not a result from an approved search. Search again first.')
        const root = await this.store.getDocumentRoot(result.rootId)
        if (!root) throw new Error('The folder that produced this photo is no longer approved.')
        // The preview is generated here from trusted state so the card cannot
        // show a filename or image the renderer chose.
        const [preview] = await this.createThumbnails(this.store, [result.id])
        return {
          ...common,
          actionType: proposal.toolName,
          fileName: result.name,
          relativePath: result.relativePath,
          folderLabel: root.label,
          question: proposal.arguments.question ?? 'What is in this photo?',
          previewDataUrl: preview?.status === 'ok' ? preview.dataUrl : undefined
        }
      }
      case 'send_telegram_message': {
        const account = this.telegram.getStatus().account
        const recipient = this.telegram.getRecipient(proposal.arguments.recipientResultId)
        if (!account || !recipient) throw new Error('That Telegram recipient is no longer available. Search again first.')
        return {
          ...common,
          actionType: proposal.toolName,
          account,
          recipient: { displayName: recipient.displayName, username: recipient.username },
          message: proposal.arguments.message
        }
      }
    }
  }

  private async executeTelegram(proposal: ToolProposal<'send_telegram_message'>): Promise<ToolExecutionResult> {
    await this.telegram.sendConfirmed(proposal.callId, proposal.arguments.recipientResultId, proposal.arguments.message)
    return { ok: true, message: 'Telegram message sent.', telegramSent: true }
  }

  private getReadyAction(approvalId: unknown): PendingAction {
    if (typeof approvalId !== 'string' || approvalId.length === 0 || approvalId.length > 250) {
      throw new Error('That approval is invalid or no longer available.')
    }
    const action = this.actions.get(approvalId)
    if (!action) throw new Error('That approval is invalid or no longer available.')
    if (Date.parse(action.preview.expiresAt) <= this.now()) {
      action.state = 'expired'
    }
    if (action.state !== 'ready') {
      throw new Error(action.state === 'expired'
        ? 'That approval expired. Ask LifeLens to propose the action again.'
        : 'That approval was already handled and cannot be used again.')
    }
    return action
  }

  private pruneExpired(): void {
    for (const [approvalId, action] of this.actions) {
      if (Date.parse(action.preview.expiresAt) <= this.now()) {
        action.state = 'expired'
      }
      if (action.state === 'expired' || action.state === 'cancelled' || action.state === 'executed' || action.state === 'failed') {
        this.actions.delete(approvalId)
      }
    }
  }
}
