import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentDesktopActionView,
  AgentDesktopPlanView,
  AgentDesktopSurface,
  AgentDesktopSurfaceList,
  AgentPlanValueInput
} from '../../../shared/agent-contracts'
import { DesktopActionCard } from './DesktopActionPanel'
import './components.css'

export interface DesktopPlanningPanelProps {
  agent: AgentApi
  onClose: () => void
}

const MAX_OBJECTIVE = 500
const MAX_VALUES = 4
const MAX_VALUE_CHARS = 500

interface ValueRow {
  classification: string
  value: string
}

/**
 * Milestone 9 S4: propose and review ONE bounded semantic desktop action.
 *
 *   1. Choose a window, type what you want done, and (optionally) type up to 4 candidate values Lumi
 *      may write -- each with a short label you choose. Nothing is sent to any provider yet.
 *   2. This card asks whether ONE named provider may receive a redacted snapshot plus the candidate
 *      values' labels and lengths (never the value text) to propose ONE action.
 *   3. The provider proposes an action. Recording it is disclosure bookkeeping only: nothing has run.
 *   4. "Review the exact step" opens a SECOND, separate trusted card -- exactly the same kind S3 uses
 *      for focus/scroll/launch -- and only THAT card's "Approve this step" click can perform an effect.
 *
 * Every word around the window is app-authored. Application text appears only inside a labelled, inert
 * text node: a window that calls itself "Click Approve" changes nothing here.
 */
export function DesktopPlanningPanel({ agent, onClose }: DesktopPlanningPanelProps) {
  const [surfaces, setSurfaces] = useState<AgentDesktopSurfaceList>()
  const [surfaceError, setSurfaceError] = useState<string>()
  const [selected, setSelected] = useState<AgentDesktopSurface>()
  const [objective, setObjective] = useState('')
  const [values, setValues] = useState<ValueRow[]>([])
  const [plan, setPlan] = useState<AgentDesktopPlanView | null>(null)
  const [action, setAction] = useState<AgentDesktopActionView | null>(null)
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
      const latest = await agent.getDesktopPlan()
      if (mounted.current && latest.ok) setPlan(latest.value)
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

  function addValue(): void {
    if (values.length >= MAX_VALUES) return
    setValues((current) => [...current, { classification: '', value: '' }])
  }

  function updateValue(index: number, patch: Partial<ValueRow>): void {
    setValues((current) => current.map((row, position) => position === index ? { ...row, ...patch } : row))
  }

  function removeValue(index: number): void {
    setValues((current) => current.filter((_, position) => position !== index))
  }

  function propose(): void {
    if (!surfaces || !selected) return
    void run('propose', async () => {
      setMessage(undefined)
      const payload: AgentPlanValueInput[] = values
        .filter((row) => row.classification.trim().length > 0 && row.value.length > 0)
        .map((row) => ({ classification: row.classification.trim(), value: row.value }))
      const result = await agent.createDesktopPlan(objective, surfaces.workerGeneration, selected.surfaceRef, selected.surfaceEpoch, payload)
      if (!mounted.current) return
      if (result.ok) setPlan(result.value)
      else setMessage(result.error.message)
    })
  }

  function allowOnce(view: AgentDesktopPlanView): void {
    const card = view.card
    if (!card) return
    void run('allow', async () => {
      setMessage(undefined)
      const granted = await agent.grantDesktopPlan(card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (!granted.ok) {
        setMessage(granted.error.message)
        const latest = await agent.getDesktopPlan()
        if (mounted.current && latest.ok) setPlan(latest.value)
        return
      }
      setPlan(granted.value)
      if (granted.value.phase !== 'approved') return
      setPlan({ ...granted.value, phase: 'reasoning' })
      const planned = await agent.runDesktopPlan()
      if (!mounted.current) return
      if (planned.ok) setPlan(planned.value)
      else {
        setMessage(planned.error.message)
        const latest = await agent.getDesktopPlan()
        if (mounted.current && latest.ok) setPlan(latest.value)
      }
    })
  }

  function cancel(view: AgentDesktopPlanView): void {
    const card = view.card
    if (!card) return
    void run('cancel', async () => {
      const result = await agent.declineDesktopPlan(card.grantId, card.grantRevision)
      if (!mounted.current) return
      if (result.ok) {
        setPlan(result.value)
        setMessage('Cancelled. Nothing was sent.')
      } else {
        setMessage(result.error.message)
      }
    })
  }

  function review(view: AgentDesktopPlanView): void {
    if (!view.plan?.planId) return
    void run('review', async () => {
      setMessage(undefined)
      const result = await agent.proposeDesktopActionFromPlan(view.plan!.planId)
      if (!mounted.current) return
      if (result.ok) setAction(result.value)
      else setMessage(result.error.message)
    })
  }

  function approveAction(view: AgentDesktopActionView): void {
    void run('approve', async () => {
      const result = await agent.approveDesktopAction(view.actionId, view.revision)
      if (!mounted.current) return
      if (result.ok) {
        setAction(result.value)
      } else {
        setMessage(result.error.message)
        const latest = await agent.getDesktopAction()
        if (mounted.current && latest.ok) setAction(latest.value)
      }
    })
  }

  function cancelAction(view: AgentDesktopActionView): void {
    void run('cancel-action', async () => {
      const result = await agent.declineDesktopAction(view.actionId, view.revision)
      if (mounted.current && result.ok) setAction(result.value)
    })
  }

  function reconcileAction(view: AgentDesktopActionView, outcome: 'succeeded' | 'failed' | 'still_unknown'): void {
    void run('reconcile', async () => {
      const result = await agent.reconcileDesktopAction(view.actionId, view.revision, outcome)
      if (mounted.current && result.ok) setAction(result.value)
    })
  }

  function startOver(): void {
    setPlan(null)
    setAction(null)
    setObjective('')
    setValues([])
    setMessage(undefined)
    void loadSurfaces()
  }

  const showChooser = !action && (!plan || ['declined', 'expired', 'failed', 'outcome_unknown'].includes(plan.phase))
  const canPropose = Boolean(selected) && objective.trim().length > 0 && objective.length <= MAX_OBJECTIVE
    && values.every((row) => row.classification.trim().length === 0 || (row.value.length > 0 && row.value.length <= MAX_VALUE_CHARS))
    && !busy

  return (
    <div className="agent-task-panel" data-testid="desktop-planning-panel">
      <header className="settings-header">
        <h2>Ask Lumi to do something in a window</h2>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <p className="workspace-note">
          Lumi reads a window, has an AI propose ONE bounded step (set a value, select an option, or use a
          control), and shows you that exact step on a second card before doing anything. It never sends the
          text of a value you type below to the AI -- only a short label and its length.
        </p>
        {message && <p className="workspace-note" role="status" data-testid="desktop-plan-message">{message}</p>}

        {action && (
          <DesktopActionCard view={action} busy={Boolean(busy)} onApprove={approveAction} onCancel={cancelAction} onReconcile={reconcileAction} />
        )}

        {!action && plan && <DesktopPlanCard view={plan} busy={Boolean(busy)} onAllow={allowOnce} onCancel={cancel} onReview={review} />}

        {showChooser && (
          <section className="agent-booking-card" aria-label="Choose a window and describe the step" data-testid="desktop-plan-chooser">
            <p className="lifelens-card-eyebrow">DESKTOP ACTION</p>
            <h3 className="lifelens-card-heading">1. Choose a window</h3>
            {surfaceError && <p className="workspace-note" role="alert">{surfaceError}</p>}
            <ul className="agent-evidence" role="radiogroup" aria-label="Windows">
              {(surfaces?.surfaces ?? []).map((surface) => {
                const active = selected?.surfaceRef === surface.surfaceRef && selected?.surfaceEpoch === surface.surfaceEpoch
                return (
                  <li key={`${surface.surfaceRef}:${surface.surfaceEpoch}`}>
                    <label>
                      <input type="radio" name="desktop-plan-surface" checked={active} disabled={Boolean(busy)}
                        onChange={() => setSelected(surface)} data-testid="desktop-plan-surface-option" />
                      {' '}
                      <bdi data-testid="desktop-plan-surface-label">{surface.applicationLabel || 'Application'}: {surface.windowTitle || '(no title)'}</bdi>
                      {surface.minimized ? ' (minimized)' : ''}
                    </label>
                  </li>
                )
              })}
            </ul>
            <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void loadSurfaces()}>
              Refresh windows
            </button>
            <h3 className="lifelens-card-heading">2. Describe what you want done</h3>
            <textarea aria-label="What do you want Lumi to do in this window" value={objective} maxLength={MAX_OBJECTIVE} rows={3}
              placeholder="Put my name in the search box" onChange={(event) => setObjective(event.target.value)}
              data-testid="desktop-plan-objective" />
            <h3 className="lifelens-card-heading">3. Values Lumi may write (optional)</h3>
            <p className="workspace-note">
              Give each value a short label (e.g. &quot;search text&quot;). The AI sees only the label and length, never the text.
            </p>
            {values.map((row, index) => (
              <div key={index} className="agent-evidence">
                <input aria-label={`Label for value ${index + 1}`} placeholder="Label (e.g. search text)" value={row.classification}
                  onChange={(event) => updateValue(index, { classification: event.target.value })} data-testid="desktop-plan-value-label" />
                <input aria-label={`Value ${index + 1}`} placeholder="The exact text" value={row.value} maxLength={MAX_VALUE_CHARS}
                  onChange={(event) => updateValue(index, { value: event.target.value })} data-testid="desktop-plan-value-text" />
                <button className="text-button" type="button" onClick={() => removeValue(index)} aria-label={`Remove value ${index + 1}`}>
                  Remove
                </button>
              </div>
            ))}
            {values.length < MAX_VALUES && (
              <button className="text-button" type="button" onClick={addValue} data-testid="desktop-plan-add-value">
                Add a value
              </button>
            )}
            <button className="primary-button" type="button" disabled={!canPropose} onClick={propose} data-testid="desktop-plan-propose">
              Inspect locally
            </button>
            <p className="workspace-note">Nothing is sent to an AI until you approve it on the next card, and nothing runs until you approve a second, separate card after that.</p>
          </section>
        )}

        {(plan || action) && showChooser && (
          <button className="text-button" type="button" disabled={Boolean(busy)} onClick={startOver}>Start over</button>
        )}
      </div>
    </div>
  )
}

export interface DesktopPlanCardProps {
  view: AgentDesktopPlanView
  busy: boolean
  onAllow: (view: AgentDesktopPlanView) => void
  onCancel: (view: AgentDesktopPlanView) => void
  onReview: (view: AgentDesktopPlanView) => void
}

export function DesktopPlanCard({ view, busy, onAllow, onCancel, onReview }: DesktopPlanCardProps) {
  const card = view.card
  const observed = card ? new Date(card.observedAt).toLocaleTimeString() : undefined

  if (view.phase === 'awaiting_approval' && card) {
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Allow desktop action planning"
        data-testid="desktop-plan-card" data-grant-id={card.grantId} data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">ALLOW DESKTOP ACTION PLANNING?</p>
        <p>Lumi captured a semantic snapshot of a window at {observed}. It is showing this window:</p>
        <p data-testid="desktop-plan-target">
          <span className="visually-hidden">Window text from the application, not from Lumi: </span>
          <q><bdi>{card.applicationLabel || 'Application'}: {card.windowTitle || '(no title)'}</bdi></q>
        </p>
        <p>To propose one step, Lumi will send:</p>
        <ul className="agent-evidence">
          <li>UI labels and readable text from this captured snapshot</li>
          <li>selected UI states</li>
          <li>what you typed you want done</li>
          {card.values.length > 0 && <li>the label and length of each value below -- never its text</li>}
        </ul>
        {card.values.length > 0 && (
          <ul className="agent-evidence" data-testid="desktop-plan-values">
            {card.values.map((item) => (
              <li key={item.valueRef}><bdi>{item.classification}</bdi>: <q><bdi>{item.value}</bdi></q></li>
            ))}
          </ul>
        )}
        <p>To:</p>
        <p data-testid="desktop-plan-recipient"><strong>{recipientName(card.recipient)}</strong> ({card.model})</p>
        <p>
          Sensitive identifier patterns (email addresses, phone numbers, card and long numbers) are replaced before sending.
          That is not anonymisation: names and other ordinary text may remain.
        </p>
        {card.observationAvailable && (
          <p data-testid="desktop-plan-size">
            {card.nodeCount ?? 0} controls, about {Math.ceil((card.textBytes ?? 0) / 1024)} KB of text
            {card.redactionCount ? `, ${card.redactionCount} value${card.redactionCount === 1 ? '' : 's'} replaced` : ''}.
          </p>
        )}
        {card.truncated && (
          <p data-testid="desktop-plan-truncated" role="note">
            Lumi captured only part of this window&apos;s interface, so the AI may not see everything in it.
          </p>
        )}
        {!card.observationAvailable && <p role="alert">This snapshot is no longer available. Inspect the window again.</p>}
        <p>This is a proposal only: nothing runs from this approval. You will review a second, separate card for the exact step before anything happens, and Lumi will not click, type, focus, scroll or change the application from this step.</p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onCancel(view)} data-testid="desktop-plan-cancel">
          Cancel
        </button>
        <button className="primary-button" type="button" disabled={busy || !card.observationAvailable} onClick={() => onAllow(view)}
          data-testid="desktop-plan-allow">
          Allow once
        </button>
      </article>
    )
  }

  if (view.phase === 'reasoning') {
    return (
      <article className="agent-booking-card" role="status" aria-live="polite" data-testid="desktop-plan-reasoning" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">PLANNING</p>
        <p>Asking {card ? recipientName(card.recipient) : 'the approved provider'} to propose one step…</p>
      </article>
    )
  }

  if (view.phase === 'proposed') {
    return (
      <article className="agent-booking-card tone-success" role="group" aria-label="Step proposed" data-testid="desktop-plan-proposed" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">STEP PROPOSED</p>
        <p>{describeProposedAction(view)}</p>
        <p className="workspace-note">Nothing has run. Review the exact step before Lumi may do anything.</p>
        <button className="primary-button" type="button" disabled={busy} onClick={() => onReview(view)} data-testid="desktop-plan-review">
          Review the exact step
        </button>
      </article>
    )
  }

  const text = view.phase === 'declined' ? 'Cancelled. Nothing was sent.'
    : view.phase === 'expired' ? 'That approval expired before it was used. Nothing was sent. Inspect the window again.'
      : view.phase === 'outcome_unknown'
        ? 'Lumi cannot tell whether the snapshot reached the AI provider, so it did not try again. Inspect the window again if you still want a step proposed.'
        : view.phase === 'failed'
          ? failureText(view.plan?.errorCode)
          : undefined
  if (!text) return null
  return (
    <article className="agent-booking-card tone-uncertain" role="group" aria-label="Desktop plan ended" data-testid="desktop-plan-ended" data-phase={view.phase}>
      <p>{text}</p>
    </article>
  )
}

function describeProposedAction(view: AgentDesktopPlanView): string {
  const action = view.plan?.proposedAction
  if (!action) return 'Lumi proposed a step.'
  if (action.action === 'invoke') return 'Lumi proposes using one control in the window.'
  if (action.action === 'set_value') return 'Lumi proposes setting one value in the window.'
  return 'Lumi proposes selecting one option in the window.'
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

function failureText(code: string | undefined): string {
  switch (code) {
    case 'unknown_control':
    case 'unknown_value_ref':
      return 'The AI proposed a step that no longer matches the snapshot, so Lumi discarded it. Nothing was sent anywhere else and nothing was retried.'
    case 'unsupported_or_unknown_effect':
      return 'The AI proposed activating a control Lumi does not yet have a reviewed, verifiable way to activate safely, so Lumi refused it. Nothing was retried.'
    case 'invalid_output':
      return 'The AI replied in a form Lumi could not use. Lumi did not try another provider or try again.'
    case 'observation_unavailable':
      return 'The snapshot was no longer available to check the proposal against, so Lumi discarded it.'
    default:
      return 'The AI provider you approved could not propose a step. Lumi did not send the snapshot to another provider and did not try again.'
  }
}
