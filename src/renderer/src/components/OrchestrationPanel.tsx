import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentApi } from '../../../shared/agent-contracts'
import type {
  AgentOrchestrationPauseReason,
  AgentOrchestrationStepStatus,
  AgentOrchestrationView
} from '../../../shared/orchestration-contracts'
import './components.css'

export interface OrchestrationPanelProps {
  agent: AgentApi
  onClose: () => void
}

type Result<T> = { ok: true; value: T } | { ok: false; error: { message: string } }

const PAUSE_REASON_TEXT: Record<AgentOrchestrationPauseReason, string> = {
  approval_required: 'Waiting for you to approve the next step, on its own card.',
  budget_exhausted: 'Lumi reached a safety limit for this task and stopped choosing further steps.',
  loop_detected: 'Lumi would have repeated a step that already finished, so it paused instead.',
  capability_unavailable: 'This step needs a capability Lumi cannot yet use on its own.',
  manual_handoff_required: 'Lumi needs you to do something yourself (like signing in) before it can continue.',
  outcome_unknown: 'Lumi is not sure what happened with one step. Check it yourself, then press Continue.'
}

const STEP_STATUS_TEXT: Record<AgentOrchestrationStepStatus, string> = {
  PENDING: 'working',
  AWAITING_APPROVAL: 'needs your approval',
  SUCCEEDED: 'done',
  FAILED: 'did not complete'
}

const STATUS_TEXT: Record<AgentOrchestrationView['status'], string> = {
  RUNNING: 'working',
  PAUSED: 'paused',
  SUCCEEDED: 'finished',
  FAILED: 'failed',
  STOPPED: 'stopped'
}

export interface OrchestrationCardProps {
  /** Absent (or the last one, `STOPPED`) means: show the start form. */
  orchestration?: AgentOrchestrationView
  objective: string
  busy: boolean
  message?: string
  onObjectiveChange: (value: string) => void
  onCreate: () => void
  onRefresh: () => void
  onContinue: () => void
  onStop: () => void
}

/**
 * Milestone 11 S4: the general task cockpit, as inert markup.
 *
 * It shows exactly what the durable orchestration graph holds and nothing else: no raw private value ever
 * appears here beyond what a step's own bounded, controller-authored summary already carries. Continue and
 * Stop are the only actions this card can take -- approving a step's own capability (a research scope, a
 * project's warning card) happens on that capability's own existing surface, not here.
 */
export function OrchestrationCard({
  orchestration, objective, busy, message, onObjectiveChange, onCreate, onRefresh, onContinue, onStop
}: OrchestrationCardProps) {
  const empty = !orchestration || orchestration.status === 'STOPPED'
  const canContinue = orchestration?.status === 'PAUSED'
  const canStop = orchestration?.status === 'RUNNING' || orchestration?.status === 'PAUSED'

  if (empty) {
    return (
      <div data-testid="orchestration-empty">
        <p className="workspace-note">
          Lumi composes its own already-reviewed capabilities to work on this — research, a document, a
          registered project. Every step it takes still needs its own approval, exactly as if you asked for
          it directly.
        </p>
        {orchestration && (
          <p className="workspace-note" data-testid="orchestration-stopped">The last task was stopped. Nothing further was done.</p>
        )}
        <label>What should Lumi work on?
          <input value={objective} maxLength={500} onChange={(event) => onObjectiveChange(event.target.value)} data-testid="orchestration-objective" />
        </label>
        <button className="primary-button" type="button" disabled={busy || !objective.trim()} onClick={onCreate} data-testid="orchestration-create">
          Start
        </button>
        {message && <p role="alert" className="workspace-note" data-testid="orchestration-message">{message}</p>}
      </div>
    )
  }

  return (
    <div data-testid="orchestration-active" data-orchestration={orchestration.orchestrationId} data-status={orchestration.status}>
      <h3 className="lifelens-card-heading">Working on: <bdi>{orchestration.objective}</bdi></h3>
      <p className="workspace-note">
        Status: <strong data-testid="orchestration-status">{STATUS_TEXT[orchestration.status]}</strong>
        {orchestration.pauseReason && (
          <> — <span data-testid="orchestration-pause-reason">{PAUSE_REASON_TEXT[orchestration.pauseReason]}</span></>
        )}
      </p>
      <button className="text-button" type="button" disabled={busy} onClick={onRefresh} data-testid="orchestration-refresh">Refresh</button>
      {canContinue && (
        <button className="primary-button" type="button" disabled={busy} onClick={onContinue} data-testid="orchestration-continue">
          Continue
        </button>
      )}
      {canStop && (
        <button className="text-button" type="button" disabled={busy} onClick={onStop} data-testid="orchestration-stop">
          Stop this task
        </button>
      )}

      <h4>Steps</h4>
      {orchestration.steps.length === 0 ? (
        <p className="workspace-note">Lumi has not chosen a first step yet.</p>
      ) : (
        <ol data-testid="orchestration-steps">
          {orchestration.steps.map((step) => (
            <li key={step.sequence} data-testid="orchestration-step" data-status={step.status}>
              <strong>{step.capabilityId.replace(/_/g, ' ')}</strong> — {STEP_STATUS_TEXT[step.status]}
              {step.resultSummary && <p className="workspace-note"><bdi>{step.resultSummary}</bdi></p>}
            </li>
          ))}
        </ol>
      )}
      {message && <p role="alert" className="workspace-note" data-testid="orchestration-message">{message}</p>}
    </div>
  )
}

export function OrchestrationPanel({ agent, onClose }: OrchestrationPanelProps) {
  const [orchestration, setOrchestration] = useState<AgentOrchestrationView>()
  const [objective, setObjective] = useState('')
  const [message, setMessage] = useState<string>()
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const run = useCallback(async <T,>(work: () => Promise<Result<T>>, done: (value: T) => void) => {
    setBusy(true)
    setMessage(undefined)
    try {
      const result = await work()
      if (!mounted.current) return
      if (result.ok) done(result.value)
      else setMessage(result.error.message)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }, [])

  useEffect(() => { void run(() => agent.getLatestOrchestration(), (value) => { if (value) setOrchestration(value) }) }, [agent, run])

  return (
    <div className="agent-task-panel" data-testid="orchestration-panel">
      <header className="settings-header">
        <h2>General task</h2>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <OrchestrationCard
          orchestration={orchestration}
          objective={objective}
          busy={busy}
          message={message}
          onObjectiveChange={setObjective}
          onCreate={() => void run(() => agent.createOrchestration(objective.trim()), (value) => { setOrchestration(value); setObjective('') })}
          onRefresh={() => { if (orchestration) void run(() => agent.getOrchestration(orchestration.orchestrationId), setOrchestration) }}
          onContinue={() => { if (orchestration) void run(() => agent.continueOrchestration(orchestration.orchestrationId), setOrchestration) }}
          onStop={() => { if (orchestration) void run(() => agent.stopOrchestration(orchestration.orchestrationId), setOrchestration) }}
        />
      </div>
    </div>
  )
}
