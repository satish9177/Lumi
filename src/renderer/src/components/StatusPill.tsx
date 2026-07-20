import type { StatusDescriptor } from '../status'

/**
 * The header's single status signal, replacing the status row, the mode badge,
 * and the scattered per-button busy text.
 *
 * The dot is decorative — the label always carries the state in words, so
 * colour is never the only channel.
 */
export function StatusPill({ status }: { status: StatusDescriptor }) {
  return (
    <span className={`status-pill tone-${status.tone}`}>
      <span className="status-pill-dot" aria-hidden="true" />
      <span className="status-pill-label">{status.label}</span>
      {status.suffix && <span className="status-pill-suffix">{status.suffix}</span>}
    </span>
  )
}
