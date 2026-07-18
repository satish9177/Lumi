import { useId } from 'react'
import type { Explanation, ExtractedSignal } from '../../../shared/contracts'
import './components.css'

export interface ExplanationCardProps {
  explanation: Explanation
  heading?: string
}

const SIGNAL_SECTIONS: Array<{ kind: ExtractedSignal['kind']; title: string }> = [
  { kind: 'date', title: 'Important dates' },
  { kind: 'link', title: 'Links to review' },
  { kind: 'next_action', title: 'Next actions' }
]

/**
 * Renders a completed screen explanation without initiating navigation or other
 * external work. Links remain visible as text so a future confirmed open_url
 * proposal can own the actual action.
 */
export function ExplanationCard({ explanation, heading = 'Screen explanation' }: ExplanationCardProps) {
  const headingId = useId()

  return (
    <article className="lifelens-explanation-card" aria-labelledby={headingId}>
      <p className="lifelens-card-eyebrow">SCREEN EXPLANATION</p>
      <h2 id={headingId} className="lifelens-card-heading">{heading}</h2>
      <p className="lifelens-explanation-summary">{explanation.summary}</p>

      {SIGNAL_SECTIONS.map(({ kind, title }) => {
        const signals = explanation.signals.filter((signal) => signal.kind === kind)
        if (signals.length === 0) {
          return null
        }

        return (
          <section className="lifelens-signal-section" key={kind} aria-label={title}>
            <h3>{title}</h3>
            <ul>
              {signals.map((signal, index) => (
                <li key={`${signal.value}-${index}`}>
                  <span className="lifelens-signal-label">{signal.label}</span>
                  {kind === 'link' ? (
                    <code className="lifelens-link-value" dir="ltr">{signal.value}</code>
                  ) : (
                    <span>{signal.value}</span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )
      })}
    </article>
  )
}
