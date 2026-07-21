import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { SCAM_RISK_LEVELS, SCAM_SAFER_STEPS, type ScamCheckAssessment } from '../../shared/contracts'
import { ScamCheckCard } from './components'
import { COPY } from './copy'

const base: ScamCheckAssessment = {
  sourceCaptureId: 'capture-1',
  riskLevel: 'high_risk',
  claimedSender: 'A national bank',
  requestedAction: 'Share the one-time password',
  urgencyOrPressure: ['Says the account closes in 30 minutes'],
  sensitiveRequests: ['Asks for the one-time password'],
  visibleIdentifiers: {
    domains: ['secure-verify-bank.example'],
    phoneNumbers: ['+91 90000 00000'],
    emailAddresses: ['alerts@secure-verify-bank.example'],
    upiIds: ['payme@examplebank'],
    shortenedLinks: ['bit.ly/abc123']
  },
  warningSigns: ['Visible text asks for a one-time password.'],
  saferNextSteps: ['never_share_otp', 'call_number_on_card'],
  summary: 'This message asks for a one-time password and pressures you to act quickly.'
}

const render = (assessment: Partial<ScamCheckAssessment> = {}): string =>
  renderToStaticMarkup(<ScamCheckCard assessment={{ ...base, ...assessment }} />)

describe('scam result card', () => {
  it('shows the level, the summary, the warning signs, and the safer steps', () => {
    const markup = render()

    expect(markup).toContain('High scam risk')
    expect(markup).toContain(base.summary)
    expect(markup).toContain('Visible text asks for a one-time password.')
    expect(markup).toContain(COPY.scamCheck.steps.never_share_otp)
    expect(markup).toContain(COPY.scamCheck.steps.call_number_on_card)
  })

  it.each([
    ['high_risk', 'High scam risk'],
    ['warning_signs', 'Some warning signs'],
    ['no_obvious_warning_signs', 'No obvious warning signs'],
    ['unable_to_assess', 'Lumi couldn’t assess this message reliably.']
  ] as const)('displays %s exactly as "%s"', (riskLevel, expected) => {
    expect(render({ riskLevel })).toContain(expected)
  })

  it('never displays the word "safe" on its own as a verdict', () => {
    for (const riskLevel of SCAM_RISK_LEVELS) {
      const markup = render({ riskLevel, warningSigns: [], saferNextSteps: [] })

      expect(markup).not.toMatch(/>\s*Safe\s*</)
      // "genuine" appears once, inside the disclaimer that denies it. Nowhere
      // else, and never as an affirmation.
      const withoutDisclaimer = markup.replace(COPY.scamCheck.disclaimer, '')
      expect(withoutDisclaimer).not.toMatch(/verified|genuine|legitimate/i)
      expect(withoutDisclaimer).not.toMatch(/\bis\s+(?:safe|genuine|legitimate|verified)\b/i)
    }
  })

  it('carries the disclaimer at every level, including when nothing was found', () => {
    for (const riskLevel of SCAM_RISK_LEVELS) {
      expect(render({ riskLevel, warningSigns: [], saferNextSteps: [] })).toContain(COPY.scamCheck.disclaimer)
    }
  })

  it('explains an unable-to-assess result instead of showing an empty card', () => {
    const markup = render({ riskLevel: 'unable_to_assess', warningSigns: [], saferNextSteps: [] })

    expect(markup).toContain(COPY.scamCheck.insufficient)
    expect(markup).toContain(COPY.scamCheck.disclaimer)
  })

  it('states what a screenshot cannot establish', () => {
    const markup = render()

    expect(markup).toContain(COPY.scamCheck.limits)
    expect(COPY.scamCheck.limits).toMatch(/cannot check who really sent/i)
  })

  it('does not rely on colour alone for the level', () => {
    const markup = render({ riskLevel: 'high_risk' })

    // A glyph and the words are both present; the class is the third signal.
    expect(markup).toContain(COPY.scamCheck.levelIcon.high_risk)
    expect(markup).toContain('High scam risk')
    expect(markup).toContain('risk-high_risk')
  })
})

/* -------------------------------------------------- identifiers stay inert */

describe('visible identifiers are untrusted text', () => {
  it('renders every identifier as plain text, never as a link', () => {
    const markup = render()

    expect(markup).toContain('secure-verify-bank.example')
    expect(markup).toContain('bit.ly/abc123')
    expect(markup).toContain('payme@examplebank')
    // Nothing in the card is an anchor, and nothing carries an href, a
    // click handler, or a tel:/mailto: target.
    expect(markup).not.toMatch(/<a[\s>]/)
    expect(markup).not.toMatch(/href=/)
    expect(markup).not.toMatch(/tel:|mailto:/)
    expect(markup).toMatch(/<code[^>]*>secure-verify-bank\.example<\/code>/)
  })

  it('collapses them and marks them as taken from the message', () => {
    const markup = render()

    expect(markup).toContain('<details')
    // No `open` attribute: collapsed until the user asks for it.
    expect(markup).not.toMatch(/<details[^>]*\sopen/)
    expect(markup).toContain(COPY.scamCheck.identifiersHeading)
    expect(markup).toContain(COPY.scamCheck.identifiersNote)
    expect(COPY.scamCheck.identifiersNote).toMatch(/does not open, call, or check/i)
  })

  it('escapes a hostile identifier rather than rendering it', () => {
    const markup = render({
      visibleIdentifiers: {
        ...base.visibleIdentifiers,
        domains: ['<img src=x onerror=alert(1)>']
      }
    })

    expect(markup).not.toContain('<img src=x')
    expect(markup).toContain('&lt;img')
  })

  it('says so plainly when nothing was legible', () => {
    const markup = render({
      visibleIdentifiers: { domains: [], phoneNumbers: [], emailAddresses: [], upiIds: [], shortenedLinks: [] }
    })

    expect(markup).toContain(COPY.scamCheck.noIdentifiers)
  })
})

/* ------------------------------------------------------------ app-authored */

describe('advice is app-authored', () => {
  it('has wording for every step code, so a valid assessment can always render', () => {
    for (const step of SCAM_SAFER_STEPS) {
      expect(COPY.scamCheck.steps[step]).toBeTypeOf('string')
      expect(COPY.scamCheck.steps[step].length).toBeGreaterThan(10)
    }
  })

  it('renders the step wording from Lumi, not any text the model sent', () => {
    const markup = render({ saferNextSteps: ['avoid_link_in_message'] })

    expect(markup).toContain(COPY.scamCheck.steps.avoid_link_in_message)
  })

  it('names no emergency number and no official web address', () => {
    for (const step of Object.values(COPY.scamCheck.steps)) {
      // Advice points at something the user already holds — a card, a saved
      // contact, an installed app — never at a number or URL Lumi supplies.
      expect(step).not.toMatch(/\b\d{3,}\b/)
      expect(step).not.toMatch(/https?:|www\.|\.gov|\.com\b/)
    }
  })

  it('keeps the India recovery note bounded and free of contact details', () => {
    const note = COPY.scamCheck.steps.india_financial_fraud_recovery

    expect(note).toMatch(/contact your bank immediately/i)
    expect(note).toMatch(/official cyber-fraud reporting channels/i)
    expect(note).not.toMatch(/\d/)
  })
})

/* ------------------------------------------------------------ accessibility */

describe('scam card accessibility', () => {
  it('labels the card by its own heading', () => {
    const markup = render()

    expect(markup).toMatch(/aria-labelledby="[^"]+"/)
    expect(markup).toMatch(/<h2 id="[^"]+"/)
  })

  it('labels every section so a screen reader can skip between them', () => {
    const markup = render()

    for (const label of [
      COPY.scamCheck.warningSignsHeading,
      COPY.scamCheck.saferStepsHeading,
      COPY.scamCheck.pressureLabel,
      COPY.scamCheck.sensitiveLabel
    ]) {
      expect(markup).toContain(`aria-label="${label}"`)
    }
  })

  it('hides the decorative level glyph from assistive technology', () => {
    expect(render()).toMatch(/aria-hidden="true"[^>]*>[^<]*<\/span>/)
  })

  it('uses a native disclosure for identifiers, so it is keyboard-operable', () => {
    const markup = render()

    expect(markup).toContain('<summary>')
  })

  it('omits an empty section rather than rendering an empty list', () => {
    const markup = render({ warningSigns: [], urgencyOrPressure: [], sensitiveRequests: [], saferNextSteps: [] })

    expect(markup).not.toContain(`aria-label="${COPY.scamCheck.warningSignsHeading}"`)
    expect(markup).not.toContain('<ul></ul>')
  })
})
