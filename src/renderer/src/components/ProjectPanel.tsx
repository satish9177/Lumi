import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentApi } from '../../../shared/agent-contracts'
import {
  RUN_WARNING,
  type AgentProjectRecipeView,
  type AgentProjectRunCardView,
  type AgentProjectRunView,
  type AgentProjectScriptView,
  type AgentProjectView
} from '../../../shared/project-contracts'
import './components.css'

export interface ProjectPanelProps {
  agent: AgentApi
}

const PHASE_TEXT: Record<AgentProjectRunView['phase'], string> = {
  awaiting_approval: 'Waiting for your approval. Nothing has run.',
  approved: 'Approved. Nothing has run yet.',
  declined: 'Cancelled. Nothing ran.',
  expired: 'That approval expired. Nothing ran.',
  starting: 'Starting…',
  running: 'Running. Not ready yet.',
  ready: 'Ready: its own process answers on the approved port.',
  succeeded: 'Finished successfully.',
  failed: 'Stopped with a problem.',
  stopped: 'Stopped. Only this run’s own processes were ended.',
  ended_with_runtime: 'Ended when Lumi’s runtime restarted.',
  outcome_unknown: 'Lumi cannot tell whether this run is still going. It will not start another; check what happened.'
}

/**
 * Milestone 10 S3: registered projects, recipes and runs.
 *
 * A project folder, a recipe and each run are confirmed in dialogs Lumi itself shows, carrying the warning
 * that the recipe executes the project's code with your permissions. A recipe can only name a script the
 * project declares. The panel never sees a path, a command line or an environment value; the log is shown
 * as inert text.
 */
export function ProjectPanel({ agent }: ProjectPanelProps) {
  const [projects, setProjects] = useState<AgentProjectView[]>([])
  const [recipes, setRecipes] = useState<AgentProjectRecipeView[]>([])
  const [label, setLabel] = useState('')
  const [chosen, setChosen] = useState<string>()
  const [scripts, setScripts] = useState<AgentProjectScriptView[]>([])
  const [script, setScript] = useState('')
  const [readiness, setReadiness] = useState<'http' | 'exit_code'>('http')
  const [port, setPort] = useState('5173')
  const [path, setPath] = useState('/')
  const [timeout, setTimeoutSeconds] = useState('60')
  const [run, setRun] = useState<AgentProjectRunView>()
  const [message, setMessage] = useState<string>()
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const exec = useCallback(async <T,>(work: () => Promise<{ ok: true; value: T } | { ok: false; error: { message: string } }>, done: (value: T) => void) => {
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

  const load = useCallback(async () => {
    await exec(() => agent.listProjects(), setProjects)
    await exec(() => agent.listProjectRecipes(), setRecipes)
    await exec(() => agent.getLatestProjectRun(), (value) => { if (value) setRun(value) })
  }, [agent, exec])
  useEffect(() => { void load() }, [load])

  // A live run's status is refreshed while it may change.
  useEffect(() => {
    if (!run || !['starting', 'running', 'ready'].includes(run.phase)) return undefined
    const timer = window.setInterval(() => {
      void agent.getProjectRun(run.taskId).then((result) => { if (result.ok && mounted.current) setRun(result.value) })
    }, 2000)
    return () => window.clearInterval(timer)
  }, [agent, run])

  const recipe = {
    label: `npm run ${script}`,
    script,
    readinessKind: readiness,
    ...(readiness === 'http' ? { readyPort: Number(port), readyPath: path || '/' } : {}),
    timeoutSeconds: Number(timeout),
    env: {}
  }

  return (
    <section data-testid="project-panel">
      <h3 className="lifelens-card-heading">Projects</h3>
      <p className="workspace-note">{RUN_WARNING} Lumi never installs dependencies, runs Git or opens a terminal.</p>
      <ul data-testid="project-list">
        {projects.map((project) => (
          <li key={project.projectId}>
            <bdi>{project.label}</bdi>
            <button className="text-button" type="button" disabled={busy}
              onClick={() => void exec(() => agent.listProjectScripts(project.projectId), (value) => { setChosen(project.projectId); setScripts(value); setScript(value[0]?.name ?? '') })}
              data-testid="project-scripts">
              New recipe
            </button>
            <button className="text-button" type="button" disabled={busy}
              onClick={() => void exec(() => agent.revokeProject(project.projectId, project.revision), () => void load())} data-testid="project-revoke">
              Remove
            </button>
          </li>
        ))}
      </ul>
      <label>Name <input value={label} maxLength={64} onChange={(event) => setLabel(event.target.value)} data-testid="project-label" /></label>
      <button className="primary-button" type="button" disabled={busy || !label.trim()}
        onClick={() => void exec(() => agent.addProject(label.trim()), () => { setLabel(''); void load() })} data-testid="project-add">
        Choose project folder…
      </button>

      {chosen && (
        <div className="lifelens-card" data-testid="recipe-form">
          <label>Script
            <select value={script} onChange={(event) => setScript(event.target.value)} data-testid="recipe-script">
              {scripts.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
            </select>
          </label>
          <p className="workspace-note">Runs: <code><bdi data-testid="recipe-script-text">{scripts.find((item) => item.name === script)?.text ?? ''}</bdi></code></p>
          <label>Ready when
            <select value={readiness} onChange={(event) => setReadiness(event.target.value === 'exit_code' ? 'exit_code' : 'http')} data-testid="recipe-readiness">
              <option value="http">it answers on a local port</option>
              <option value="exit_code">it exits successfully</option>
            </select>
          </label>
          {readiness === 'http' && (
            <>
              <label>Port <input value={port} inputMode="numeric" onChange={(event) => setPort(event.target.value)} data-testid="recipe-port" /></label>
              <label>Path <input value={path} maxLength={128} onChange={(event) => setPath(event.target.value)} data-testid="recipe-path" /></label>
            </>
          )}
          <label>Timeout (s) <input value={timeout} inputMode="numeric" onChange={(event) => setTimeoutSeconds(event.target.value)} data-testid="recipe-timeout" /></label>
          <button className="primary-button" type="button" disabled={busy || !script}
            onClick={() => void exec(() => agent.createProjectRecipe(chosen, recipe), (value) => { if (value) { setChosen(undefined); void load() } })}
            data-testid="recipe-create">
            Register recipe…
          </button>
        </div>
      )}

      <ul data-testid="recipe-list">
        {recipes.map((item) => (
          <li key={item.recipeId}>
            <bdi>{item.label}</bdi> {item.status === 'INVALIDATED' ? `— changed (${item.invalidReason ?? 'recipe_changed'}); register it again` : ''}
            <button className="text-button" type="button" disabled={busy || item.status !== 'ACTIVE'}
              onClick={() => void exec(() => agent.createProjectRun(item.recipeId), setRun)} data-testid="recipe-run">
              Run…
            </button>
            <button className="text-button" type="button" disabled={busy}
              onClick={() => void exec(() => agent.revokeProjectRecipe(item.recipeId, item.revision), () => void load())} data-testid="recipe-revoke">
              Remove
            </button>
          </li>
        ))}
      </ul>

      {run && (
        <ProjectRunCard run={run} busy={busy}
          onAllow={(card) => void exec(() => agent.grantProjectRun(run.taskId, card.grantId, card.grantRevision), (value) => { if (value) setRun(value) })}
          onDecline={(card) => void exec(() => agent.declineProjectRun(run.taskId, card.grantId, card.grantRevision), setRun)}
          onStart={() => void exec(() => agent.startProjectRun(run.taskId), setRun)}
          onStop={() => void exec(() => agent.stopProjectRun(run.taskId), setRun)}
          onReconcile={() => void exec(() => agent.reconcileProjectRun(run.taskId), setRun)} />
      )}
      {message && <p role="alert" className="workspace-note" data-testid="project-message">{message}</p>}
    </section>
  )
}

export interface ProjectRunCardProps {
  run: AgentProjectRunView
  busy: boolean
  onAllow: (card: AgentProjectRunCardView) => void
  onDecline: (card: AgentProjectRunCardView) => void
  onStart: () => void
  onStop: () => void
  onReconcile: () => void
}

/** The trusted run card. Every value comes from the runtime's record and is rendered as inert text. */
export function ProjectRunCard({ run, busy, onAllow, onDecline, onStart, onStop, onReconcile }: ProjectRunCardProps) {
  const card = run.card
  const phase = run.phase
  return (
    <div className="lifelens-card" data-testid="project-run-card" data-phase={phase}>
      {card && (
        <>
          <p className="workspace-note" data-testid="project-run-warning"><strong>{card.warning}</strong></p>
          <dl>
            <dt>Project</dt><dd><bdi>{card.projectLabel}</bdi></dd>
            <dt>Runs</dt><dd><code><bdi data-testid="project-run-argv">{card.argv.join(' ')}</bdi></code></dd>
            <dt>Script</dt><dd><code><bdi data-testid="project-run-script">{card.scriptText}</bdi></code></dd>
            {card.preText && (<><dt>First</dt><dd><code><bdi>{card.preText}</bdi></code></dd></>)}
            {card.postText && (<><dt>After</dt><dd><code><bdi>{card.postText}</bdi></code></dd></>)}
            <dt>Variables</dt><dd>{card.envNames.length ? card.envNames.join(', ') : 'none'} (plus Lumi’s own safe basics; never your keys)</dd>
            <dt>Ready when</dt><dd>{card.readinessKind === 'http' ? `it answers on port ${card.readyPort ?? ''}${card.readyPath ?? ''}` : 'it exits successfully'}</dd>
            <dt>Stops</dt><dd>when you press Stop, or after {card.timeoutSeconds} s if not ready — only its own processes</dd>
          </dl>
        </>
      )}
      <p className="workspace-note" data-testid="project-run-phase">{PHASE_TEXT[phase]}</p>
      {run.errorCode && <p className="workspace-note" data-testid="project-run-error">{run.errorCode}{run.exitCode !== undefined ? ` (exit ${run.exitCode})` : ''}</p>}
      {phase === 'awaiting_approval' && card && (
        <>
          <button className="primary-button" type="button" disabled={busy} onClick={() => onAllow(card)} data-testid="project-run-allow">Allow this run…</button>
          <button className="text-button" type="button" disabled={busy} onClick={() => onDecline(card)} data-testid="project-run-decline">Cancel</button>
        </>
      )}
      {phase === 'approved' && (
        <button className="primary-button" type="button" disabled={busy} onClick={onStart} data-testid="project-run-start">Start</button>
      )}
      {(phase === 'running' || phase === 'ready') && (
        <button className="primary-button" type="button" disabled={busy} onClick={onStop} data-testid="project-run-stop">Stop</button>
      )}
      {phase === 'outcome_unknown' && (
        <button className="primary-button" type="button" disabled={busy} onClick={onReconcile} data-testid="project-run-reconcile">Check what happened</button>
      )}
      {run.logTail.length > 0 && (
        <figure>
          <figcaption className="workspace-note">Output from the project (not from Lumi)</figcaption>
          <pre data-testid="project-run-log">{run.logTail.join('\n')}</pre>
        </figure>
      )}
    </div>
  )
}
