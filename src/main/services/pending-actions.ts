import {
  parseToolProposal,
  type PendingActionPreview,
  type ToolExecutionResult,
  type ToolProposal
} from '../../shared/contracts'
import { LocalStore } from './store'
import {
  TelegramAttachmentDeliveryError,
  TelegramService,
  type TrustedTelegramRecipientSnapshot
} from './telegram'
import type { DroppedFileLookup } from './dropped-files'
import { createResultThumbnails } from './thumbnails'
import { DOCUMENT_ANALYSIS_MESSAGE, executeConfirmedTool } from './tools'

/** Shown when a temporary dropped record lapsed or changed before an action ran. */
const DROPPED_GONE = 'That dropped file is no longer available. Drop it again to use it.'
import {
  revalidateTrustedAttachment,
  validateTrustedAttachment,
  type TrustedAttachmentSnapshot
} from './attachment-validation'

const DEFAULT_TTL_MS = 2 * 60 * 1_000

type PendingState = 'ready' | 'executing' | 'executed' | 'cancelled' | 'expired' | 'failed' | 'uncertain'

interface PendingAction {
  proposal: ToolProposal
  preview: PendingActionPreview
  state: PendingState
  attachment?: Readonly<TrustedAttachmentSnapshot>
  recipientSnapshot?: Readonly<TrustedTelegramRecipientSnapshot>
}

export class PendingActionStore {
  private readonly actions = new Map<string, PendingAction>()

  constructor(
    private readonly store: LocalStore,
    private readonly telegram: TelegramService,
    private readonly validateCapture: (proposal: ToolProposal) => void,
    private readonly now: () => number = () => Date.now(),
    private readonly ttlMs = DEFAULT_TTL_MS,
    private readonly createThumbnails: typeof createResultThumbnails = createResultThumbnails,
    private readonly validateAttachment: typeof validateTrustedAttachment = validateTrustedAttachment,
    private readonly revalidateAttachment: typeof revalidateTrustedAttachment = revalidateTrustedAttachment,
    /** Absent in tests that only exercise approved-folder results. */
    private readonly droppedFiles?: DroppedFileLookup
  ) {}

  async create(rawProposal: unknown): Promise<PendingActionPreview> {
    const proposal = parseToolProposal(rawProposal)
    this.validateCapture(proposal)
    this.pruneExpired()
    const createdAt = new Date(this.now()).toISOString()
    const expiresAt = new Date(this.now() + this.ttlMs).toISOString()
    const approvalId = crypto.randomUUID()
    const attachment = proposal.toolName === 'send_telegram_attachment'
      ? await this.validateAttachment(this.store, proposal.arguments.fileResultId, undefined, this.droppedFiles)
      : undefined
    const recipientSnapshot = proposal.toolName === 'send_telegram_attachment'
      ? this.telegram.snapshotRecipient(proposal.arguments.recipientResultId)
      : undefined
    if (proposal.toolName === 'send_telegram_attachment' && !recipientSnapshot) {
      throw new Error('That Telegram recipient is no longer available. Search again first.')
    }
    if (recipientSnapshot?.kind === 'channel') {
      throw new Error('LifeLens can send personal messages and groups, not channel posts.')
    }
    const preview = await this.createTrustedPreview(proposal, approvalId, createdAt, expiresAt, attachment, recipientSnapshot)
    this.actions.set(approvalId, { proposal, preview, state: 'ready', attachment, recipientSnapshot })
    return preview
  }

  async approve(approvalId: unknown): Promise<ToolExecutionResult> {
    const action = this.getReadyAction(approvalId)
    action.state = 'executing'
    try {
      const result = action.proposal.toolName === 'send_telegram_message'
        ? await this.executeTelegram(action.proposal)
        : action.proposal.toolName === 'send_telegram_attachment'
          ? await this.executeTelegramAttachment(action)
          : await executeConfirmedTool(this.store, action.proposal, this.droppedFiles)
      action.state = result.ok ? 'executed' : 'failed'
      return result
    } catch (error) {
      action.state = error instanceof TelegramAttachmentDeliveryError && error.uncertain ? 'uncertain' : 'failed'
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
    this.clearByTool('send_telegram_attachment')
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
    expiresAt: string,
    attachment?: Readonly<TrustedAttachmentSnapshot>,
    recipientSnapshot?: Readonly<TrustedTelegramRecipientSnapshot>
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
        if (this.droppedFiles?.wasInvalidated(proposal.arguments.resultId)) throw new Error(DROPPED_GONE)
        // The card must never imply folder trust a dropped file does not have.
        const dropped = this.droppedFiles?.snapshot(proposal.arguments.resultId)
        if (dropped) {
          if (!(await this.droppedFiles?.resolve(proposal.arguments.resultId))) {
            throw new Error(DROPPED_GONE)
          }
          return {
            ...common,
            actionType: proposal.toolName,
            fileName: dropped.fileName,
            relativePath: dropped.fileName,
            folderLabel: 'Dropped file',
            source: 'dropped-file'
          }
        }
        const result = await this.store.getSearchResult(proposal.arguments.resultId)
        if (!result) throw new Error('That file is not a result from an approved search. Search again first.')
        const root = await this.store.getDocumentRoot(result.rootId)
        if (!root) throw new Error('The folder that produced this result is no longer approved.')
        return {
          ...common,
          actionType: proposal.toolName,
          fileName: result.name,
          relativePath: result.relativePath,
          folderLabel: root.label,
          source: 'approved-folder'
        }
      }
      case 'open_url': {
        const parsed = new URL(proposal.arguments.url)
        return { ...common, actionType: proposal.toolName, url: parsed.toString(), domain: parsed.hostname }
      }
      case 'save_context':
        return { ...common, actionType: proposal.toolName, label: proposal.arguments.label, summary: proposal.arguments.sourceContext.summary }
      case 'analyze_photo': {
        if (this.droppedFiles?.wasInvalidated(proposal.arguments.resultId)) throw new Error(DROPPED_GONE)
        const droppedPhoto = this.droppedFiles?.snapshot(proposal.arguments.resultId)
        if (droppedPhoto) {
          if (droppedPhoto.mediaKind !== 'photo') throw new Error(DOCUMENT_ANALYSIS_MESSAGE)
          if (!(await this.droppedFiles?.resolve(proposal.arguments.resultId))) throw new Error(DROPPED_GONE)
          const [droppedPreview] = await this.createThumbnails(
            this.store,
            [proposal.arguments.resultId],
            undefined,
            this.droppedFiles
          )
          return {
            ...common,
            actionType: proposal.toolName,
            fileName: droppedPhoto.fileName,
            relativePath: droppedPhoto.fileName,
            folderLabel: 'Dropped file',
            source: 'dropped-file',
            question: proposal.arguments.question ?? 'What is in this photo?',
            previewDataUrl: droppedPreview?.status === 'ok' ? droppedPreview.dataUrl : undefined
          }
        }
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
      case 'send_telegram_attachment': {
        const account = this.telegram.getStatus().account
        if (!account || !attachment || !recipientSnapshot) {
          throw new Error('Telegram is not connected or that trusted attachment is no longer available.')
        }
        const [preview] = attachment.mediaKind === 'photo'
          ? await this.createThumbnails(this.store, [attachment.fileResultId], undefined, this.droppedFiles)
          : []
        return {
          ...common,
          actionType: proposal.toolName,
          source: this.droppedFiles?.snapshot(attachment.fileResultId) ? 'dropped-file' : 'approved-folder',
          account,
          recipient: {
            displayName: recipientSnapshot.displayName,
            username: recipientSnapshot.username,
            kind: recipientSnapshot.kind
          },
          fileName: attachment.fileName,
          mediaKind: attachment.mediaKind,
          fileSizeBytes: attachment.sizeBytes,
          fileTypeLabel: attachment.fileTypeLabel,
          caption: proposal.arguments.caption,
          previewDataUrl: preview?.status === 'ok' ? preview.dataUrl : undefined
        }
      }
    }
  }

  private async executeTelegram(proposal: ToolProposal<'send_telegram_message'>): Promise<ToolExecutionResult> {
    await this.telegram.sendConfirmed(proposal.callId, proposal.arguments.recipientResultId, proposal.arguments.message)
    return { ok: true, message: 'Telegram message sent.', telegramSent: true }
  }

  private async executeTelegramAttachment(action: PendingAction): Promise<ToolExecutionResult> {
    if (action.proposal.toolName !== 'send_telegram_attachment' || !action.attachment || !action.recipientSnapshot) {
      throw new Error('That Telegram attachment approval is no longer available.')
    }
    // Second check, at approval: a dropped record that expired or changed since
    // the card was rendered fails here and nothing is sent.
    const attachment = await this.revalidateAttachment(this.store, action.attachment, undefined, this.droppedFiles)
    await this.telegram.sendConfirmedAttachment(action.proposal.callId, action.recipientSnapshot, {
      canonicalPath: attachment.canonicalPath,
      fileName: attachment.fileName,
      sizeBytes: attachment.sizeBytes,
      mediaKind: attachment.mediaKind,
      caption: action.proposal.arguments.caption
    })
    return {
      ok: true,
      message: `Telegram ${attachment.mediaKind === 'photo' ? 'photo' : 'document'} sent.`,
      telegramSent: true
    }
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
