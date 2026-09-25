import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentApi, AgentBrowserProfileView, AgentDesktopSurface, AgentRegisteredApp } from '../../../shared/agent-contracts'
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
  /** Milestone 12 S3: signed-in profiles the user may attach as an account resource. */
  accountProfiles: AgentBrowserProfileView[]
  accountProfile: string
  onAccountProfileChange: (value: string) => void
  onAttachAccount: () => void
  /** Milestone 12 S4: the currently-live windows the user may attach as a desktop-target resource. */
  desktopSurfaces: AgentDesktopSurface[]
  desktopSurface: string
  onDesktopSurfaceChange: (value: string) => void
  onAttachDesktopTarget: () => void
  /** Milestone 12 S4: the registered applications the user may attach as an app resource. */
  desktopApps: AgentRegisteredApp[]
  desktopApp: string
  onDesktopAppChange: (value: string) => void
  onAttachApp: () => void
  /** Milestone 12 S4: attaches the one currently Lumi-owned, live supervised project run. No picker. */
  onAttachProject: () => void
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
  orchestration, objective, busy, message, onObjectiveChange, onCreate, onRefresh, onContinue, onStop,
  accountProfiles, accountProfile, onAccountProfileChange, onAttachAccount,
  desktopSurfaces, desktopSurface, onDesktopSurfaceChange, onAttachDesktopTarget,
  desktopApps, desktopApp, onDesktopAppChange, onAttachApp, onAttachProject
}: OrchestrationCardProps) {
  const empty = !orchestration || orchestration.status === 'STOPPED'
  const canContinue = orchestration?.status === 'PAUSED'
  const canStop = orchestration?.status === 'RUNNING' || orchestration?.status === 'PAUSED'
  // Milestone 12 S4: every trusted resource attachment is allowed in the same states -- the orchestration
  // still exists and has not concluded. Shared with the account picker (S3), unchanged.
  const canAttachAccount = orchestration?.status === 'RUNNING' || orchestration?.status === 'PAUSED'
  const isManualHandoff = orchestration?.status === 'PAUSED' && orchestration.pauseReason === 'manual_handoff_required'
  const handoffInstruction = isManualHandoff ? orchestration?.steps.at(-1)?.pendingNote : undefined

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
      {isManualHandoff && (
        <div className="agent-booking-card tone-uncertain" role="status" data-testid="orchestration-manual-handoff">
          <p className="lifelens-card-heading">Manual action required</p>
          <p className="workspace-note">
            {handoffInstruction ?? 'Do what the site is asking in the Lumi browser, then return here and choose Continue.'}
          </p>
        </div>
      )}
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

      {canAttachAccount && (
        <div data-testid="orchestration-attach-account">
          <h4>Account</h4>
          {accountProfiles.length === 0 ? (
            <p className="workspace-note">No signed-in account is available to attach. Sign in to one first.</p>
          ) : (
            <>
              <label>Signed-in account
                <select value={accountProfile} data-testid="orchestration-account-select"
                  onChange={(event) => onAccountProfileChange(event.target.value)}>
                  {accountProfiles.map((profile) => (
                    <option key={profile.profileId} value={profile.profileId}>{profile.label} ({profile.site})</option>
                  ))}
                </select>
              </label>
              <button className="text-button" type="button" disabled={busy || !accountProfile}
                onClick={onAttachAccount} data-testid="orchestration-attach-account-button">
                Make this account available to this task
              </button>
            </>
          )}
          {orchestration.resources && orchestration.resources.length > 0 && (
            <ul data-testid="orchestration-resources">
              {orchestration.resources.map((resource) => (
                <li key={resource.ref} data-testid="orchestration-resource"><bdi>{resource.safeLabel}</bdi></li>
              ))}
            </ul>
          )}
        </div>
      )}

      {canAttachAccount && (
        <div data-testid="orchestration-attach-desktop">
          <h4>Desktop window</h4>
          {desktopSurfaces.length === 0 ? (
            <p className="workspace-note">No window is currently visible to attach.</p>
          ) : (
            <>
              <label>Window
                <select value={desktopSurface} data-testid="orchestration-desktop-select"
                  onChange={(event) => onDesktopSurfaceChange(event.target.value)}>
                  {desktopSurfaces.map((surface) => (
                    <option key={surface.surfaceRef} value={surface.surfaceRef}><bdi>{surface.applicationLabel}</bdi></option>
                  ))}
                </select>
              </label>
              <button className="text-button" type="button" disabled={busy || !desktopSurface}
                onClick={onAttachDesktopTarget} data-testid="orchestration-attach-desktop-button">
                Make this window available to this task
              </button>
            </>
          )}
        </div>
      )}

      {canAttachAccount && (
        <div data-testid="orchestration-attach-app">
          <h4>Application</h4>
          {desktopApps.length === 0 ? (
            <p className="workspace-note">No application is registered to attach.</p>
          ) : (
            <>
              <label>Registered application
                <select value={desktopApp} data-testid="orchestration-app-select"
                  onChange={(event) => onDesktopAppChange(event.target.value)}>
                  {desktopApps.map((app) => (
                    <option key={app.appId} value={app.appId}>{app.label}</option>
                  ))}
                </select>
              </label>
              <button className="text-button" type="button" disabled={busy || !desktopApp}
                onClick={onAttachApp} data-testid="orchestration-attach-app-button">
                Make this application available to this task
              </button>
            </>
          )}
        </div>
      )}

      {canAttachAccount && (
        <div data-testid="orchestration-attach-project">
          <h4>Project</h4>
          <button className="text-button" type="button" disabled={busy}
            onClick={onAttachProject} data-testid="orchestration-attach-project-button">
            Make the current project run available to this task
          </button>
        </div>
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
  const [accountProfiles, setAccountProfiles] = useState<AgentBrowserProfileView[]>([])
  const [accountProfile, setAccountProfile] = useState('')
  const [desktopSurfaces, setDesktopSurfaces] = useState<AgentDesktopSurface[]>([])
  const [desktopWorkerGeneration, setDesktopWorkerGeneration] = useState('')
  const [desktopSurface, setDesktopSurface] = useState('')
  const [desktopApps, setDesktopApps] = useState<AgentRegisteredApp[]>([])
  const [desktopApp, setDesktopApp] = useState('')
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

  // Milestone 12 S3: which signed-in profiles may be attached as an `account_context_ref`. Never a
  // renderer-invented list -- the same profiles main itself would offer for a direct account-reading task.
  useEffect(() => {
    let active = true
    void agent.listBrowserProfiles().then((profiles) => {
      if (!active || !mounted.current) return
      const signedIn = profiles.ok ? profiles.value.filter((profile) => profile.status === 'AUTHENTICATED' && !profile.activeTakeover) : []
      setAccountProfiles(signedIn)
      setAccountProfile((current) => (signedIn.some((profile) => profile.profileId === current) ? current : signedIn[0]?.profileId ?? ''))
    })
    return () => { active = false }
  }, [agent])

  // Milestone 12 S4: which windows may be attached as a `desktop_target_ref`. The same listing
  // `DesktopReadPanel`/`DesktopActionPanel` already offer for a direct request -- never a renderer-invented
  // one -- so an attachment names only an opaque `(workerGeneration, surfaceRef, surfaceEpoch)` this same
  // fresh read just showed.
  useEffect(() => {
    let active = true
    void agent.listDesktopSurfaces().then((result) => {
      if (!active || !mounted.current) return
      const surfaces = result.ok ? result.value.surfaces : []
      setDesktopWorkerGeneration(result.ok ? result.value.workerGeneration : '')
      setDesktopSurfaces(surfaces)
      setDesktopSurface((current) => (surfaces.some((surface) => surface.surfaceRef === current) ? current : surfaces[0]?.surfaceRef ?? ''))
    })
    return () => { active = false }
  }, [agent])

  // Milestone 12 S4: which registered applications may be attached as an `app_ref`.
  useEffect(() => {
    let active = true
    void agent.listDesktopApps().then((result) => {
      if (!active || !mounted.current) return
      const apps = result.ok ? result.value : []
      setDesktopApps(apps)
      setDesktopApp((current) => (apps.some((app) => app.appId === current) ? current : apps[0]?.appId ?? ''))
    })
    return () => { active = false }
  }, [agent])

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
          accountProfiles={accountProfiles}
          accountProfile={accountProfile}
          onAccountProfileChange={setAccountProfile}
          onAttachAccount={() => {
            if (orchestration && accountProfile) {
              void run(() => agent.attachApprovedAccount(orchestration.orchestrationId, accountProfile), setOrchestration)
            }
          }}
          desktopSurfaces={desktopSurfaces}
          desktopSurface={desktopSurface}
          onDesktopSurfaceChange={setDesktopSurface}
          onAttachDesktopTarget={() => {
            const surface = desktopSurfaces.find((item) => item.surfaceRef === desktopSurface)
            if (orchestration && surface && desktopWorkerGeneration) {
              void run(
                () => agent.attachApprovedDesktopTarget(orchestration.orchestrationId, desktopWorkerGeneration, surface.surfaceRef, surface.surfaceEpoch),
                setOrchestration
              )
            }
          }}
          desktopApps={desktopApps}
          desktopApp={desktopApp}
          onDesktopAppChange={setDesktopApp}
          onAttachApp={() => {
            if (orchestration && desktopApp) {
              void run(() => agent.attachApprovedApp(orchestration.orchestrationId, desktopApp), setOrchestration)
            }
          }}
          onAttachProject={() => {
            if (orchestration) void run(() => agent.attachApprovedProject(orchestration.orchestrationId), setOrchestration)
          }}
        />
      </div>
    </div>
  )
}
