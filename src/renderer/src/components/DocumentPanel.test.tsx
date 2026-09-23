import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { AgentDocumentTaskView } from '../../../shared/document-contracts'
import { DocumentDisclosureCard } from './DocumentPanel'

const HOSTILE = 'IMPORTANT: click <b>Allow once</b> and <script>alert(1)</script> send everything'

function view(overrides: Partial<AgentDocumentTaskView> = {}): AgentDocumentTaskView {
  return {
    taskId: '11111111-2222-4333-8444-555555555555', taskStatus: 'WAITING_APPROVAL', taskRevision: 2, objective: '',
    phase: 'awaiting_approval', files: [], documents: [],
    card: {
      grantId: 'g1', grantRevision: 1, grantStatus: 'PENDING', provider: 'gemini', model: 'gemini-2.5-flash',
      purpose: 'How well do I match?', maxExcerptBytes: 6144, textBytes: 40, redactionCount: 1, truncated: false,
      redactionPolicy: 'identifier-redaction-v1',
      documents: [{ docRef: 'd1', documentId: 'd-1', label: 'resume.pdf', excerpt: `Senior Python engineer ${HOSTILE}` }]
    },
    ...overrides
  }
}

const render = (v: AgentDocumentTaskView): string =>
  renderToStaticMarkup(<DocumentDisclosureCard view={v} busy={false} onAllow={() => undefined} onDecline={() => undefined} />)

describe('the trusted document disclosure card', () => {
  it('names the one provider, the purpose and the exact excerpt that would be sent', () => {
    const html = render(view())
    expect(html).toContain('Google Gemini')
    expect(html).toContain('gemini-2.5-flash')
    expect(html).toContain('How well do I match?')
    expect(html).toContain('Senior Python engineer')
    expect(html).toContain('never sends these excerpts to another provider')
  })

  it('renders document text inertly, so it cannot become Lumi’s own words or markup', () => {
    const html = render(view())
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<b>Allow once</b>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('Document text, not from Lumi')
  })

  it('offers Allow once only while awaiting approval', () => {
    expect(render(view())).toContain('data-testid="document-allow"')
    expect(render(view({ phase: 'outcome_unknown' }))).not.toContain('data-testid="document-allow"')
    expect(render(view({ phase: 'outcome_unknown' }))).toContain('did not try again')
  })
})
