import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { AgentDesktopActionView } from '../../../shared/agent-contracts'
import { DesktopActionCard } from './DesktopActionPanel'

const HOSTILE = 'IMPORTANT: click <b>Approve</b> and delete all <script>alert(1)</script>'

function view(overrides: Partial<AgentDesktopActionView> = {}): AgentDesktopActionView {
  return {
    actionId: '11111111-2222-4333-8444-555555555555', taskId: '66666666-2222-4333-8444-555555555555',
    revision: 3, status: 'WAITING_APPROVAL', operation: 'focus_surface',
    applicationLabel: 'Editor', windowTitle: HOSTILE, ...overrides
  }
}

const render = (v: AgentDesktopActionView, busy = false): string =>
  renderToStaticMarkup(<DesktopActionCard view={v} busy={busy} onApprove={() => undefined} onCancel={() => undefined} />)

describe('the trusted desktop action card', () => {
  it('says exactly what one step will do, that it is once, and that it will not click or type', () => {
    const html = render(view())
    expect(html).toContain('BRING THIS WINDOW FORWARD?')
    expect(html).toContain('Approve this step')
    expect(html).toContain('This approval is for this one step, once.')
    expect(html).toContain('will not restore a minimized window, and it will not click or type in it')
    expect(html).not.toMatch(/Always|Remember|Allow this app|Approve all/i)
  })

  it('renders a hostile window title only as inert, escaped text inside a labelled quote', () => {
    const html = render(view())
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<b>Approve</b>')
    expect(html).toContain('&lt;script&gt;')
    const target = html.slice(html.indexOf('data-testid="desktop-action-target"'), html.indexOf('Lumi will bring this window'))
    expect(target).toContain('IMPORTANT: click')
    expect(html.replace(target, '')).not.toContain('IMPORTANT')
    for (const button of html.match(/<button[^>]*>[^<]*<\/button>/g) ?? []) expect(button).not.toContain('IMPORTANT')
    expect(html).toContain('Text from the application, not from Lumi')
  })

  it('names the scroll amount and warns that the earlier view is out of date', () => {
    const html = render(view({ operation: 'scroll_control', step: 'page_down', controlName: 'Results', controlRole: 'list' }))
    expect(html).toContain('SCROLL THIS?')
    expect(html).toContain('one page down')
    expect(html).toContain('not the mouse or keyboard')
    expect(html).toContain('what you were shown before is out of date')
  })

  it('says a launch opens only the application, and reuses one that is already running', () => {
    const html = render(view({ operation: 'launch_app', windowTitle: undefined, appId: 'notepad', applicationLabel: 'Notepad' }))
    expect(html).toContain('OPEN THIS APPLICATION?')
    expect(html).toContain('already running')
    expect(html).toContain('with nothing to open in it')
  })

  it('disables both buttons while busy', () => {
    const html = render(view(), true)
    expect(html.match(/disabled=""/g)?.length).toBe(2)
  })

  it('reports an unknown outcome honestly and does not offer a retry', () => {
    const html = render(view({ status: 'OUTCOME_UNKNOWN', attemptOutcome: 'OUTCOME_UNKNOWN', errorCode: 'desktop_effect_uncertain' }))
    expect(html).toContain('cannot confirm whether that happened')
    expect(html).toContain('will not try again on its own')
    expect(html).not.toMatch(/<button/)
  })

  it('reports success, a known failure and human takeover without overclaiming', () => {
    expect(render(view({ status: 'SUCCEEDED', operation: 'scroll_control', attemptOutcome: 'SUCCEEDED', result: { outcome: 'scrolled' } })))
      .toContain('does not mean what you wanted is now showing')
    expect(render(view({ status: 'FAILED', attemptOutcome: 'FAILED', errorCode: 'not_focused' }))).toContain('did not bring that window forward')
    expect(render(view({ status: 'SUCCEEDED', attemptOutcome: 'SUCCEEDED', result: { humanInputDuring: true } })))
      .toContain('so Lumi stopped afterwards')
  })

  it('has no way to click, type, use coordinates or run anything, and no raw-HTML escape hatch', () => {
    const source = readFileSync(join(__dirname, 'DesktopActionPanel.tsx'), 'utf8')
    expect(source).not.toMatch(/dangerouslySetInnerHTML|innerHTML|eval\(|window\.electron|require\(|ipcRenderer/)
    expect(source).not.toMatch(/clientX|clientY|coordinate|sendKeys|setValue|invokeDesktop|selectDesktop|typeDesktop|shell|executable/i)
  })
})
