import { useId } from 'react'
import type { SourceContext, ToolProposal } from '../../../shared/contracts'
import './components.css'

export interface ToolConfirmationCardProps {
  proposal: ToolProposal
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

      <ToolProposalDetails proposal={proposal} />

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

function ToolProposalDetails({ proposal }: Pick<ToolConfirmationCardProps, 'proposal'>) {
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
      return (
        <dl className="lifelens-action-details">
          <div><dt>Approved folder</dt><dd>{proposal.arguments.rootId}</dd></div>
          <div><dt>Search for</dt><dd>{proposal.arguments.query}</dd></div>
        </dl>
      )
    case 'open_file':
      return (
        <dl className="lifelens-action-details">
          <div><dt>Selected file</dt><dd>{proposal.arguments.resultId}</dd></div>
        </dl>
      )
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
  }
}

function formatDateTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}
