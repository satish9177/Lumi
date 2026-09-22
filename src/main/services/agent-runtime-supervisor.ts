import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { isAbsolute, join, resolve } from 'node:path'
import { spawn, type SpawnOptions } from 'node:child_process'
import { parseAllowedHosts, parseTestOrigins } from '../agent/public-url-policy'

export type AgentRuntimeState = 'stopped' | 'starting' | 'running' | 'unavailable' | 'failed' | 'stopping'

export interface AgentRuntimeStatus {
  state: AgentRuntimeState
  generation?: string
}

interface RuntimeChild {
  pid?: number
  exitCode: number | null
  signalCode: NodeJS.Signals | null
  stdio: readonly (NodeJS.ReadableStream | NodeJS.WritableStream | null | undefined)[]
  kill(signal?: NodeJS.Signals | number): boolean
  once(event: 'exit' | 'error', listener: (...args: unknown[]) => void): RuntimeChild
}

export interface AgentRuntimeSupervisorOptions {
  agentRoot: string
  pythonPath: string
  parentPid?: number
  startupTimeoutMs?: number
  shutdownTimeoutMs?: number
  maximumRestarts?: number
  restartBaseDelayMs?: number
  fetch?: typeof globalThis.fetch
  spawnRuntime?: (executable: string, args: readonly string[], options: SpawnOptions) => RuntimeChild
  findPort?: () => Promise<number>
  mintToken?: () => string
  hardKillTree?: (child: RuntimeChild) => Promise<void>
  pathExists?: (path: string) => boolean
  onStatus?: (status: AgentRuntimeStatus) => void
  /** Validated runtime settings from trusted main configuration only. */
  runtimeSettings?: AgentRuntimeSettings
}

export interface AgentRuntimeSettings {
  /** The one reviewed fixture origin the runtime-owned worker may visit. */
  browserSiteOrigin?: string
  browserHeadless?: boolean
  /** Development/test override of the runtime's own `.env` database; the packaged app's only database. */
  databaseUrl?: string
  /** Packaged builds: the bundled Chromium directory. */
  browsersPath?: string
  /** Packaged builds: bring the schema to the Alembic head before starting. */
  migrate?: boolean
  /** Milestone 7a: hosts an approved page inspection may open. Trusted configuration only. */
  publicInspectionHosts?: readonly string[]
  /** Unpackaged builds: exact http://127.0.0.1:<port> origins of controlled test pages. */
  inspectionTestOrigins?: readonly string[]
  /**
   * Milestone 7b public research. `researchAnyPublicHost` is the honest name
   * for what research needs: a research task cannot know its destinations in
   * advance, so the host allowlist layer is replaced by "not local, not
   * private, not reserved, resolving only to globally routable addresses".
   * `researchHosts` narrows it again when configuration wants a list.
   */
  researchAnyPublicHost?: boolean
  researchHosts?: readonly string[]
  /** Unpackaged builds only: exact loopback origins of controlled fixtures. */
  researchTestOrigins?: readonly string[]
  /** A URL template containing `{query}`; empty means research has no search. */
  researchSearchEndpoint?: string
  /**
   * Milestone 8a S2. Unpackaged builds only: exact loopback origins of the
   * synthetic login/SSO fixture the manual-takeover acceptance test drives.
   * Empty in every build that does not test manual login.
   */
  authTestOrigins?: readonly string[]
  /**
   * Milestone 9 S2. Opt in to Windows semantic observation (and so to the desktop-read flow). Off by
   * default, like the browser worker: a deployment cannot acquire the capability by accident.
   */
  desktopObservation?: boolean
  /**
   * Milestone 9 S3: the user's registered applications, a JSON list read once from main's own trusted
   * configuration. Neither the renderer nor a model can add to it; the runtime validates it whole.
   */
  desktopRegisteredApps?: string
}

export type RuntimeMethod = 'GET' | 'POST'

export interface RuntimeReply {
  status: number
  body: unknown
  generation: string
}

/** The runtime is not running (or not yet authenticated). Nothing was sent. */
export class RuntimeUnavailableError extends Error {
  constructor() {
    super('The Lumi agent runtime is not available.')
    this.name = 'RuntimeUnavailableError'
  }
}

/**
 * A request was sent but no trustworthy reply arrived: it timed out, the
 * connection dropped, or the runtime process changed meanwhile. A mutation may
 * or may not have been applied; callers must re-read durable state, never retry.
 */
export class RuntimeRestartedError extends Error {
  constructor() {
    super('The Lumi agent runtime did not confirm the request.')
    this.name = 'RuntimeRestartedError'
  }
}

const UUID_PART = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
/** The complete set of runtime routes main may call. Nothing else is reachable. */
const ALLOWED_ROUTES: ReadonlyArray<{ method: RuntimeMethod; pattern: RegExp }> = [
  { method: 'POST', pattern: /^\/tasks$/ },
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}$`) },
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}/events[?]after_sequence=[0-9]{1,15}&limit=[0-9]{1,3}$`) },
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}/actions[?]limit=[0-9]{1,3}$`) },
  { method: 'POST', pattern: new RegExp(`^/tasks/${UUID_PART}/booking/(search|prepare|criteria|cancel)$`) },
  { method: 'POST', pattern: new RegExp(`^/tasks/${UUID_PART}/info/lookup$`) },
  { method: 'POST', pattern: new RegExp(`^/tasks/${UUID_PART}/inspection/prepare$`) },
  { method: 'GET', pattern: new RegExp(`^/actions/${UUID_PART}/inspection$`) },
  { method: 'POST', pattern: new RegExp(`^/actions/${UUID_PART}/inspection/answer$`) },
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}/research$`) },
  {
    method: 'POST',
    pattern: new RegExp(`^/tasks/${UUID_PART}/research/(prepare|grant|revoke|steps|answer)$`)
  },
  // Milestone 8a S3: authenticated account reading. Six routes, every segment an
  // opaque id or one of six fixed words. `grant` is the trusted click; `steps`
  // takes one closed-union step; none takes or returns an address.
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}/authenticated$`) },
  {
    method: 'POST',
    pattern: new RegExp(`^/tasks/${UUID_PART}/authenticated/(prepare|grant|revoke|steps|answer)$`)
  },
  // Milestone 8b S5: form planning and the exact disclosure approval. Main calls
  // exactly these. `PUT /protected-values/{kind}` is deliberately absent: no route
  // main can reach saves, reads or echoes a saved value.
  { method: 'GET', pattern: new RegExp(`^/tasks/${UUID_PART}/authenticated/form$`) },
  {
    method: 'POST',
    pattern: new RegExp(`^/tasks/${UUID_PART}/authenticated/form/(prepare-scope|grant|revoke|planning-context|propose|preparation-mode|stop)$`)
  },
  // Milestone 8b S6: the network-frozen local draft. Six routes, every segment an opaque id or
  // one of a few fixed words; none takes or returns a value, a manifest, an origin or a URL.
  { method: 'POST', pattern: new RegExp(`^/form-drafts/${UUID_PART}/(discard|handover-request)$`) },
  { method: 'POST', pattern: new RegExp(`^/actions/${UUID_PART}/form-handover/(approve|reject)$`) },
  {
    method: 'POST',
    pattern: new RegExp(`^/actions/${UUID_PART}/field-disclosure/(approve|reject)$`)
  },
  { method: 'GET', pattern: new RegExp(`^/actions/${UUID_PART}$`) },
  {
    method: 'POST',
    pattern: new RegExp(`^/actions/${UUID_PART}/(approval-request|approve|reject|browser-execution|browser-reconciliation)$`)
  },
  // Milestone 8a S1/S2: browser profiles and manual login takeover. Every
  // path segment here is an opaque id; none is a hostname, a path or a
  // credential.
  { method: 'GET', pattern: /^\/browser-profiles$/ },
  { method: 'GET', pattern: new RegExp(`^/browser-profiles/${UUID_PART}$`) },
  { method: 'POST', pattern: new RegExp(`^/browser-profiles/${UUID_PART}/takeover$`) },
  { method: 'GET', pattern: new RegExp(`^/browser-profiles/${UUID_PART}/takeover/${UUID_PART}$`) },
  { method: 'POST', pattern: new RegExp(`^/browser-profiles/${UUID_PART}/takeover/${UUID_PART}/(confirm|cancel)$`) },
  // Milestone 9 S2: exact desktop disclosure. Main calls exactly these. `/desktop/observations` is
  // deliberately absent: no route main can reach observes a surface without also opening the trusted
  // card, so a raw observation can never be requested on its own, and none of these takes or returns a
  // handle, a process, a selector, a coordinate or an action.
  { method: 'GET', pattern: /^\/desktop\/surfaces$/ },
  { method: 'POST', pattern: /^\/desktop\/read-tasks$/ },
  { method: 'GET', pattern: /^\/desktop\/read-tasks\/latest$/ },
  { method: 'GET', pattern: new RegExp(`^/desktop/read-tasks/${UUID_PART}$`) },
  { method: 'POST', pattern: new RegExp(`^/desktop/read-tasks/${UUID_PART}/(grant|revoke|disclosure|result)$`) },
  // Milestone 9 S3/S4: trusted focus, semantic scroll, registered-app launch and (S4) the SECOND,
  // separate execution card built from a validated plan. Each proposal opens an exact approval card
  // and performs nothing; only `approve` (the trusted click) can begin an effect. `reconcile` is the
  // only way out of an unresolved S4 mutation and never performs an effect either.
  { method: 'GET', pattern: /^\/desktop\/actions\/apps$/ },
  { method: 'GET', pattern: /^\/desktop\/actions\/latest$/ },
  { method: 'POST', pattern: /^\/desktop\/actions\/(focus|scroll|launch|scroll-targets|from-plan)$/ },
  { method: 'POST', pattern: new RegExp(`^/desktop/actions/${UUID_PART}/(approve|decline|reconcile)$`) },
  // Milestone 9 S4: exact desktop action-planning disclosure. Disclosure authority only; nothing here
  // performs a desktop action, and `claim`/`result` are internal (main calls them, never the renderer
  // directly).
  { method: 'POST', pattern: /^\/desktop\/action-plans$/ },
  { method: 'GET', pattern: /^\/desktop\/action-plans\/latest$/ },
  { method: 'GET', pattern: new RegExp(`^/desktop/action-plans/${UUID_PART}$`) },
  { method: 'POST', pattern: new RegExp(`^/desktop/action-plans/${UUID_PART}/(grant|revoke|claim|result)$`) },
  // Milestone 9 S5: scoped desktop visual fallback. A capture card, then a SEPARATE vision-disclosure
  // card for the same task. `claim`/`result` are internal, exactly like S2/S4's own claim/result
  // routes. No route here takes or returns a pixel, a coordinate or an action.
  { method: 'POST', pattern: /^\/desktop\/captures$/ },
  { method: 'GET', pattern: new RegExp(`^/desktop/captures/${UUID_PART}$`) },
  { method: 'POST', pattern: new RegExp(`^/desktop/captures/${UUID_PART}/(grant|revoke|claim|disclosure)$`) },
  { method: 'POST', pattern: new RegExp(`^/desktop/captures/${UUID_PART}/disclosure/(grant|revoke|claim|result)$`) }
]

export function isAllowedRuntimeRoute(method: RuntimeMethod, path: string): boolean {
  return ALLOWED_ROUTES.some((route) => route.method === method && route.pattern.test(path))
}

const SITE_ORIGIN = /^http:\/\/127\.0\.0\.1:(\d{1,5})$/

export function validateRuntimeSettings(settings: AgentRuntimeSettings): Record<string, string> {
  const environment: Record<string, string> = {}
  if (settings.browserSiteOrigin !== undefined) {
    const match = SITE_ORIGIN.exec(settings.browserSiteOrigin)
    const port = match ? Number(match[1]) : 0
    if (!match || port < 1 || port > 65_535) throw new Error('The appointment site origin must be http://127.0.0.1:<port>.')
    environment.LUMI_BROWSER_SITE_ORIGIN = settings.browserSiteOrigin
    environment.LUMI_BROWSER_HEADLESS = settings.browserHeadless === false ? 'false' : 'true'
  }
  if (settings.databaseUrl !== undefined) {
    if (!/^postgresql\+asyncpg:\/\/[^\s]+$/.test(settings.databaseUrl) || settings.databaseUrl.length > 1_000) {
      throw new Error('The agent database URL must use postgresql+asyncpg.')
    }
    environment.DATABASE_URL = settings.databaseUrl
  }
  if (settings.publicInspectionHosts !== undefined && settings.publicInspectionHosts.length > 0) {
    environment.LUMI_PUBLIC_INSPECTION_HOSTS = parseAllowedHosts(settings.publicInspectionHosts).join(',')
  }
  if (settings.inspectionTestOrigins !== undefined && settings.inspectionTestOrigins.length > 0) {
    environment.LUMI_INSPECTION_TEST_ORIGINS = parseTestOrigins(settings.inspectionTestOrigins).join(',')
  }
  if (settings.researchAnyPublicHost === true) {
    environment.LUMI_RESEARCH_ANY_PUBLIC_HOST = 'true'
  }
  if (settings.researchHosts !== undefined && settings.researchHosts.length > 0) {
    environment.LUMI_RESEARCH_HOSTS = parseAllowedHosts(settings.researchHosts).join(',')
  }
  if (settings.researchTestOrigins !== undefined && settings.researchTestOrigins.length > 0) {
    environment.LUMI_RESEARCH_TEST_ORIGINS = parseTestOrigins(settings.researchTestOrigins).join(',')
  }
  if (settings.authTestOrigins !== undefined && settings.authTestOrigins.length > 0) {
    environment.LUMI_AUTH_TEST_ORIGINS = parseTestOrigins(settings.authTestOrigins).join(',')
  }
  if (settings.researchSearchEndpoint !== undefined && settings.researchSearchEndpoint !== '') {
    // A URL template, checked here as a shape and again by the runtime against
    // its own destination policy before a single request is made.
    const endpoint = settings.researchSearchEndpoint
    if (
      !/^https?:\/\/[^\s"'\\]{1,1000}$/.test(endpoint) ||
      !endpoint.includes('{query}') ||
      endpoint.split('{query}').length !== 2
    ) {
      throw new Error('The research search endpoint must be an http(s) URL containing exactly one {query}.')
    }
    environment.LUMI_RESEARCH_SEARCH_ENDPOINT = endpoint
  }
  if (settings.desktopObservation === true) environment.LUMI_DESKTOP_OBSERVATION = 'true'
  if (settings.desktopRegisteredApps !== undefined && settings.desktopRegisteredApps !== '') {
    if (settings.desktopRegisteredApps.length > 8_000) throw new Error('The registered application list is too long.')
    environment.LUMI_DESKTOP_REGISTERED_APPS = settings.desktopRegisteredApps
  }
  if (settings.browsersPath !== undefined) {
    if (!isAbsolute(settings.browsersPath) || /["\r\n]/.test(settings.browsersPath) || settings.browsersPath.length > 500) {
      throw new Error('The bundled browser path is invalid.')
    }
    environment.PLAYWRIGHT_BROWSERS_PATH = settings.browsersPath
  }
  return environment
}

interface RuntimeSession {
  epoch: number
  baseUrl: string
  token: string
  child: RuntimeChild
  generation?: string
}

const SAFE_ENVIRONMENT_KEYS = [
  'SystemRoot',
  'WINDIR',
  'SystemDrive',
  'TEMP',
  'TMP',
  'USERPROFILE',
  'LOCALAPPDATA',
  'APPDATA',
  'PROGRAMDATA'
] as const

/** Where electron-builder places the bundled runtime (see scripts/build-agent-runtime.mjs). */
export function packagedAgentRuntimePaths(resourcesPath: string): { agentRoot: string; pythonPath: string; browsersPath: string } {
  const root = join(resolve(resourcesPath), 'agent-runtime')
  return {
    agentRoot: join(root, 'agent'),
    pythonPath: join(root, 'python', 'python.exe'),
    browsersPath: join(root, 'ms-playwright')
  }
}

export function developmentAgentRuntimePaths(appRoot: string): { agentRoot: string; pythonPath: string } {
  const trustedRoot = resolve(appRoot)
  const agentRoot = join(trustedRoot, 'services', 'agent')
  return { agentRoot, pythonPath: join(agentRoot, '.venv', 'Scripts', 'python.exe') }
}

function controlledEnvironment(token: string, parentPid: number, settings: Record<string, string>): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {
    ...settings,
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    // An installed runtime never writes beside its own code.
    PYTHONDONTWRITEBYTECODE: '1',
    LUMI_RUNTIME_TOKEN: token,
    LUMI_RUNTIME_PARENT_PID: String(parentPid),
    LUMI_RUNTIME_READY_FD: '3'
  }
  for (const key of SAFE_ENVIRONMENT_KEYS) {
    const value = process.env[key]
    if (value !== undefined) environment[key] = value
  }
  return environment
}

async function availableLoopbackPort(): Promise<number> {
  return await new Promise((resolvePort, reject) => {
    const server = createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (address === null || typeof address === 'string') {
        server.close()
        reject(new Error('Could not allocate the agent runtime port.'))
        return
      }
      server.close((error) => error ? reject(error) : resolvePort(address.port))
    })
  })
}

async function defaultHardKillTree(child: RuntimeChild): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return
  // app.server owns a Windows kill-on-close job. Terminating its root closes
  // that job and kills every descendant without resolving a utility via PATH.
  child.kill('SIGKILL')
}

function delay(ms: number): Promise<void> {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, ms))
}

export class AgentRuntimeSupervisor {
  private readonly options: Required<Pick<AgentRuntimeSupervisorOptions,
    'parentPid' | 'startupTimeoutMs' | 'shutdownTimeoutMs' | 'maximumRestarts' | 'restartBaseDelayMs' |
    'fetch' | 'spawnRuntime' | 'findPort' | 'mintToken' | 'hardKillTree' | 'pathExists' | 'onStatus'>> &
    Pick<AgentRuntimeSupervisorOptions, 'agentRoot' | 'pythonPath'>
  private state: AgentRuntimeState = 'stopped'
  private session?: RuntimeSession
  private wantsRunning = false
  private epoch = 0
  private restartCount = 0
  private restartTimer?: NodeJS.Timeout
  private exitedChildren = new WeakSet<RuntimeChild>()
  private cleanupPromises = new Map<number, Promise<void>>()
  private launchPromise?: Promise<void>
  private readonly settingsEnvironment: Record<string, string>
  private readonly migrateFirst: boolean
  private migrated = false

  constructor(options: AgentRuntimeSupervisorOptions) {
    this.options = {
      ...options,
      parentPid: options.parentPid ?? process.pid,
      startupTimeoutMs: options.startupTimeoutMs ?? 30_000,
      shutdownTimeoutMs: options.shutdownTimeoutMs ?? 5_000,
      maximumRestarts: options.maximumRestarts ?? 3,
      restartBaseDelayMs: options.restartBaseDelayMs ?? 500,
      fetch: options.fetch ?? globalThis.fetch,
      spawnRuntime: options.spawnRuntime ?? ((executable, args, spawnOptions) => spawn(executable, args, spawnOptions)),
      findPort: options.findPort ?? availableLoopbackPort,
      mintToken: options.mintToken ?? (() => randomBytes(32).toString('base64url')),
      hardKillTree: options.hardKillTree ?? defaultHardKillTree,
      pathExists: options.pathExists ?? existsSync,
      onStatus: options.onStatus ?? (() => undefined)
    }
    this.settingsEnvironment = validateRuntimeSettings(options.runtimeSettings ?? {})
    this.migrateFirst = options.runtimeSettings?.migrate === true
  }

  /**
   * Packaged builds: run the fixed migration entry point once, with the same
   * constructed environment minus the runtime credential. The runtime itself
   * still refuses to serve an unmigrated database.
   */
  private async migrateOnce(): Promise<void> {
    if (!this.migrateFirst || this.migrated) return
    const environment: NodeJS.ProcessEnv = { ...this.settingsEnvironment, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' }
    for (const key of SAFE_ENVIRONMENT_KEYS) {
      const value = process.env[key]
      if (value !== undefined) environment[key] = value
    }
    const child = this.options.spawnRuntime(this.options.pythonPath, ['-m', 'app.migrate'], {
      cwd: this.options.agentRoot, env: environment, shell: false, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore']
    })
    const code = await new Promise<number | null>((resolveExit) => {
      const timer = setTimeout(() => {
        child.kill('SIGKILL')
        resolveExit(null)
      }, 120_000)
      child.once('exit', (exitCode) => {
        clearTimeout(timer)
        resolveExit(typeof exitCode === 'number' ? exitCode : null)
      })
      child.once('error', () => {
        clearTimeout(timer)
        resolveExit(null)
      })
    })
    if (code !== 0) throw new Error('The Lumi agent database could not be prepared.')
    this.migrated = true
  }

  status(): AgentRuntimeStatus {
    return { state: this.state, ...(this.session?.generation ? { generation: this.session.generation } : {}) }
  }

  /**
   * Whether the trusted runtime files exist on this machine at all -- the same
   * check `start()` refuses on, asked without starting anything. Milestone 8a
   * S2's screen-capture guard needs to tell "there is no runtime here, and
   * never was" apart from "the runtime is not answering right now": the first
   * is an answer (no runtime process has ever run here, so no login takeover
   * can exist), the second is an outage and is never treated as one.
   */
  installed(): boolean {
    return this.options.pathExists(this.options.agentRoot) && this.options.pathExists(this.options.pythonPath)
  }

  async start(): Promise<void> {
    if (this.wantsRunning) return
    if (!this.options.pathExists(this.options.agentRoot) || !this.options.pathExists(this.options.pythonPath)) {
      this.setState('failed')
      throw new Error('The trusted Lumi agent runtime is not installed.')
    }
    this.wantsRunning = true
    this.restartCount = 0
    try {
      await this.migrateOnce()
    } catch (error) {
      this.wantsRunning = false
      this.setState('failed')
      throw error
    }
    await this.beginLaunch()
  }

  /** A user-requested restart after the bounded automatic restarts gave up. */
  async restart(): Promise<void> {
    if (this.state === 'running' || this.state === 'starting' || this.state === 'stopping') return
    if (this.wantsRunning && this.restartTimer !== undefined) return
    this.wantsRunning = false
    await this.start()
  }

  /**
   * One authenticated call to an allowlisted route of the current generation.
   *
   * The credential never leaves this class. Redirects are refused, no Origin is
   * sent, and a reply that arrives after the process changed is discarded.
   */
  async request(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    if (!isAllowedRuntimeRoute(method, path)) throw new Error('Refused an unlisted agent runtime route.')
    const session = this.session
    if (this.state !== 'running' || session === undefined || session.generation === undefined) {
      throw new RuntimeUnavailableError()
    }
    const { epoch, generation } = session
    let response: Response
    try {
      response = await this.options.fetch(`${session.baseUrl}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${session.token}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' })
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        redirect: 'error',
        signal: AbortSignal.timeout(timeoutMs)
      })
    } catch {
      // A timeout or dropped connection after sending: the runtime may have
      // applied it. Reported as indeterminate so callers re-read, never retry.
      throw new RuntimeRestartedError()
    }
    let parsed: unknown = undefined
    try {
      parsed = await response.json()
    } catch {
      parsed = undefined
    }
    if (this.session?.epoch !== epoch || this.session.generation !== generation) throw new RuntimeRestartedError()
    return { status: response.status, body: parsed, generation }
  }

  async stop(): Promise<void> {
    this.wantsRunning = false
    if (this.restartTimer !== undefined) {
      clearTimeout(this.restartTimer)
      this.restartTimer = undefined
    }
    if (this.launchPromise !== undefined) {
      try { await this.launchPromise } catch { /* Startup failure is handled below. */ }
    }
    if (this.cleanupPromises.size > 0) {
      await Promise.allSettled([...this.cleanupPromises.values()])
    }
    const session = this.session
    if (session === undefined) {
      this.setState('stopped')
      return
    }
    this.setState('stopping')
    try {
      await this.options.fetch(`${session.baseUrl}/lifecycle/shutdown`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${session.token}` },
        redirect: 'error',
        signal: AbortSignal.timeout(Math.min(1_500, this.options.shutdownTimeoutMs))
      })
    } catch {
      // The process may already be unavailable. The bounded wait and tree kill
      // below are the authority for cleanup.
    }
    const exited = await this.waitForExit(session.child, this.options.shutdownTimeoutMs)
    if (!exited) {
      await this.options.hardKillTree(session.child)
      const killed = await this.waitForExit(session.child, this.options.shutdownTimeoutMs)
      if (!killed) {
        this.setState('failed')
        throw new Error('The Lumi agent runtime could not be terminated.')
      }
    }
    if (this.session?.epoch === session.epoch) this.session = undefined
    this.setState('stopped')
  }

  private async launch(): Promise<void> {
    if (!this.wantsRunning || this.session !== undefined) return
    const token = this.options.mintToken()
    if (token.length < 32) throw new Error('Runtime credential generation failed.')
    let port: number
    try {
      port = await this.options.findPort()
    } catch (error) {
      this.scheduleRestart()
      throw error
    }
    if (!this.wantsRunning || this.session !== undefined) return
    const epoch = ++this.epoch
    let child: RuntimeChild
    try {
      child = this.options.spawnRuntime(
        this.options.pythonPath,
        ['-m', 'app.server', '--port', String(port)],
        {
          cwd: this.options.agentRoot,
          env: controlledEnvironment(token, this.options.parentPid, this.settingsEnvironment),
          shell: false,
          windowsHide: true,
          stdio: ['ignore', 'ignore', 'ignore', 'pipe']
        }
      )
    } catch (error) {
      this.scheduleRestart()
      throw error
    }
    const session: RuntimeSession = { epoch, baseUrl: `http://127.0.0.1:${port}`, token, child }
    this.session = session
    this.setState('starting')
    child.once('exit', () => {
      this.exitedChildren.add(child)
      void this.unexpectedExit(epoch)
    })
    child.once('error', () => { void this.unexpectedExit(epoch) })
    try {
      const generation = await this.waitUntilReady(session)
      if (!this.wantsRunning || this.session?.epoch !== epoch) return
      session.generation = generation
      this.setState('running')
    } catch (error) {
      await this.unexpectedExit(epoch)
      throw error
    }
  }

  private beginLaunch(): Promise<void> {
    const pending = this.launch()
    this.launchPromise = pending
    void pending.finally(() => {
      if (this.launchPromise === pending) this.launchPromise = undefined
    }).catch(() => undefined)
    return pending
  }

  private async waitUntilReady(session: RuntimeSession): Promise<string> {
    await this.waitForReadySignal(session)
    const deadline = Date.now() + this.options.startupTimeoutMs
    while (Date.now() < deadline) {
      if (!this.wantsRunning || this.hasExited(session.child) || this.session?.epoch !== session.epoch) break
      try {
        const response = await this.options.fetch(`${session.baseUrl}/health`, {
          headers: { Authorization: `Bearer ${session.token}` },
          redirect: 'error',
          signal: AbortSignal.timeout(1_000)
        })
        if (response.ok) {
          const body: unknown = await response.json()
          if (isAuthenticatedHealth(body)) return body.runtime_generation
        }
      } catch {
        // Connection refusal is expected while uvicorn and migrations start.
      }
      await delay(100)
    }
    throw new Error('The Lumi agent runtime did not become ready.')
  }

  private async waitForReadySignal(session: RuntimeSession): Promise<void> {
    const ready = session.child.stdio[3]
    if (ready === null || ready === undefined || !('on' in ready)) {
      throw new Error('The Lumi agent runtime readiness pipe is unavailable.')
    }
    await new Promise<void>((resolveReady, reject) => {
      let settled = false
      let buffer = ''
      const finish = (error?: Error): void => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        ready.removeListener('data', onData)
        error === undefined ? resolveReady() : reject(error)
      }
      const onData = (chunk: unknown): void => {
        buffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk)
        if (Buffer.byteLength(buffer, 'utf8') > 256) {
          finish(new Error('The Lumi agent runtime sent invalid readiness metadata.'))
          return
        }
        const newline = buffer.indexOf('\n')
        if (newline < 0) return
        try {
          const value: unknown = JSON.parse(buffer.slice(0, newline))
          if (!isReadySignal(value, Number(new URL(session.baseUrl).port))) {
            finish(new Error('The Lumi agent runtime sent invalid readiness metadata.'))
            return
          }
          finish()
        } catch {
          finish(new Error('The Lumi agent runtime sent invalid readiness metadata.'))
        }
      }
      const timer = setTimeout(
        () => finish(new Error('The Lumi agent runtime did not bind in time.')),
        this.options.startupTimeoutMs
      )
      ready.on('data', onData)
      session.child.once('exit', () => finish(new Error('The Lumi agent runtime exited before binding.')))
      session.child.once('error', () => finish(new Error('The Lumi agent runtime failed before binding.')))
    })
  }

  private unexpectedExit(epoch: number): Promise<void> {
    const existing = this.cleanupPromises.get(epoch)
    if (existing !== undefined) return existing
    const cleanup = this.cleanupUnexpectedExit(epoch)
    this.cleanupPromises.set(epoch, cleanup)
    void cleanup.finally(() => {
      if (this.cleanupPromises.get(epoch) === cleanup) this.cleanupPromises.delete(epoch)
    }).catch(() => undefined)
    return cleanup
  }

  private async cleanupUnexpectedExit(epoch: number): Promise<void> {
    const session = this.session
    if (session?.epoch !== epoch) return
    if (!this.hasExited(session.child)) {
      await this.options.hardKillTree(session.child)
      if (!await this.waitForExit(session.child, this.options.shutdownTimeoutMs)) {
        this.wantsRunning = false
        this.setState('failed')
        return
      }
    }
    if (this.session?.epoch === epoch) this.session = undefined
    if (!this.wantsRunning) return
    this.scheduleRestart()
  }

  private scheduleRestart(): void {
    if (!this.wantsRunning) return
    this.setState('unavailable')
    if (this.restartCount >= this.options.maximumRestarts) {
      this.wantsRunning = false
      this.setState('failed')
      return
    }
    const retry = this.restartCount++
    const waitMs = this.options.restartBaseDelayMs * (2 ** retry)
    this.restartTimer = setTimeout(() => {
      this.restartTimer = undefined
      void this.beginLaunch().catch(() => undefined)
    }, waitMs)
  }

  private async waitForExit(child: RuntimeChild, timeoutMs: number): Promise<boolean> {
    if (this.hasExited(child)) return true
    return await new Promise((resolveExit) => {
      const timer = setTimeout(() => resolveExit(false), timeoutMs)
      child.once('exit', () => {
        clearTimeout(timer)
        resolveExit(true)
      })
    })
  }

  private hasExited(child: RuntimeChild): boolean {
    return child.exitCode !== null || child.signalCode !== null || this.exitedChildren.has(child)
  }

  private setState(state: AgentRuntimeState): void {
    this.state = state
    this.options.onStatus(this.status())
  }
}

function isAuthenticatedHealth(value: unknown): value is { status: 'ok'; database: 'ok'; runtime_generation: string } {
  if (typeof value !== 'object' || value === null) return false
  const body = value as Record<string, unknown>
  return body.status === 'ok' && body.database === 'ok' &&
    typeof body.runtime_generation === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.runtime_generation)
}

function isReadySignal(value: unknown, expectedPort: number): boolean {
  if (typeof value !== 'object' || value === null) return false
  const message = value as Record<string, unknown>
  return Object.keys(message).length === 2 &&
    message.event === 'lumi-runtime-ready' && message.port === expectedPort
}
