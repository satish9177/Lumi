import { useEffect, useId, useRef } from 'react'
import { focusableWithin, nextTrappedFocus } from '../focus-trap'
import './components.css'

/**
 * The wording of a yes-or-no question.
 *
 * The title *is* the question, the body says what actually happens including
 * anything that cannot be undone, and the confirm label names the action so the
 * button reads on its own — "Delete", never "OK".
 */
export interface ConfirmContent {
  readonly title: string
  readonly body?: string
  readonly confirmLabel: string
  /** Marks the action as unrecoverable: red confirm, and Cancel takes focus. */
  readonly destructive?: boolean
}

/** A question together with what to run if the answer is yes. */
export interface ConfirmRequest {
  readonly content: ConfirmContent
  readonly onConfirm: () => void
}

/** How any surface asks a question without reaching for `window.confirm`. */
export type RequestConfirmation = (content: ConfirmContent, onConfirm: () => void) => void

export interface ConfirmDialogProps {
  content: ConfirmContent
  onConfirm: () => void
  onCancel: () => void
}

/**
 * The in-app replacement for `window.confirm`.
 *
 * A native confirm opens a second operating-system window, which for a
 * frameless always-on-top companion means the question lands outside Lumi's own
 * frame, in someone else's visual language, and blocks the panel until it is
 * answered. This keeps the question on Lumi's surface and answers it without
 * blocking anything.
 *
 * This is not the approval surface for anything Lumi *does* on the user's
 * behalf — that remains `ToolConfirmationCard`, whose content main authors. This
 * only guards local settings choices the user makes for themselves.
 */
export function ConfirmDialog({ content, onConfirm, onCancel }: ConfirmDialogProps) {
  const headingId = useId()
  const bodyId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const destructive = content.destructive ?? false

  /*
   * The dialog owns focus while it is open: it takes focus on mount, keeps Tab
   * inside itself, and hands focus back to whatever opened it on close. Escape
   * is deliberately not handled here — the app owns the one Escape chain, and a
   * second listener on the same target would race it.
   */
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    // A destructive question opens on Cancel, so a reflexive Enter or Space
    // cannot delete something the user has not read yet.
    const initial = destructive ? cancelRef.current : confirmRef.current
    initial?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !dialogRef.current) {
        return
      }
      const target = nextTrappedFocus(
        focusableWithin(dialogRef.current),
        document.activeElement as HTMLElement | null,
        event.shiftKey
      )
      if (target) {
        event.preventDefault()
        target.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      opener?.focus()
    }
  }, [destructive])

  return (
    <div className="confirm-scrim">
      <div
        ref={dialogRef}
        className="confirm-dialog"
        // alertdialog, not dialog: this interrupts to ask about something the
        // user cannot take back, so it is announced rather than merely entered.
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={headingId}
        aria-describedby={content.body ? bodyId : undefined}
      >
        <h2 id={headingId} className="confirm-title">{content.title}</h2>
        {content.body && <p id={bodyId} className="confirm-body">{content.body}</p>}
        <div className="confirm-actions">
          <button ref={cancelRef} className="secondary-button" type="button" onClick={onCancel}>Cancel</button>
          <button
            ref={confirmRef}
            className={destructive ? 'primary-button confirm-destructive' : 'primary-button'}
            type="button"
            onClick={onConfirm}
          >
            {content.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
