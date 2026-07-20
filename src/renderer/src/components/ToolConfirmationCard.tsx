import { useId } from 'react'
import type { PendingActionPreview, TrustedSourceKind } from '../../../shared/contracts'
import './components.css'

export interface ToolConfirmationCardProps {
  action: PendingActionPreview
  isConfirming?: boolean
  onConfirm: (approvalId: string) => void | Promise<void>
  onDismiss: (approvalId: string) => void | Promise<void>
}

/** The only approval surface. Details come from the immutable main-process action. */
export function ToolConfirmationCard({ action, isConfirming = false, onConfirm, onDismiss }: ToolConfirmationCardProps) {
  const headingId = useId()
  const descriptionId = useId()
  const label = actionLabel(action)

  return (
    <article className="lifelens-tool-confirmation-card" aria-labelledby={headingId} aria-describedby={descriptionId}>
      <p className="lifelens-card-eyebrow">READY TO {label.toUpperCase()}</p>
      <h2 id={headingId} className="lifelens-card-heading">{headingLabel(action)}</h2>
      <div id={descriptionId}><ActionDetails action={action} /></div>
      <p className="lifelens-confirmation-notice">LifeLens will act only if you choose {label}.</p>
      <div className="lifelens-confirmation-actions">
        <button className="lifelens-confirm-button" type="button" disabled={isConfirming} onClick={() => void onConfirm(action.approvalId)}>
          {isConfirming ? pendingLabel(action) : label}
        </button>
        <button className="lifelens-dismiss-button" type="button" disabled={isConfirming} onClick={() => void onDismiss(action.approvalId)}>Cancel</button>
      </div>
    </article>
  )
}

function ActionDetails({ action }: { action: PendingActionPreview }) {
  switch (action.actionType) {
    case 'analyze_photo':
      return (
        <>
          {action.previewDataUrl && (
            <img className="lifelens-photo-preview" src={action.previewDataUrl} alt={`Preview of ${action.fileName}`} />
          )}
          <Details rows={[
            ['Photo', action.fileName],
            ...sourceRows(action.source, action.relativePath, action.folderLabel),
            ['Question', action.question]
          ]} />
          <p className="lifelens-upload-notice">
            This one photo will be sent to OpenAI so Lumi can answer. No other photo leaves your computer.
          </p>
        </>
      )
    case 'send_telegram_message':
      return <Details rows={[
        ['Account', accountLabel(action.account)],
        ['Recipient', accountLabel(action.recipient)],
        ['Message', action.message]
      ]} />
    case 'send_telegram_attachment':
      return (
        <>
          {action.mediaKind === 'photo' && action.previewDataUrl
            ? <img className="lifelens-photo-preview" src={action.previewDataUrl} alt={`Preview of ${action.fileName}`} />
            : action.mediaKind === 'document' && <div className="lifelens-document-icon" aria-hidden="true">DOC</div>}
          <Details rows={[
            ['From', accountLabel(action.account)],
            ['To', accountLabel(action.recipient)],
            ['File', action.fileName],
            ['Type and size', `${action.fileTypeLabel} · ${formatFileSize(action.fileSizeBytes)}`],
            ...(action.source === 'dropped-file' ? [['Source', 'Dropped file'] as [string, string]] : []),
            ['Caption', action.caption ?? 'No caption']
          ]} />
          <p className="lifelens-upload-notice">Only this confirmed file will be sent to Telegram. It is not sent to OpenAI.</p>
        </>
      )
    case 'create_reminder':
      return <Details rows={[
        ['Reminder', action.title],
        ['When', formatDateTime(action.dueAt)],
        ['Source context', action.sourceContextSummary]
      ]} />
    case 'search_documents':
      return <Details rows={[['Approved folder', action.folderLabel], ['Search for', action.query]]} />
    case 'open_file':
      return <Details rows={[['File', action.fileName], ...sourceRows(action.source, action.relativePath, action.folderLabel)]} />
    case 'open_url':
      return <Details rows={[['Destination', action.domain], ['URL', action.url]]} />
    case 'save_context':
      return <Details rows={[['Label', action.label], ['Summary', action.summary]]} />
  }
}

/**
 * Describes where a file came from without ever implying folder trust it does
 * not have. A dropped file has no approved folder, so it is not given one.
 */
function sourceRows(
  source: TrustedSourceKind | undefined,
  relativePath: string,
  folderLabel: string
): Array<[string, string]> {
  return source === 'dropped-file'
    ? [['Source', 'Dropped file — temporary, not an approved folder']]
    : [['Location', relativePath], ['Approved folder', folderLabel]]
}

function Details({ rows }: { rows: Array<[string, string]> }) {
  return <dl className="lifelens-action-details">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}

function actionLabel(action: PendingActionPreview): string {
  switch (action.actionType) {
    case 'analyze_photo': return 'Analyse photo'
    case 'send_telegram_message': return 'Send message'
    case 'send_telegram_attachment': return action.mediaKind === 'photo' ? 'Send photo' : 'Send document'
    case 'create_reminder': return 'Create reminder'
    case 'search_documents': return 'Search folder'
    case 'open_file': return 'Open file'
    case 'open_url': return 'Open link'
    case 'save_context': return 'Save context'
  }
}

function pendingLabel(action: PendingActionPreview): string {
  switch (action.actionType) {
    case 'send_telegram_attachment': return action.mediaKind === 'photo' ? 'Sending photo…' : 'Sending document…'
    case 'analyze_photo': return 'Sending photo…'
    case 'send_telegram_message': return 'Sending…'
    case 'create_reminder': return 'Creating…'
    case 'search_documents': return 'Searching…'
    case 'open_file': return 'Opening…'
    case 'open_url': return 'Opening…'
    case 'save_context': return 'Saving…'
  }
}

function headingLabel(action: PendingActionPreview): string {
  return action.actionType === 'send_telegram_attachment'
    ? `Send Telegram ${action.mediaKind}`
    : actionLabel(action)
}

function accountLabel(value: { displayName: string; username?: string }): string {
  return value.username ? `${value.displayName} (@${value.username})` : value.displayName
}

function formatDateTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
