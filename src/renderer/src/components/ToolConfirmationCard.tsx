import { useId } from 'react'
import type { ApprovedDocumentRoot, DocumentSearchResult, SourceContext, TelegramAccount, TelegramRecipient, ToolProposal } from '../../../shared/contracts'
import './components.css'

export interface ToolConfirmationCardProps {
  proposal: ToolProposal
  approvedRoots?: ApprovedDocumentRoot[]
  searchResults?: DocumentSearchResult[]
  telegramAccount?: TelegramAccount
  telegramRecipients?: TelegramRecipient[]
  isConfirming?: boolean
  onConfirm: (proposal: ToolProposal) => void | Promise<void>
  onDismiss: () => void
}

/**
 * A deliberately explicit approval surface. This component only reports the
 * user's intent through callbacks; execution remains in the main process.
 */
export function ToolConfirmationCard({
  proposal,
  approvedRoots = [],
  searchResults = [],
  telegramAccount,
  telegramRecipients = [],
  isConfirming = false,
  onConfirm,
  onDismiss
}: ToolConfirmationCardProps) {
  const headingId = useId()
  const descriptionId = useId()
  const actionLabel = confirmationLabel(proposal)

  return (
    <article
      className="lifelens-tool-confirmation-card"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
    >
      <p className="lifelens-card-eyebrow">CONFIRM ACTION</p>
      <h2 id={headingId} className="lifelens-card-heading">{actionLabel}</h2>
      <p id={descriptionId} className="lifelens-tool-reason">{proposal.reason}</p>

      <ToolProposalDetails proposal={proposal} approvedRoots={approvedRoots} searchResults={searchResults} telegramAccount={telegramAccount} telegramRecipients={telegramRecipients} />

      <p className="lifelens-confirmation-notice">
        LifeLens will not run this action until you select {actionLabel}.
      </p>
      <div className="lifelens-confirmation-actions">
        <button
          className="lifelens-confirm-button"
          type="button"
          disabled={isConfirming}
          onClick={() => void onConfirm(proposal)}
        >
          {isConfirming ? 'Working...' : actionLabel}
        </button>
        <button className="lifelens-dismiss-button" type="button" disabled={isConfirming} onClick={onDismiss}>
          Not now
        </button>
      </div>
    </article>
  )
}

function ToolProposalDetails({
  proposal,
  approvedRoots = [],
  searchResults = [],
  telegramAccount,
  telegramRecipients = []
}: Pick<ToolConfirmationCardProps, 'proposal' | 'approvedRoots' | 'searchResults' | 'telegramAccount' | 'telegramRecipients'>) {
  switch (proposal.toolName) {
    case 'create_reminder':
      return (
        <>
          <dl className="lifelens-action-details">
            <div><dt>Reminder</dt><dd>{proposal.arguments.title}</dd></div>
            <div><dt>When</dt><dd>{formatDateTime(proposal.arguments.dueAt)}</dd></div>
          </dl>
          <SourceContextPreview context={proposal.arguments.sourceContext} />
        </>
      )
    case 'search_documents':
      {
        const root = approvedRoots.find((candidate) => candidate.id === proposal.arguments.rootId)
      return (
        <dl className="lifelens-action-details">
          <div><dt>Approved folder</dt><dd>{root?.label ?? 'Unavailable approved folder'}</dd></div>
          <div><dt>Search for</dt><dd>{proposal.arguments.query}</dd></div>
        </dl>
      )
      }
    case 'open_file':
      {
        const result = searchResults.find((candidate) => candidate.id === proposal.arguments.resultId)
      return (
        <dl className="lifelens-action-details">
          <div><dt>Selected file</dt><dd>{result?.name ?? 'Unavailable selected file'}</dd></div>
          {result && <div><dt>Location</dt><dd>{result.relativePath}</dd></div>}
        </dl>
      )
      }
    case 'open_url':
      return (
        <dl className="lifelens-action-details">
          <div><dt>Website</dt><dd><code dir="ltr">{proposal.arguments.url}</code></dd></div>
        </dl>
      )
    case 'save_context':
      return (
        <>
          <dl className="lifelens-action-details">
            <div><dt>Context label</dt><dd>{proposal.arguments.label}</dd></div>
          </dl>
          <SourceContextPreview context={proposal.arguments.sourceContext} />
        </>
      )
    case 'send_telegram_message':
      {
        const recipient = telegramRecipients.find((candidate) => candidate.resultId === proposal.arguments.recipientResultId)
      return (
        <dl className="lifelens-action-details">
          <div><dt>Account</dt><dd>{telegramAccount ? accountLabel(telegramAccount) : 'Connected Telegram account'}</dd></div>
          <div><dt>Recipient</dt><dd>{recipient ? accountLabel(recipient) : 'Unavailable selected recipient'}</dd></div>
          <div><dt>Message</dt><dd>{proposal.arguments.message}</dd></div>
        </dl>
      )
      }
  }
}

function SourceContextPreview({ context }: { context: SourceContext }) {
  return (
    <section className="lifelens-source-context" aria-label="Source context to retain">
      <h3>Why this exists</h3>
      <p>{context.summary}</p>
      {context.signals.length > 0 && (
        <ul>
          {context.signals.slice(0, 3).map((signal, index) => (
            <li key={`${signal.kind}-${signal.value}-${index}`}>
              <span>{signal.label}:</span> {signal.value}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function confirmationLabel(proposal: ToolProposal): string {
  switch (proposal.toolName) {
    case 'create_reminder':
      return 'Create reminder'
    case 'search_documents':
      return 'Search approved folder'
    case 'open_file':
      return 'Open selected file'
    case 'open_url':
      return 'Open website'
    case 'save_context':
      return 'Save context'
    case 'send_telegram_message':
      return 'Send Telegram message'
  }
}

function accountLabel(value: { displayName: string; username?: string }): string {
  return value.username ? `${value.displayName} (@${value.username})` : value.displayName
}

function formatDateTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}
