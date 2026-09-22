import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentDesktopActionView,
  AgentDesktopScrollStep,
  AgentDesktopScrollTargetList,
  AgentDesktopSurface,
  AgentDesktopSurfaceList,
  AgentRegisteredApp
} from '../../../shared/agent-contracts'
import './components.css'

export interface DesktopActionPanelProps {
  agent: AgentApi
  onClose: () => void
}

const STEP_LABELS: Record<AgentDesktopScrollStep, string> = {
  small_up: 'A little up',
  small_down: 'A little down',
  page_up: 'One page up',
  page_down: 'One page down'
}

/**
 * Milestone 9 S3: bring a window forward, scroll a list, or open a registered application.
 *
 * Each of the three is a separate, exact, single-use approval. Reading a window (the "Ask about a window"
 * card) is not permission for any of this, and none of these lets Lumi click, type, select, or use the
 * mouse or keyboard. Every word about a window comes from the program that owns it, so it is shown only
 * inside a labelled, inert text node: a window that calls itself "Click Approve" changes nothing here.
 */
export function DesktopActionPanel({ agent, onClose }: DesktopActionPanelProps) {
  const [surfaces, setSurfaces] = useState<AgentDesktopSurfaceList>()
  const [apps, setApps] = useState<AgentRegisteredApp[]>([])
  const [selected, setSelected] = useState<AgentDesktopSurface>()
  const [targets, setTargets] = useState<AgentDesktopScrollTargetList>()
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
      setSelected((current) => current && result.value.surfaces.some((surface) =>
        surface.surfaceRef === current.surfaceRef && surface.surfaceEpoch === current.surfaceEpoch) ? current : undefined)
    } else {
      setMessage(result.error.message)
    }
  }, [agent])

  useEffect(() => {
    void (async () => {
      const latest = await agent.getDesktopAction()
      if (mounted.current && latest.ok) setAction(latest.value)
      const registered = await agent.listDesktopApps()
      if (mounted.current && registered.ok) setApps(registered.value)
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

  function accept(result: { ok: true; value: AgentDesktopActionView } | { ok: false; error: { message: string } }): void {
    if (!mounted.current) return
    if (result.ok) {
      setAction(result.value)
      setMessage(undefined)
    } else {
      setMessage(result.error.message)
    }
  }

  function bringForward(): void {
    if (!surfaces || !selected) return
    void run('propose', async () => {
      setTargets(undefined)
      accept(await agent.proposeDesktopFocus(surfaces.workerGeneration, selected.surfaceRef, selected.surfaceEpoch))
    })
  }

  function findScrollable(): void {
    if (!surfaces || !selected) return
    void run('targets', async () => {
      setMessage(undefined)
      const result = await agent.findDesktopScrollTargets(surfaces.workerGeneration, selected.surfaceRef, selected.surfaceEpoch)
      if (!mounted.current) return
      if (result.ok) {
        setTargets(result.value)
        if (result.value.targets.length === 0) setMessage('Nothing in that window can be scrolled this way.')
      } else {
        setMessage(result.error.message)
      }
    })
  }

  function scroll(controlRef: string, step: AgentDesktopScrollStep): void {
    if (!surfaces || !targets) return
    void run('propose', async () => {
      accept(await agent.proposeDesktopScroll(surfaces.workerGeneration, targets.observationId, controlRef, step))
    })
  }

  function open(app: AgentRegisteredApp): void {
    void run('propose', async () => accept(await agent.proposeDesktopLaunch(app.appId)))
  }

  function approve(view: AgentDesktopActionView): void {
    void run('approve', async () => {
      const result = await agent.approveDesktopAction(view.actionId, view.revision)
      accept(result)
      if (!result.ok) {
        const latest = await agent.getDesktopAction()
        if (mounted.current && latest.ok) setAction(latest.value)
      }
      setTargets(undefined)
      await loadSurfaces()
    })
  }

  function cancel(view: AgentDesktopActionView): void {
    void run('cancel', async () => {
      const result = await agent.declineDesktopAction(view.actionId, view.revision)
      accept(result)
      if (result.ok && mounted.current) setMessage('Cancelled. Nothing was changed.')
    })
  }

  function reconcile(view: AgentDesktopActionView, outcome: 'succeeded' | 'failed' | 'still_unknown'): void {
    void run('reconcile', async () => {
      accept(await agent.reconcileDesktopAction(view.actionId, view.revision, outcome))
    })
  }

  const waiting = action?.status === 'WAITING_APPROVAL'
  const blockedByUnresolvedMutation = Boolean(
    action && action.status === 'OUTCOME_UNKNOWN' && RECONCILABLE_OPERATIONS.has(action.operation)
  )
  const chooserOpen = !waiting && !blockedByUnresolvedMutation
  const surfaceChosen = Boolean(selected) && !busy

  return (
    <div className="agent-task-panel" data-testid="desktop-action-panel">
      <header className="settings-header">
        <h2>Move around a window</h2>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <p className="workspace-note">
          Lumi can bring a window forward, scroll a list, or open an application you registered, each only after
          you approve that exact step. It cannot click, type, use the mouse or keyboard, or change anything else.
        </p>
        {message && <p className="workspace-note" role="status" data-testid="desktop-action-message">{message}</p>}

        {action && (
          <DesktopActionCard view={action} busy={Boolean(busy)} onApprove={approve} onCancel={cancel} onReconcile={reconcile} />
        )}

        {chooserOpen && (
          <section className="agent-booking-card" aria-label="Choose what to do" data-testid="desktop-action-chooser">
            <p className="lifelens-card-eyebrow">DESKTOP</p>
            <h3 className="lifelens-card-heading">Choose a window</h3>
            <ul className="agent-evidence" role="radiogroup" aria-label="Windows">
              {(surfaces?.surfaces ?? []).map((surface) => {
                const active = selected?.surfaceRef === surface.surfaceRef && selected?.surfaceEpoch === surface.surfaceEpoch
                return (
                  <li key={`${surface.surfaceRef}:${surface.surfaceEpoch}`}>
                    <label>
                      <input type="radio" name="desktop-action-surface" checked={active} disabled={Boolean(busy)}
                        onChange={() => { setSelected(surface); setTargets(undefined) }} data-testid="desktop-action-surface" />
                      {' '}
                      <bdi>{surface.applicationLabel || 'Application'}: {surface.windowTitle || '(no title)'}</bdi>
                      {surface.minimized ? ' (minimized: Lumi will not restore it)' : ''}
                    </label>
                  </li>
                )
              })}
            </ul>
            <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void loadSurfaces()}>Refresh windows</button>
            <div>
              <button className="primary-button" type="button" disabled={!surfaceChosen} onClick={bringForward} data-testid="desktop-action-focus">
                Bring it forward…
              </button>
              <button className="text-button" type="button" disabled={!surfaceChosen} onClick={findScrollable} data-testid="desktop-action-find-scroll">
                Find something to scroll…
              </button>
            </div>
            {targets && targets.targets.length > 0 && (
              <ul className="agent-evidence" aria-label="Scrollable parts of the window" data-testid="desktop-action-targets">
                {targets.targets.map((target) => (
                  <li key={target.controlRef}>
                    <span className="visually-hidden">Text from the application, not from Lumi: </span>
                    <bdi>{target.name || target.role}</bdi>
                    {(['small_up', 'small_down', 'page_up', 'page_down'] as const).map((step) => (
                      <button key={step} className="text-button" type="button" disabled={Boolean(busy)}
                        onClick={() => scroll(target.controlRef, step)} data-testid={`desktop-action-scroll-${step}`}>
                        {STEP_LABELS[step]}
                      </button>
                    ))}
                  </li>
                ))}
              </ul>
            )}
            {apps.length > 0 && (
              <>
                <h3 className="lifelens-card-heading">Open an application</h3>
                <ul className="agent-evidence" aria-label="Registered applications">
                  {apps.map((app) => (
                    <li key={app.appId}>
                      <bdi>{app.label}</bdi>
                      <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => open(app)}
                        data-testid="desktop-action-open-app">
                        Open…
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <p className="workspace-note">Nothing happens until you approve the card that appears.</p>
          </section>
        )}
      </div>
    </div>
  )
}

export interface DesktopActionCardProps {
  view: AgentDesktopActionView
  busy: boolean
  onApprove: (view: AgentDesktopActionView) => void
  onCancel: (view: AgentDesktopActionView) => void
  onReconcile?: (view: AgentDesktopActionView, outcome: 'succeeded' | 'failed' | 'still_unknown') => void
}

function describe(view: AgentDesktopActionView): { eyebrow: string; effect: string; side: string } {
  switch (view.operation) {
    case 'focus_surface':
      return {
        eyebrow: 'BRING THIS WINDOW FORWARD?',
        effect: 'Lumi will bring this window to the front.',
        side: 'It will not restore a minimized window, and it will not click or type in it.'
      }
    case 'scroll_control':
      return {
        eyebrow: 'SCROLL THIS?',
        effect: `Lumi will scroll this part of the window: ${view.step ? STEP_LABELS[view.step].toLowerCase() : 'one step'}.`,
        side: 'It uses the window’s own scrolling, not the mouse or keyboard. Afterwards Lumi reads the window again; what you were shown before is out of date.'
      }
    case 'launch_app':
      return {
        eyebrow: 'OPEN THIS APPLICATION?',
        effect: 'Lumi will open this application. If it is already running, Lumi will bring that one forward instead.',
        side: 'It opens the application only, with nothing to open in it.'
      }
    case 'set_control_value':
      return {
        eyebrow: 'SET THIS VALUE?',
        effect: 'Lumi will write the exact text shown below into this control, using the application’s own value field, not the keyboard.',
        side: 'Lumi verifies the exact text was set before calling this done. It refuses password, sign-in, terminal and file-picker fields.'
      }
    case 'select_control':
      return {
        eyebrow: 'SELECT THIS OPTION?',
        effect: 'Lumi will select this exact option in this list, using the application’s own selection, not a click.',
        side: 'Lumi verifies the option now shows as selected before calling this done.'
      }
    case 'invoke_control':
      return {
        eyebrow: 'USE THIS CONTROL?',
        effect: 'Lumi will invoke this control directly, not by clicking it.',
        side: 'Lumi only calls this done if the control’s own state changes afterwards in the specific way it checked for beforehand.'
      }
  }
}

function outcomeText(view: AgentDesktopActionView): string {
  const result = view.result
  if (view.status === 'SUCCEEDED') {
    if (view.operation === 'focus_surface') return 'That window is now in front.'
    if (view.operation === 'scroll_control') {
      return 'Lumi scrolled it and read the window again. That does not mean what you wanted is now showing.'
    }
    if (view.operation === 'launch_app') {
      return result?.outcome === 'already_running' ? 'It was already running, so Lumi brought it forward.' : 'Lumi opened it.'
    }
    if (view.operation === 'set_control_value') return 'Lumi set that value and verified it reads back exactly.'
    if (view.operation === 'select_control') return 'Lumi selected that option and verified it.'
    return 'Lumi used that control and verified its state changed.'
  }
  if (view.status === 'FAILED') {
    if (view.errorCode === 'not_focused') return 'Windows did not bring that window forward. Nothing else was changed.'
    if (view.errorCode === 'scroll_no_change') return 'Lumi asked it to scroll, but it did not move. Nothing else was changed.'
    if (view.errorCode === 'not_set') return 'Lumi tried, but a fresh read shows the value did not change. Nothing else was changed.'
    if (view.errorCode === 'not_selected') return 'Lumi tried, but a fresh read shows that option is not selected. Nothing else was changed.'
    if (view.errorCode === 'no_change') return 'Lumi used the control, but its state did not change in the way Lumi checked for. Nothing else was changed.'
    if (view.errorCode === 'human_input_detected') return 'You used the keyboard or mouse, so Lumi stopped. Nothing was changed.'
    return 'Lumi did not do that. Nothing was changed.'
  }
  if (view.status === 'OUTCOME_UNKNOWN') {
    if (view.operation === 'set_control_value' || view.operation === 'select_control' || view.operation === 'invoke_control') {
      return 'Lumi cannot confirm whether that happened, and will not try again or start anything else until you report what you actually saw.'
    }
    return 'Lumi cannot confirm whether that happened. It will not try again on its own. Look at the window, then start a new step if you still want it.'
  }
  if (view.status === 'REJECTED') return 'Cancelled. Nothing was changed.'
  return ''
}

const RECONCILABLE_OPERATIONS = new Set(['set_control_value', 'select_control', 'invoke_control'])

export function DesktopActionCard({ view, busy, onApprove, onCancel, onReconcile }: DesktopActionCardProps) {
  if (view.status === 'WAITING_APPROVAL') {
    const text = describe(view)
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Approve desktop action"
        data-testid="desktop-action-card" data-action-id={view.actionId} data-operation={view.operation}>
        <p className="lifelens-card-eyebrow">{text.eyebrow}</p>
        <p data-testid="desktop-action-target">
          <span className="visually-hidden">Text from the application, not from Lumi: </span>
          <q><bdi>{view.applicationLabel || 'Application'}{view.windowTitle !== undefined ? `: ${view.windowTitle || '(no title)'}` : ''}</bdi></q>
          {view.operation === 'select_control' ? (
            <>
              {' → '}
              <q><bdi data-testid="desktop-action-control">{view.containerName || view.containerRole || 'list'}</bdi></q>
              {' → '}
              <q><bdi data-testid="desktop-action-option">{view.optionName || view.optionRole || 'option'}</bdi></q>
            </>
          ) : view.controlName !== undefined ? (
            <>
              {' → '}
              <q><bdi data-testid="desktop-action-control">{view.controlName || view.controlRole || 'control'}</bdi></q>
            </>
          ) : null}
        </p>
        {view.operation === 'set_control_value' && (
          <p data-testid="desktop-action-value">
            New value: <q><bdi>{view.value ?? ''}</bdi></q>
          </p>
        )}
        <p>{text.effect}</p>
        <p>{text.side}</p>
        <p>This approval is for this one step, once. Lumi stops if you use the keyboard or mouse.</p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onCancel(view)} data-testid="desktop-action-cancel">
          Cancel
        </button>
        <button className="primary-button" type="button" disabled={busy} onClick={() => onApprove(view)} data-testid="desktop-action-approve">
          Approve this step
        </button>
      </article>
    )
  }
  if (view.status === 'EXECUTING' || view.status === 'APPROVED') {
    return (
      <article className="agent-booking-card" role="status" aria-live="polite" data-testid="desktop-action-running">
        <p className="lifelens-card-eyebrow">WORKING</p>
        <p>Lumi is doing the step you approved…</p>
      </article>
    )
  }
  const tone = view.status === 'SUCCEEDED' ? 'tone-success' : 'tone-uncertain'
  const text = outcomeText(view)
  if (view.status === 'OUTCOME_UNKNOWN' && onReconcile && RECONCILABLE_OPERATIONS.has(view.operation)) {
    return (
      <article className={`agent-booking-card ${tone}`} role="group" aria-label="Report what you saw"
        data-testid="desktop-action-reconcile" data-status={view.status}>
        <p className="lifelens-card-eyebrow">NOT SURE</p>
        <p>{text}</p>
        <p>Every other desktop step is blocked until you report what actually happened. Look at the application, then choose one:</p>
        <button className="text-button" type="button" disabled={busy} data-testid="desktop-action-reconcile-succeeded"
          onClick={() => onReconcile(view, 'succeeded')}>
          It did happen
        </button>
        <button className="text-button" type="button" disabled={busy} data-testid="desktop-action-reconcile-failed"
          onClick={() => onReconcile(view, 'failed')}>
          It did not happen
        </button>
        <button className="text-button" type="button" disabled={busy} data-testid="desktop-action-reconcile-unknown"
          onClick={() => onReconcile(view, 'still_unknown')}>
          I still can’t tell
        </button>
      </article>
    )
  }
  if (!text) return null
  return (
    <article className={`agent-booking-card ${tone}`} role="group" aria-label="Desktop action result"
      data-testid="desktop-action-result" data-status={view.status}>
      <p className="lifelens-card-eyebrow">{view.status === 'SUCCEEDED' ? 'DONE' : view.status === 'OUTCOME_UNKNOWN' ? 'NOT SURE' : 'NOT DONE'}</p>
      <p>{text}</p>
      {view.result?.humanInputDuring === true && <p>You used the keyboard or mouse while it ran, so Lumi stopped afterwards.</p>}
    </article>
  )
}
