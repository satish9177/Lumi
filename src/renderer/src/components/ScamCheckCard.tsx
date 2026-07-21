import { useId } from 'react'
import type { ScamCheckAssessment, ScamSaferStep } from '../../../shared/contracts'
import { COPY } from '../copy'
import './components.css'

export interface ScamCheckCardProps {
  assessment: ScamCheckAssessment
}

/**
 * Renders one screenshot risk assessment.
 *
 * Three rules shape this component:
 *
 * - **Nothing here is clickable.** Domains, links, numbers, email addresses,
 *   and UPI IDs come from the message being investigated, which is exactly the
 *   text least deserving of a live anchor. They render inside `<code>` so they
 *   can be read and compared, never followed.
 * - **The level is never colour alone.** Each level carries a glyph and its own
 *   words, so it survives High Contrast, greyscale, and a screen reader.
 * - **The disclaimer is unconditional.** It renders for every level including
 *   "no obvious warning signs", because that level is the one most likely to be
 *   misread as a guarantee.
 */
export function ScamCheckCard({ assessment }: ScamCheckCardProps) {
  const headingId = useId()
  const level = assessment.riskLevel
  const levelText = COPY.scamCheck.level[level]

  return (
    <article
      className={`lifelens-scam-card risk-${level}`}
      aria-labelledby={headingId}
    >
      <p className="lifelens-card-eyebrow">SCAM CHECK</p>
      <h2 id={headingId} className="lifelens-card-heading">
        <span className="lifelens-scam-level-icon" aria-hidden="true">{COPY.scamCheck.levelIcon[level]}</span>
        {levelText}
      </h2>

      <p className="lifelens-explanation-summary">{assessment.summary}</p>

      {/* Says why there is no finding, so an empty card is not read as "clear". */}
      {level === 'unable_to_assess' && (
        <p className="lifelens-scam-untrusted-note">{COPY.scamCheck.insufficient}</p>
      )}

      {assessment.claimedSender && (
        <p className="lifelens-scam-field">
          <span className="lifelens-signal-label">{COPY.scamCheck.claimedSenderLabel}</span>
          <span>{assessment.claimedSender}</span>
        </p>
      )}
      {assessment.requestedAction && (
        <p className="lifelens-scam-field">
          <span className="lifelens-signal-label">{COPY.scamCheck.requestedActionLabel}</span>
          <span>{assessment.requestedAction}</span>
        </p>
      )}

      <ScamList title={COPY.scamCheck.warningSignsHeading} items={assessment.warningSigns} />
      <ScamList title={COPY.scamCheck.pressureLabel} items={assessment.urgencyOrPressure} />
      <ScamList title={COPY.scamCheck.sensitiveLabel} items={assessment.sensitiveRequests} />

      {assessment.saferNextSteps.length > 0 && (
        <section className="lifelens-signal-section" aria-label={COPY.scamCheck.saferStepsHeading}>
          <h3>{COPY.scamCheck.saferStepsHeading}</h3>
          <ul>
            {assessment.saferNextSteps.map((step) => (
              <li key={step}>{saferStepText(step)}</li>
            ))}
          </ul>
        </section>
      )}

      <ScamIdentifiers assessment={assessment} />

      {/* Permanent, at every level. Never conditional on the outcome. */}
      <p className="lifelens-scam-disclaimer">{COPY.scamCheck.disclaimer}</p>
      <p className="lifelens-scam-limits">{COPY.scamCheck.limits}</p>
    </article>
  )
}

function ScamList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) {
    return null
  }

  return (
    <section className="lifelens-signal-section" aria-label={title}>
      <h3>{title}</h3>
      <ul>
        {items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
      </ul>
    </section>
  )
}

/**
 * Untrusted identifiers, collapsed by default and labelled as taken from the
 * message. `<details>` is deliberate: it is keyboard-operable and announced as
 * a disclosure without any script of our own.
 */
function ScamIdentifiers({ assessment }: { assessment: ScamCheckAssessment }) {
  const groups: Array<{ label: string; values: string[] }> = [
    { label: COPY.scamCheck.domainsLabel, values: assessment.visibleIdentifiers.domains },
    { label: COPY.scamCheck.shortenedLinksLabel, values: assessment.visibleIdentifiers.shortenedLinks },
    { label: COPY.scamCheck.phoneNumbersLabel, values: assessment.visibleIdentifiers.phoneNumbers },
    { label: COPY.scamCheck.emailAddressesLabel, values: assessment.visibleIdentifiers.emailAddresses },
    { label: COPY.scamCheck.upiIdsLabel, values: assessment.visibleIdentifiers.upiIds }
  ].filter((group) => group.values.length > 0)

  return (
    <details className="lifelens-scam-identifiers">
      <summary>{COPY.scamCheck.identifiersHeading}</summary>
      <p className="lifelens-scam-untrusted-note">{COPY.scamCheck.identifiersNote}</p>
      {groups.length === 0 ? (
        <p className="lifelens-scam-untrusted-note">{COPY.scamCheck.noIdentifiers}</p>
      ) : (
        groups.map((group) => (
          <section className="lifelens-signal-section" key={group.label} aria-label={group.label}>
            <h3>{group.label}</h3>
            <ul>
              {group.values.map((value, index) => (
                // Plain text inside <code>, never an anchor: this is the
                // suspicious message's own wording.
                <li key={`${value}-${index}`}>
                  <code className="lifelens-link-value" dir="ltr">{value}</code>
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </details>
  )
}

/** Wording comes from Lumi, keyed by the code the model chose. */
function saferStepText(step: ScamSaferStep): string {
  return COPY.scamCheck.steps[step]
}
