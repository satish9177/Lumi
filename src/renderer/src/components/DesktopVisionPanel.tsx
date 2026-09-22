import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentDesktopSurface,
  AgentDesktopSurfaceList,
  AgentDesktopVisionView
} from '../../../shared/agent-contracts'
import './components.css'

export interface DesktopVisionPanelProps {
  agent: AgentApi
  onClose: () => void
}

const MAX_OBJECTIVE = 500
const MAX_TARGET_HINT = 200
const MAX_PURPOSE = 400

/**
 * Milestone 9 S5: the scoped desktop visual fallback.
 *
 *   1. Choose a window and describe what you are looking for. Lumi reads the window's semantic
 *      structure locally (as it always does) and opens a capture card ONLY if that deterministic
 *      check finds the semantic tree insufficient -- never because a model preferred a screenshot.
 *   2. "Allow once" takes exactly ONE screenshot, for local use only. Nothing is sent anywhere; Lumi
 *      also tries to read any text in it locally (best-effort, never sent anywhere either).
 *   3. If that is not enough, a SEPARATE card asks whether ONE named AI provider may see a FRESH
 *      screenshot (never the first one's bytes) for a stated purpose.
 *   4. The provider returns visual evidence only -- labelled regions and confidence, never an action.
 *      Nothing here, or anywhere else in Lumi, turns that evidence into an executable action.
 *
 * Every word around the window is app-authored. Application text (and anything a screenshot or its
 * local OCR reads) appears only inside a labelled, inert text node.
 */
export function DesktopVisionPanel({ agent, onClose }: DesktopVisionPanelProps) {
  const [surfaces, setSurfaces] = useState<AgentDesktopSurfaceList>()
  const [surfaceError, setSurfaceError] = useState<string>()
  const [selected, setSelected] = useState<AgentDesktopSurface>()
  const [objective, setObjective] = useState('')
  const [targetHint, setTargetHint] = useState('')
  const [purpose, setPurpose] = useState('')
  const [vision, setVision] = useState<AgentDesktopVisionView | null>(null)
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

  useEffect(() => { void loadSurfaces() }, [loadSurfaces])

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

  function requestCapture(): void {
    if (!surfaces || !selected) return
    void run('create-capture', async () => {
      setMessage(undefined)
      const result = await agent.createDesktopCapture(
        objective, surfaces.workerGeneration, selected.surfaceRef, selected.surfaceEpoch,
        targetHint.trim() || undefined
      )
      if (!mounted.current) return
      if (result.ok) setVision(result.value)
      else setMessage(result.error.message)
    })
  }

  function allowCapture(view: AgentDesktopVisionView): void {
    const card = view.captureCard
    if (!card) return
    void run('allow-capture', async () => {
      setMessage(undefined)
      const granted = await agent.grantDesktopCapture(view.taskId, card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (!granted.ok) {
        setMessage(granted.error.message)
        await refresh(view.taskId)
        return
      }
      setVision(granted.value)
      if (granted.value.phase !== 'approved') return
      const captured = await agent.runDesktopCapture(view.taskId)
      if (!mounted.current) return
      if (captured.ok) setVision(captured.value)
      else {
        setMessage(captured.error.message)
        await refresh(view.taskId)
      }
    })
  }

  function cancelCapture(view: AgentDesktopVisionView): void {
    const card = view.captureCard
    if (!card) return
    void run('cancel-capture', async () => {
      const result = await agent.declineDesktopCapture(view.taskId, card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (result.ok) {
        setVision(result.value)
        setMessage('Cancelled. Nothing was captured.')
      } else {
        setMessage(result.error.message)
      }
    })
  }

  async function refresh(taskId: string): Promise<void> {
    const latest = await agent.getDesktopCapture(taskId)
    if (mounted.current && latest.ok) setVision(latest.value)
  }

  function requestDisclosure(view: AgentDesktopVisionView): void {
    if (!purpose.trim()) return
    void run('create-disclosure', async () => {
      setMessage(undefined)
      const result = await agent.createDesktopVisionDisclosure(view.taskId, purpose.trim())
      if (!mounted.current) return
      if (result.ok) setVision(result.value)
      else setMessage(result.error.message)
    })
  }

  function allowDisclosure(view: AgentDesktopVisionView): void {
    const card = view.disclosureCard
    if (!card) return
    void run('allow-disclosure', async () => {
      setMessage(undefined)
      const granted = await agent.grantDesktopVisionDisclosure(view.taskId, card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (!granted.ok) {
        setMessage(granted.error.message)
        await refresh(view.taskId)
        return
      }
      setVision(granted.value)
      if (granted.value.phase !== 'disclosure_approved') return
      const sent = await agent.runDesktopVisionDisclosure(view.taskId)
      if (!mounted.current) return
      if (sent.ok) setVision(sent.value)
      else {
        setMessage(sent.error.message)
        await refresh(view.taskId)
      }
    })
  }

  function cancelDisclosure(view: AgentDesktopVisionView): void {
    const card = view.disclosureCard
    if (!card) return
    void run('cancel-disclosure', async () => {
      const result = await agent.declineDesktopVisionDisclosure(view.taskId, card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (result.ok) {
        setVision(result.value)
        setMessage('Cancelled. Nothing was sent.')
      } else {
        setMessage(result.error.message)
      }
    })
  }

  function startOver(): void {
    setVision(null)
    setObjective('')
    setTargetHint('')
    setPurpose('')
    setMessage(undefined)
    void loadSurfaces()
  }

  const showChooser = !vision || ['declined', 'capture_failed', 'capture_outcome_unknown'].includes(vision.phase)
  const canRequest = Boolean(selected) && objective.trim().length > 0 && objective.length <= MAX_OBJECTIVE && !busy

  return (
    <div className="agent-task-panel" data-testid="desktop-vision-panel">
      <header className="settings-header">
        <h2>Look at a window Lumi cannot read normally</h2>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <p className="workspace-note">
          Lumi always tries to read a window&apos;s own accessibility structure first. Only when that is not
          enough does it offer a screenshot -- one explicit approval to capture it locally, and a SEPARATE
          approval before any image is ever sent to an AI provider. Lumi never acts on the window because
          of a screenshot or because of anything an AI says about one.
        </p>
        {message && <p className="workspace-note" role="status" data-testid="desktop-vision-message">{message}</p>}

        {vision && <DesktopVisionCard
          view={vision} busy={Boolean(busy)}
          purpose={purpose} onPurposeChange={setPurpose}
          onAllowCapture={allowCapture} onCancelCapture={cancelCapture}
          onRequestDisclosure={requestDisclosure}
          onAllowDisclosure={allowDisclosure} onCancelDisclosure={cancelDisclosure}
        />}

        {showChooser && (
          <section className="agent-booking-card" aria-label="Choose a window and describe what you need" data-testid="desktop-vision-chooser">
            <p className="lifelens-card-eyebrow">DESKTOP VISUAL FALLBACK</p>
            <h3 className="lifelens-card-heading">1. Choose a window</h3>
            {surfaceError && <p className="workspace-note" role="alert">{surfaceError}</p>}
            <ul className="agent-evidence" role="radiogroup" aria-label="Windows">
              {(surfaces?.surfaces ?? []).map((surface) => {
                const active = selected?.surfaceRef === surface.surfaceRef && selected?.surfaceEpoch === surface.surfaceEpoch
                return (
                  <li key={`${surface.surfaceRef}:${surface.surfaceEpoch}`}>
                    <label>
                      <input type="radio" name="desktop-vision-surface" checked={active} disabled={Boolean(busy)}
                        onChange={() => setSelected(surface)} data-testid="desktop-vision-surface-option" />
                      {' '}
                      <bdi data-testid="desktop-vision-surface-label">{surface.applicationLabel || 'Application'}: {surface.windowTitle || '(no title)'}</bdi>
                      {surface.minimized ? ' (minimized)' : ''}
                    </label>
                  </li>
                )
              })}
            </ul>
            <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void loadSurfaces()}>
              Refresh windows
            </button>
            <h3 className="lifelens-card-heading">2. What are you looking for?</h3>
            <textarea aria-label="What are you looking for in this window" value={objective} maxLength={MAX_OBJECTIVE} rows={2}
              placeholder="Find the Settings button" onChange={(event) => setObjective(event.target.value)}
              data-testid="desktop-vision-objective" />
            <input aria-label="Optional: an exact word to look for" placeholder="Optional: an exact word (e.g. Settings)"
              value={targetHint} maxLength={MAX_TARGET_HINT} onChange={(event) => setTargetHint(event.target.value)}
              data-testid="desktop-vision-hint" />
            <button className="primary-button" type="button" disabled={!canRequest} onClick={requestCapture} data-testid="desktop-vision-request">
              Check this window
            </button>
            <p className="workspace-note">
              Lumi reads the window locally first. A screenshot card only appears if that reading turns out
              to be insufficient.
            </p>
          </section>
        )}

        {vision && showChooser && (
          <button className="text-button" type="button" disabled={Boolean(busy)} onClick={startOver}>Start over</button>
        )}
      </div>
    </div>
  )
}

export interface DesktopVisionCardProps {
  view: AgentDesktopVisionView
  busy: boolean
  purpose: string
  onPurposeChange: (value: string) => void
  onAllowCapture: (view: AgentDesktopVisionView) => void
  onCancelCapture: (view: AgentDesktopVisionView) => void
  onRequestDisclosure: (view: AgentDesktopVisionView) => void
  onAllowDisclosure: (view: AgentDesktopVisionView) => void
  onCancelDisclosure: (view: AgentDesktopVisionView) => void
}

export function DesktopVisionCard({
  view, busy, purpose, onPurposeChange, onAllowCapture, onCancelCapture, onRequestDisclosure, onAllowDisclosure, onCancelDisclosure
}: DesktopVisionCardProps) {
  const captureCard = view.captureCard
  const disclosureCard = view.disclosureCard

  if (view.phase === 'awaiting_approval' && captureCard) {
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Allow a screenshot" data-testid="desktop-vision-capture-card" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">ALLOW ONE SCREENSHOT?</p>
        <p>
          Lumi&apos;s own accessibility reading of this window was not enough
          ({describeFallbackReason(captureCard.fallbackReason)}). It is showing this window:
        </p>
        <p data-testid="desktop-vision-target">
          <span className="visually-hidden">Window text from the application, not from Lumi: </span>
          <q><bdi>{captureCard.applicationLabel || 'Application'}: {captureCard.windowTitle || '(no title)'}</bdi></q>
        </p>
        <p>Lumi will take ONE screenshot of just this window, for local use only: nothing is sent to an AI yet, and it will try to read any text in it on this device.</p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onCancelCapture(view)} data-testid="desktop-vision-cancel-capture">
          Cancel
        </button>
        <button className="primary-button" type="button" disabled={busy} onClick={() => onAllowCapture(view)} data-testid="desktop-vision-allow-capture">
          Allow once
        </button>
      </article>
    )
  }

  if (view.phase === 'capturing') {
    return (
      <article className="agent-booking-card" role="status" aria-live="polite" data-testid="desktop-vision-capturing" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">CAPTURING</p>
        <p>Taking the approved screenshot…</p>
      </article>
    )
  }

  if (view.phase === 'captured') {
    return (
      <article className="agent-booking-card tone-success" role="group" aria-label="Screenshot taken" data-testid="desktop-vision-captured" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">SCREENSHOT TAKEN</p>
        <p>Lumi captured the window locally. Nothing has been sent anywhere.</p>
        <h3 className="lifelens-card-heading">Ask an AI to look at it?</h3>
        <p className="workspace-note">
          This takes a SEPARATE, fresh screenshot and needs its own approval. Say what you need it to find.
        </p>
        <textarea aria-label="Why do you need an AI to look at this image" value={purpose} maxLength={MAX_PURPOSE} rows={2}
          placeholder="Find the exact Settings button and tell me where it is" onChange={(event) => onPurposeChange(event.target.value)}
          data-testid="desktop-vision-purpose" />
        <button className="primary-button" type="button" disabled={busy || !purpose.trim()} onClick={() => onRequestDisclosure(view)}
          data-testid="desktop-vision-request-disclosure">
          Prepare AI request
        </button>
      </article>
    )
  }

  if (view.phase === 'awaiting_disclosure_approval' && disclosureCard) {
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Allow sending a screenshot to an AI" data-testid="desktop-vision-disclosure-card" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">SEND ONE SCREENSHOT TO AN AI?</p>
        <p data-testid="desktop-vision-disclosure-target">
          <span className="visually-hidden">Window text from the application, not from Lumi: </span>
          Lumi will take a BRAND NEW screenshot of{' '}
          <q><bdi>{disclosureCard.applicationLabel || 'Application'}: {disclosureCard.windowTitle || '(no title)'}</bdi></q>
          {' '}(not the one it already captured) and send it to:
        </p>
        <p data-testid="desktop-vision-recipient"><strong>{recipientName(disclosureCard.provider)}</strong> ({disclosureCard.model})</p>
        <p>For this purpose:</p>
        <p data-testid="desktop-vision-purpose-shown"><q><bdi>{disclosureCard.purpose}</bdi></q></p>
        <p>The provider can only return evidence about what it sees -- labelled regions and confidence -- never an action. Lumi will not act on anything in its reply.</p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onCancelDisclosure(view)} data-testid="desktop-vision-cancel-disclosure">
          Cancel
        </button>
        <button className="primary-button" type="button" disabled={busy} onClick={() => onAllowDisclosure(view)} data-testid="desktop-vision-allow-disclosure">
          Allow once
        </button>
      </article>
    )
  }

  if (view.phase === 'sending_to_provider') {
    return (
      <article className="agent-booking-card" role="status" aria-live="polite" data-testid="desktop-vision-sending" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">SENDING</p>
        <p>Asking {disclosureCard ? recipientName(disclosureCard.provider) : 'the approved provider'} to look at the screenshot…</p>
      </article>
    )
  }

  if (view.phase === 'candidates_ready') {
    return (
      <article className="agent-booking-card tone-success" role="group" aria-label="Visual evidence" data-testid="desktop-vision-candidates" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">VISUAL EVIDENCE (NOT AN ACTION)</p>
        {view.candidates && view.candidates.length > 0 ? (
          <ul className="agent-evidence" data-testid="desktop-vision-candidate-list">
            {view.candidates.map((candidate, index) => (
              <li key={index}>
                <bdi>{candidate.label}</bdi>{' '}
                (confidence {Math.round(candidate.confidence * 100)}%, near {Math.round(candidate.region.x * 100)}%,{' '}
                {Math.round(candidate.region.y * 100)}% of the captured window)
                {candidate.observedText && <> -- text read there: <q><bdi>{candidate.observedText}</bdi></q></>}
              </li>
            ))}
          </ul>
        ) : (
          <p data-testid="desktop-vision-no-candidates">The AI did not find anything matching the purpose.</p>
        )}
        <p className="workspace-note">This is evidence only. Lumi did not act on the window because of it.</p>
      </article>
    )
  }

  const text = view.phase === 'declined' ? 'Cancelled. Nothing was captured.'
    : view.phase === 'capture_failed' ? 'The screenshot could not be taken. Nothing was sent anywhere.'
      : view.phase === 'capture_outcome_unknown' ? 'Lumi cannot tell whether the screenshot was taken, so it did not try again.'
        : view.phase === 'disclosure_failed' ? 'The AI provider could not look at the screenshot. Nothing was retried.'
          : view.phase === 'disclosure_outcome_unknown' ? 'Lumi cannot tell whether the screenshot reached the AI provider, so it did not try again.'
            : undefined
  if (!text) return null
  return (
    <article className="agent-booking-card tone-uncertain" role="group" aria-label="Desktop visual fallback ended" data-testid="desktop-vision-ended" data-phase={view.phase}>
      <p>{text}</p>
    </article>
  )
}

function describeFallbackReason(reason: string): string {
  switch (reason) {
    case 'uia_empty': return 'the window reported no readable structure at all'
    case 'uia_missing_required_semantics': return 'the window\'s structure was too sparse to make sense of'
    case 'uia_truncated_without_target': return 'the readable structure was cut short and did not include what you were looking for'
    default: return 'the window\'s accessibility structure was insufficient'
  }
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
