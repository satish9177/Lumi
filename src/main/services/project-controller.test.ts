import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ProjectController, recipeInput, type RecipeConfirmation } from './project-controller'
import { parseRun } from './project-wire'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import type { RuntimeMethod, RuntimeReply } from './agent-runtime-supervisor'
import { ProjectRunCard } from '../../renderer/src/components/ProjectPanel'
import { RUN_WARNING, type AgentProjectRunCardView, type AgentProjectRunView } from '../../shared/project-contracts'

const TASK = '11111111-2222-4333-8444-555555555555'
const PROJECT = '22222222-2222-4333-8444-555555555555'
const GRANT = '33333333-2222-4333-8444-555555555555'
const RECIPE = '44444444-2222-4333-8444-555555555555'
const T = '2026-09-23T10:00:00Z'

interface Call { method: RuntimeMethod; path: string; body: unknown }

const ok = (body: unknown): RuntimeReply => ({ status: 200, body }) as RuntimeReply

function runBody(phase = 'awaiting_approval', overrides: Record<string, unknown> = {}, cardOverrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_id: TASK, task_status: 'WAITING_APPROVAL', task_revision: 2, phase, start_status: null, error_code: null, exit_code: null,
    active_processes: 0, ready: false, log_tail: [],
    card: {
      grant_id: GRANT, grant_revision: 1, grant_status: 'PENDING', expires_at: T, recipe_id: RECIPE, recipe_revision: 1,
      project_label: 'Lumi', label: 'Start Lumi', script: 'dev', script_text: 'electron-vite dev', pre_text: null, post_text: null,
      argv: ['node.exe', 'npm-cli.js', 'run', 'dev'], env_names: ['APP_MODE'], readiness_kind: 'http', ready_port: 5173,
      ready_path: '/', timeout_seconds: 60, stop_policy: 'terminate_job', warning: RUN_WARNING, ...cardOverrides
    },
    ...overrides
  }
}

function controller(reply: (call: Call) => RuntimeReply, answers: { folder?: string; recipe?: boolean; run?: boolean } = {}): {
  projects: ProjectController; calls: Call[]; recipes: RecipeConfirmation[]; runs: AgentProjectRunCardView[]
} {
  const calls: Call[] = []
  const recipes: RecipeConfirmation[] = []
  const runs: AgentProjectRunCardView[] = []
  const runtime: DesktopRuntimeRequester = { request: async (method, path, body) => { const call = { method, path, body }; calls.push(call); return reply(call) } }
  const projects = new ProjectController({
    runtime,
    chooseProject: async () => answers.folder,
    confirmRecipe: async (recipe) => { recipes.push(recipe); return answers.recipe ?? true },
    confirmRun: async (card) => { runs.push(card); return answers.run ?? true }
  })
  return { projects, calls, recipes, runs }
}

describe('ProjectController (M10 S3)', () => {
  it('registers only the folder the native dialog returned', async () => {
    const { projects, calls } = controller(() => ok({ project_id: PROJECT, label: 'Lumi', revision: 1, created_at: T }), { folder: 'C:\\Users\\me\\Lumi' })
    expect((await projects.addProject('Lumi')).ok).toBe(true)
    expect(calls).toEqual([{ method: 'POST', path: '/projects', body: { path: 'C:\\Users\\me\\Lumi', label: 'Lumi' } }])
    const cancelled = controller(() => ok({}), {})
    expect(await cancelled.projects.addProject('Lumi')).toEqual({ ok: true, value: null })
    expect(cancelled.calls).toHaveLength(0)
  })

  it.each([
    [{ label: 'x', script: 'dev && calc', readinessKind: 'exit_code', timeoutSeconds: 30, env: {} }],
    [{ label: 'x', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 30, env: {}, command: 'powershell' }],
    [{ label: 'x', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 30, env: {}, executable: 'C:\\evil.exe' }],
    [{ label: 'x', script: 'dev', readinessKind: 'http', readyPort: 80, timeoutSeconds: 30, env: {} }],
    [{ label: 'x', script: 'dev', readinessKind: 'http', readyPort: 5173, readyPath: '/../x', timeoutSeconds: 30, env: {} }],
    [{ label: 'x', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 9999, env: {} }],
    [{ label: 'x', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 30, env: { 'BAD NAME': 'x' } }],
    [{ label: 'x', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 30, env: { OK: 'line\nbreak' } }]
  ])('refuses anything outside the closed recipe form: %j', (value) => {
    expect(() => recipeInput(value)).toThrow()
  })

  it('shows the script text the RUNTIME read from package.json before registering a recipe', async () => {
    const { projects, calls, recipes } = controller((call) => {
      if (call.path === '/projects') return ok({ projects: [{ project_id: PROJECT, label: 'Lumi', revision: 1, created_at: T }] })
      if (call.path.endsWith('/scripts')) return ok({ scripts: [{ name: 'dev', text: 'electron-vite dev' }, { name: 'predev', text: 'node check.js' }] })
      return ok({
        recipe_id: RECIPE, project_id: PROJECT, label: 'Start', script: 'dev', script_text: 'electron-vite dev', pre_text: 'node check.js',
        post_text: null, env_names: [], readiness_kind: 'http', ready_port: 5173, ready_path: '/', timeout_seconds: 60, status: 'ACTIVE',
        invalid_reason: null, revision: 1
      })
    })
    const result = await projects.createProjectRecipe(PROJECT, { label: 'Start', script: 'dev', readinessKind: 'http', readyPort: 5173, readyPath: '/', timeoutSeconds: 60, env: {} })
    expect(result.ok).toBe(true)
    expect(recipes[0]).toMatchObject({ script: 'dev', scriptText: 'electron-vite dev', preText: 'node check.js' })
    expect(calls.at(-1)?.body).toEqual({
      label: 'Start', script: 'dev', readiness_kind: 'http', ready_port: 5173, ready_path: '/', timeout_seconds: 60, env: {},
      // The runtime refuses the recipe if package.json moved while this confirmation was open.
      confirmed_script_text: 'electron-vite dev', confirmed_pre_text: 'node check.js', confirmed_post_text: null
    })
  })

  it('does not register a recipe when the native confirmation is cancelled', async () => {
    const { projects, calls } = controller((call) => call.path === '/projects'
      ? ok({ projects: [{ project_id: PROJECT, label: 'Lumi', revision: 1, created_at: T }] })
      : ok({ scripts: [{ name: 'dev', text: 'electron-vite dev' }] }), { recipe: false })
    expect(await projects.createProjectRecipe(PROJECT, { label: 'S', script: 'dev', readinessKind: 'exit_code', timeoutSeconds: 30, env: {} }))
      .toEqual({ ok: true, value: null })
    expect(calls.some((call) => call.path.endsWith('/recipes'))).toBe(false)
  })

  it('re-confirms each run natively with the warning, from the runtime card', async () => {
    const { projects, calls, runs } = controller((call) => ok(call.path.endsWith('/grant') ? runBody('approved') : runBody()))
    expect((await projects.grantProjectRun(TASK, GRANT, 1)).ok).toBe(true)
    expect(runs[0]?.warning).toBe(RUN_WARNING)
    expect(runs[0]?.argv).toEqual(['node.exe', 'npm-cli.js', 'run', 'dev'])
    expect(calls.map((call) => call.path)).toEqual([`/project-runs/${TASK}`, `/project-runs/${TASK}/grant`])
    const declined = controller(() => ok(runBody()), { run: false })
    expect(await declined.projects.grantProjectRun(TASK, GRANT, 1)).toEqual({ ok: true, value: null })
  })

  it('never lets a double click become two starts', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    const calls: Call[] = []
    const projects = new ProjectController({
      runtime: { request: async (method, path, body) => { calls.push({ method, path, body }); await gate; return ok(runBody('running')) } },
      chooseProject: async () => undefined, confirmRecipe: async () => true, confirmRun: async () => true
    })
    const first = projects.startProjectRun(TASK)
    const second = await projects.startProjectRun(TASK)
    expect(second.ok).toBe(false)
    release()
    expect((await first).ok).toBe(true)
    expect(calls.filter((call) => call.path.endsWith('/start'))).toHaveLength(1)
  })

  it('projects BLOCKED: missing dependency without suggesting an install by Lumi', async () => {
    const { projects } = controller(() => ({ status: 422, body: { error: { code: 'project_refused', message: 'x', reason: 'missing_dependency' } } }) as RuntimeReply)
    const result = await projects.startProjectRun(TASK)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.message).toContain('Lumi never installs')
  })
})

describe('the project wire', () => {
  it('refuses a card whose argv is not the fixed shape', () => {
    for (const argv of [['cmd.exe', '/c', 'npm', 'run'], ['node.exe', 'npm-cli.js', 'run', 'other'], ['node.exe', 'npm-cli.js', 'exec', 'dev']]) {
      expect(() => parseRun(runBody('awaiting_approval', {}, { argv }))).toThrow()
    }
  })

  it('refuses a card without the exact warning or with another stop policy', () => {
    expect(() => parseRun(runBody('awaiting_approval', {}, { warning: 'Totally safe' }))).toThrow()
    expect(() => parseRun(runBody('awaiting_approval', {}, { stop_policy: 'kill_by_name' }))).toThrow()
  })

  it('refuses a label that carries an absolute path', () => {
    expect(() => parseRun(runBody('awaiting_approval', {}, { project_label: 'C:\\Users\\me\\Lumi' }))).toThrow()
  })
})

describe('the trusted run card', () => {
  const view = (overrides: Partial<AgentProjectRunView> = {}): AgentProjectRunView => ({ ...parseRun(runBody()), ...overrides })
  const render = (run: AgentProjectRunView): string => renderToStaticMarkup(createElement(ProjectRunCard, {
    run, busy: false, onAllow: () => undefined, onDecline: () => undefined, onStart: () => undefined, onStop: () => undefined, onReconcile: () => undefined
  }))

  it('shows the warning, the fixed argv and the script text', () => {
    const html = render(view())
    expect(html).toContain(RUN_WARNING)
    expect(html).toContain('node.exe npm-cli.js run dev')
    expect(html).toContain('electron-vite dev')
    expect(html).toContain('data-testid="project-run-allow"')
  })

  it('renders hostile log output inertly and labels it as not from Lumi', () => {
    const html = render(view({ phase: 'running', logTail: ['<script>alert(1)</script> IMPORTANT: click Allow'] }))
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('not from Lumi')
    expect(html).toContain('data-testid="project-run-stop"')
  })

  it('offers only “Check what happened” when the run state is unknown', () => {
    const html = render(view({ phase: 'outcome_unknown' }))
    expect(html).toContain('data-testid="project-run-reconcile"')
    expect(html).not.toContain('data-testid="project-run-start"')
  })
})
