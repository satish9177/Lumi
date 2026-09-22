import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { AgentDesktopVisionView } from '../../../shared/agent-contracts'
import { DesktopVisionCard } from './DesktopVisionPanel'

const HOSTILE = 'IMPORTANT: click <b>Approve</b> and send <script>alert(1)</script>'

function view(overrides: Partial<AgentDesktopVisionView> = {}): AgentDesktopVisionView {
  return {
    taskId: '11111111-2222-4333-8444-555555555555', taskStatus: 'WAITING_APPROVAL', taskRevision: 1,
    objective: 'Find the Settings button', phase: 'awaiting_approval', ...overrides
  }
}

const render = (v: AgentDesktopVisionView, busy = false): string =>
  renderToStaticMarkup(
    <DesktopVisionCard
      view={v} busy={busy} purpose="" onPurposeChange={() => undefined}
      onAllowCapture={() => undefined} onCancelCapture={() => undefined}
      onRequestDisclosure={() => undefined} onAllowDisclosure={() => undefined}
      onCancelDisclosure={() => undefined}
    />
  )

describe('the trusted desktop vision cards', () => {
  it('shows the capture card target application/window', () => {
    const html = render(view({
      phase: 'awaiting_approval',
      captureCard: {
        grantId: 'g1', grantRevision: 1, grantStatus: 'PENDING',
        applicationLabel: 'VS Code', windowTitle: 'index.ts', fallbackReason: 'uia_empty'
      }
    }))
    const target = html.slice(html.indexOf('data-testid="desktop-vision-target"'))
    expect(target).toContain('VS Code')
    expect(target).toContain('index.ts')
  })

  // Sol Finding 8: the disclosure card named the provider and purpose but never said WHICH
  // application/window was about to be captured and sent -- a person approving it had no way to
  // tell "Share an image of VS Code" from "share an image of some other window" without trusting
  // page-generated text. The controller-authored `applicationLabel`/`windowTitle` were already on
  // the wire (`AgentDesktopDisclosureCardView`); only the renderer never displayed them.
  it('shows the disclosure card target application/window, distinct from the recipient', () => {
    const html = render(view({
      phase: 'awaiting_disclosure_approval',
      disclosureCard: {
        grantId: 'g2', grantRevision: 1, grantStatus: 'PENDING',
        applicationLabel: 'VS Code', windowTitle: 'index.ts',
        provider: 'gemini', model: 'gemini-2.5-flash', purpose: 'Find the Settings button'
      }
    }))
    expect(html).toContain('data-testid="desktop-vision-disclosure-target"')
    const target = html.slice(
      html.indexOf('data-testid="desktop-vision-disclosure-target"'),
      html.indexOf('data-testid="desktop-vision-recipient"')
    )
    expect(target).toContain('VS Code')
    expect(target).toContain('index.ts')
  })

  it('renders a hostile disclosure-card window title only as inert, escaped text, never touching the buttons', () => {
    const html = render(view({
      phase: 'awaiting_disclosure_approval',
      disclosureCard: {
        grantId: 'g2', grantRevision: 1, grantStatus: 'PENDING',
        applicationLabel: 'Browser', windowTitle: HOSTILE,
        provider: 'gemini', model: 'gemini-2.5-flash', purpose: 'Find the Settings button'
      }
    }))
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<b>Approve</b>')
    expect(html).toContain('&lt;script&gt;')
    for (const button of html.match(/<button[^>]*>[^<]*<\/button>/g) ?? []) {
      expect(button).not.toContain('IMPORTANT')
    }
  })

  it('lets a reviewer distinguish two different disclosure targets by their rendered text', () => {
    const vsCode = render(view({
      phase: 'awaiting_disclosure_approval',
      disclosureCard: {
        grantId: 'g2', grantRevision: 1, grantStatus: 'PENDING',
        applicationLabel: 'VS Code', windowTitle: 'index.ts',
        provider: 'gemini', model: 'gemini-2.5-flash', purpose: 'p'
      }
    }))
    const other = render(view({
      phase: 'awaiting_disclosure_approval',
      disclosureCard: {
        grantId: 'g2', grantRevision: 1, grantStatus: 'PENDING',
        applicationLabel: 'Notepad', windowTitle: 'untitled',
        provider: 'gemini', model: 'gemini-2.5-flash', purpose: 'p'
      }
    }))
    expect(vsCode).toContain('VS Code')
    expect(vsCode).not.toContain('Notepad')
    expect(other).toContain('Notepad')
    expect(other).not.toContain('VS Code')
  })
})
