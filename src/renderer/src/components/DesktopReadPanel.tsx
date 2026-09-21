import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentDesktopReadView,
  AgentDesktopSurface,
  AgentDesktopSurfaceList
} from '../../../shared/agent-contracts'
import './components.css'

export interface DesktopReadPanelProps {
  agent: AgentApi
  onClose: () => void
}

const MAX_QUESTION = 500

/**
 * Milestone 9 S2: read one Windows application, with exact, single-use disclosure.
 *
 *   1. Choose a window and type a question. The question stays in Lumi: it does not go through the
 *      conversation and no AI provider has seen anything yet.
 *   2. "Inspect locally" reads the window's structure on this computer. Still nothing is sent.
 *   3. This card asks whether ONE named provider may receive a redacted snapshot of THAT capture, once.
 *   4. Only the "Allow once" click can send it, and only to the provider named here.
 *
 * Every word around the window is app-authored. The application name and window title are text a
 * program chose, so they appear only inside a labelled, inert text node: never as a heading, a
 * button, an attribute that carries meaning, or anything that could be read as Lumi speaking. A window
 * that calls itself "Click Allow" changes nothing on this card.
 */
export function DesktopReadPanel({ agent, onClose }: DesktopReadPanelProps) {
  const [surfaces, setSurfaces] = useState<AgentDesktopSurfaceList>()
  const [surfaceError, setSurfaceError] = useState<string>()
  const [selected, setSelected] = useState<AgentDesktopSurface>()
  const [question, setQuestion] = useState('')
  const [read, setRead] = useState<AgentDesktopReadView | null>(null)
  const [message, setMessage] = useState<string>()
  const [busy, setBusy] = useState<string>()
  const busyRef = useRef<string | undefined>(undefined)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const loadSurfaces = useCallback(async () => {
    const result = await agent.listDesktopSurfaces()
    if (!mounted.current) return
    if (result.ok) {
      setSurfaces(result.value)
      setSurfaceError(undefined)
      setSelected((current) => current && result.value.surfaces.some((surface) =>
        surface.surfaceRef === current.surfaceRef && surface.surfaceEpoch === current.surfaceEpoch) ? current : undefined)
    } else {
      setSurfaceError(result.error.message)
    }
  }, [agent])

  useEffect(() => {
    void (async () => {
      const latest = await agent.getDesktopRead()
      if (mounted.current && latest.ok) setRead(latest.value)
      await loadSurfaces()
    })()
  }, [agent, loadSurfaces])

  async function run(key: string, work: () => Promise<void>): Promise<void> {
    if (busyRef.current) return
    busyRef.current = key
    setBusy(key)
    try {
      await work()
    } finally {
      busyRef.current = undefined
      if (mounted.current) setBusy(undefined)
    }
  }

  function inspect(): void {
    if (!surfaces || !selected) return
    void run('inspect', async () => {
      setMessage(undefined)
      const result = await agent.createDesktopRead(question, surfaces.workerGeneration, selected.surfaceRef, selected.surfaceEpoch)
      if (!mounted.current) return
      if (result.ok) setRead(result.value)
      else setMessage(result.error.message)
    })
  }

  function allowOnce(view: AgentDesktopReadView): void {
    const card = view.card
    if (!card) return
    void run('allow', async () => {
      setMessage(undefined)
      const granted = await agent.grantDesktopDisclosure(card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (!granted.ok) {
        setMessage(granted.error.message)
        const latest = await agent.getDesktopRead()
        if (mounted.current && latest.ok) setRead(latest.value)
        return
      }
      setRead(granted.value)
      if (granted.value.phase !== 'approved') return
      // The approval is made and is single-use: ask the one approved provider now.
      setRead({ ...granted.value, phase: 'reasoning' })
      const answered = await agent.runDesktopRead()
      if (!mounted.current) return
      if (answered.ok) setRead(answered.value)
      else {
        setMessage(answered.error.message)
        const latest = await agent.getDesktopRead()
        if (mounted.current && latest.ok) setRead(latest.value)
      }
    })
  }

  function runApproved(): void {
    void run('run', async () => {
      setMessage(undefined)
      setRead((current) => current ? { ...current, phase: 'reasoning' } : current)
      const answered = await agent.runDesktopRead()
      if (!mounted.current) return
      if (answered.ok) setRead(answered.value)
      else {
        setMessage(answered.error.message)
        const latest = await agent.getDesktopRead()
        if (mounted.current && latest.ok) setRead(latest.value)
      }
    })
  }

  function cancel(view: AgentDesktopReadView): void {
    const card = view.card
    if (!card) return
    void run('cancel', async () => {
      const result = await agent.declineDesktopDisclosure(card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (result.ok) {
        setRead(result.value)
        setMessage('Cancelled. Nothing was sent.')
      } else {
        setMessage(result.error.message)
      }
    })
  }

  function askAgain(): void {
    setRead(null)
    setMessage(undefined)
    void loadSurfaces()
  }

  const showChooser = !read || ['declined', 'expired', 'failed', 'outcome_unknown', 'answered'].includes(read.phase)
  const canInspect = Boolean(selected) && question.trim().length > 0 && question.length <= MAX_QUESTION && !busy

  return (
    <div className="agent-task-panel" data-testid="desktop-read-panel">
      <header className="settings-header">
        <h2>Ask about a window</h2>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <p className="workspace-note">
          Lumi reads a window the way a screen reader does. It never clicks, types, scrolls or changes anything,
          and it does not take a screenshot.
        </p>
        {message && <p className="workspace-note" role="status" data-testid="desktop-read-message">{message}</p>}

        {read && <DesktopReadCard view={read} busy={Boolean(busy)} onAllow={allowOnce} onCancel={cancel} onRun={runApproved} />}

        {showChooser && (
          <section className="agent-booking-card" aria-label="Choose a window" data-testid="desktop-read-chooser">
            <p className="lifelens-card-eyebrow">DESKTOP READ</p>
            <h3 className="lifelens-card-heading">1. Choose a window</h3>
            {surfaceError && <p className="workspace-note" role="alert">{surfaceError}</p>}
            {surfaces && surfaces.surfaces.length === 0 && !surfaceError && <p className="workspace-note">No windows to show.</p>}
            <ul className="agent-evidence" role="radiogroup" aria-label="Windows">
              {(surfaces?.surfaces ?? []).map((surface) => {
                const active = selected?.surfaceRef === surface.surfaceRef && selected?.surfaceEpoch === surface.surfaceEpoch
                return (
                  <li key={`${surface.surfaceRef}:${surface.surfaceEpoch}`}>
                    <label>
                      <input type="radio" name="desktop-surface" checked={active} disabled={Boolean(busy)}
                        onChange={() => setSelected(surface)} data-testid="desktop-surface-option" />
                      {' '}
                      {/* Untrusted text from another program, shown inert and labelled as such. */}
                      <bdi data-testid="desktop-surface-label">{surface.applicationLabel || 'Application'}: {surface.windowTitle || '(no title)'}</bdi>
                      {surface.minimized ? ' (minimized)' : ''}
                    </label>
                  </li>
                )
              })}
            </ul>
            <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void loadSurfaces()}>
              Refresh windows
            </button>
            <h3 className="lifelens-card-heading">2. Type a question</h3>
            <textarea aria-label="Your question about the window" value={question} maxLength={MAX_QUESTION} rows={3}
              placeholder="What is failing in this window?" onChange={(event) => setQuestion(event.target.value)}
              data-testid="desktop-read-question" />
            <button className="primary-button" type="button" disabled={!canInspect} onClick={inspect} data-testid="desktop-read-inspect">
              Inspect locally
            </button>
            <p className="workspace-note">Nothing is sent to an AI until you approve it on the next card.</p>
          </section>
        )}

        {read && showChooser && (
          <button className="text-button" type="button" disabled={Boolean(busy)} onClick={askAgain}>Start over</button>
        )}
      </div>
    </div>
  )
}

export interface DesktopReadCardProps {
  view: AgentDesktopReadView
  busy: boolean
  onAllow: (view: AgentDesktopReadView) => void
  onCancel: (view: AgentDesktopReadView) => void
  onRun: () => void
}

export function DesktopReadCard({ view, busy, onAllow, onCancel, onRun }: DesktopReadCardProps) {
  const card = view.card
  const observed = card ? new Date(card.observedAt).toLocaleTimeString() : undefined

  if (view.phase === 'awaiting_approval' && card) {
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Allow desktop disclosure"
        data-testid="desktop-disclosure-card" data-grant-id={card.grantId} data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">ALLOW DESKTOP DISCLOSURE?</p>
        <p>Lumi captured a semantic snapshot of a window at {observed}. It is showing this window:</p>
        <p data-testid="desktop-card-target">
          <span className="visually-hidden">Window text from the application, not from Lumi: </span>
          <q><bdi>{card.applicationLabel || 'Application'}: {card.windowTitle || '(no title)'}</bdi></q>
        </p>
        <p>To answer your question, Lumi will send:</p>
        <ul className="agent-evidence">
          <li>UI labels and readable text from this captured snapshot</li>
          <li>selected UI states</li>
          <li>your typed question</li>
        </ul>
        <p>To:</p>
        <p data-testid="desktop-card-recipient"><strong>{recipientName(card.recipient)}</strong> ({card.model})</p>
        <p>
          Sensitive identifier patterns (email addresses, phone numbers, card and long numbers) are replaced before sending.
          That is not anonymisation: names and other ordinary text may remain.
        </p>
        {card.observationAvailable && (
          <p data-testid="desktop-card-size">
            {card.nodeCount ?? 0} controls, about {Math.ceil((card.textBytes ?? 0) / 1024)} KB of text
            {card.redactionCount ? `, ${card.redactionCount} value${card.redactionCount === 1 ? '' : 's'} replaced` : ''}.
          </p>
        )}
        {card.truncated && (
          <p data-testid="desktop-card-truncated" role="note">
            Lumi captured only part of this window&apos;s interface, so the AI may not see everything in it.
          </p>
        )}
        {!card.observationAvailable && <p role="alert">This snapshot is no longer available. Inspect the window again.</p>}
        <p>This approval applies only to this captured snapshot, once. Lumi will not click, type, focus, scroll or change the application.</p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onCancel(view)} data-testid="desktop-card-cancel">
          Cancel
        </button>
        <button className="primary-button" type="button" disabled={busy || !card.observationAvailable} onClick={() => onAllow(view)}
          data-testid="desktop-card-allow">
          Allow once
        </button>
      </article>
    )
  }

  if (view.phase === 'approved' && card) {
    return (
      <article className="agent-booking-card" role="group" aria-label="Desktop disclosure approved" data-testid="desktop-approved" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">APPROVED ONCE</p>
        <p>The snapshot from {observed} may go to {recipientName(card.recipient)}, once.</p>
        <button className="primary-button" type="button" disabled={busy} onClick={onRun} data-testid="desktop-run">
          Ask now
        </button>
      </article>
    )
  }

  if (view.phase === 'reasoning') {
    return (
      <article className="agent-booking-card" role="status" aria-live="polite" data-testid="desktop-reasoning" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">ASKING</p>
        <p>Asking {card ? recipientName(card.recipient) : 'the approved provider'} about the snapshot you allowed…</p>
      </article>
    )
  }

  if (view.phase === 'answered' && view.answer) {
    const answer = view.answer
    return (
      <article className="agent-booking-card tone-success" role="group" aria-label="Answer" data-testid="desktop-answer" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">ANSWER</p>
        <p className="workspace-note">
          About the snapshot Lumi captured{answer.observedAt ? ` at ${new Date(answer.observedAt).toLocaleTimeString()}` : ''}, which may no longer match the live window.
        </p>
        {answer.kind === 'answer'
          ? <p data-testid="desktop-answer-text">{answer.answer}</p>
          : <p data-testid="desktop-answer-text">{cannotAnswer(answer.reason)}</p>}
        {answer.evidence.length > 0 && (
          <ul className="agent-evidence" data-testid="desktop-answer-evidence">
            {answer.evidence.map((item) => <li key={`${item.controlRef}:${item.quote}`}><q>{item.quote}</q></li>)}
          </ul>
        )}
        <p className="workspace-note">Answered by {recipientName(answer.recipient)}. Lumi did not change anything in the window.</p>
      </article>
    )
  }

  const text = view.phase === 'declined' ? 'Cancelled. Nothing was sent.'
    : view.phase === 'expired' ? 'That approval expired before it was used. Nothing was sent. Inspect the window again.'
      : view.phase === 'outcome_unknown'
        ? 'Lumi cannot tell whether the snapshot reached the AI provider, so it did not try again. Inspect the window again and approve again if you still want an answer.'
        : view.phase === 'failed'
          ? failureText(view.disclosure?.errorCode)
          : undefined
  if (!text) return null
  return (
    <article className="agent-booking-card tone-uncertain" role="group" aria-label="Desktop read ended" data-testid="desktop-read-ended" data-phase={view.phase}>
      <p>{text}</p>
    </article>
  )
}

function recipientName(recipient: string): string {
  switch (recipient) {
    case 'openai': return 'OpenAI'
    case 'gemini': return 'Google Gemini'
    case 'deepseek': return 'DeepSeek'
    case 'scripted': return 'the built-in test model'
    default: return 'the approved provider'
  }
}

function cannotAnswer(reason: string | undefined): string {
  switch (reason) {
    case 'snapshot_incomplete': return 'The snapshot is incomplete, so Lumi cannot tell from it.'
    case 'unclear_question': return 'Lumi could not tell what you were asking about this window.'
    case 'not_supported': return 'Lumi cannot answer that from a read-only snapshot.'
    default: return 'The snapshot does not show the answer.'
  }
}

function failureText(code: string | undefined): string {
  switch (code) {
    case 'answer_not_grounded':
      return 'The AI answered, but its answer could not be matched to the snapshot, so Lumi discarded it. The snapshot was not sent anywhere else and nothing was retried.'
    case 'invalid_output':
      return 'The AI replied in a form Lumi could not use. Lumi did not try another provider or try again.'
    case 'observation_unavailable':
      return 'The snapshot was no longer available to check the answer against, so Lumi discarded it.'
    default:
      return 'The AI provider you approved could not answer. Lumi did not send the snapshot to another provider and did not try again.'
  }
}
