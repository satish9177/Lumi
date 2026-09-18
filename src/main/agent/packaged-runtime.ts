import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { parseAllowedHosts } from './public-url-policy'

/**
 * Packaged-app configuration for the agent runtime.
 *
 * An installed Lumi has no developer shell and no `.env`. Its runtime settings
 * live in one small JSON file in the user's profile, read only by Electron
 * main:
 *
 *   %APPDATA%\Lumi\agent-runtime.json
 *   {
 *     "databaseUrl": "postgresql+asyncpg://lumi:<password>@127.0.0.1:5432/lumi_agent",
 *     "clinicSite": "demo"            // or "http://127.0.0.1:<port>", or "none"
 *   }
 *
 * `demo` starts the bundled demonstration clinic site (the same deterministic
 * fixture the tests use, with its catalogue moved to the coming Saturday). The
 * file never reaches the renderer, and nothing here is bundled into the
 * installer. When the file is missing, Lumi writes an example next to it and
 * reports the runtime as not configured.
 */

export const RUNTIME_CONFIG_FILE = 'agent-runtime.json'
export const RUNTIME_CONFIG_EXAMPLE = 'agent-runtime.example.json'

export interface PackagedRuntimeConfig {
  databaseUrl: string
  clinicSite: 'demo' | 'none' | { origin: string }
  headless: boolean
  /** Milestone 7a: public hosts an approved page inspection may open. Empty: none. */
  publicInspectionHosts: string[]
  /**
   * Milestone 7b: allow bounded public research. `true` lets a granted
   * research task reach any host that is not local, private or reserved --
   * which is what research needs, and which the installed app must opt into
   * explicitly. `researchHosts` narrows it to a list instead.
   */
  research: boolean
  researchHosts: string[]
  /** A search endpoint template containing {query}. Empty: no search. */
  researchSearchEndpoint: string
}

export type ConfigResult =
  | { kind: 'ok'; config: PackagedRuntimeConfig }
  | { kind: 'missing' }
  | { kind: 'invalid'; reason: string }

const DATABASE_URL = /^postgresql\+asyncpg:\/\/[^\s"]+$/
const LOOPBACK_ORIGIN = /^http:\/\/127\.0\.0\.1:(\d{1,5})$/

export function parseRuntimeConfig(value: unknown): ConfigResult {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return { kind: 'invalid', reason: 'not an object' }
  const record = value as Record<string, unknown>
  for (const key of Object.keys(record)) {
    if (!['databaseUrl', 'clinicSite', 'headless', 'publicInspectionHosts', 'research', 'researchHosts', 'researchSearchEndpoint', '$comment'].includes(key)) return { kind: 'invalid', reason: `unknown field ${key}` }
  }
  if (typeof record.databaseUrl !== 'string' || !DATABASE_URL.test(record.databaseUrl) || record.databaseUrl.length > 1_000) {
    return { kind: 'invalid', reason: 'databaseUrl must be a postgresql+asyncpg:// URL' }
  }
  let clinicSite: PackagedRuntimeConfig['clinicSite'] = 'demo'
  if (record.clinicSite !== undefined) {
    if (record.clinicSite === 'demo' || record.clinicSite === 'none') {
      clinicSite = record.clinicSite
    } else if (typeof record.clinicSite === 'string' && LOOPBACK_ORIGIN.test(record.clinicSite) &&
        Number(LOOPBACK_ORIGIN.exec(record.clinicSite)?.[1]) >= 1 && Number(LOOPBACK_ORIGIN.exec(record.clinicSite)?.[1]) <= 65_535) {
      clinicSite = { origin: record.clinicSite }
    } else {
      return { kind: 'invalid', reason: 'clinicSite must be "demo", "none" or http://127.0.0.1:<port>' }
    }
  }
  if (record.headless !== undefined && typeof record.headless !== 'boolean') return { kind: 'invalid', reason: 'headless must be true or false' }
  let publicInspectionHosts: string[] = []
  if (record.publicInspectionHosts !== undefined) {
    if (!Array.isArray(record.publicInspectionHosts) || record.publicInspectionHosts.some((host) => typeof host !== 'string')) {
      return { kind: 'invalid', reason: 'publicInspectionHosts must be a list of host names' }
    }
    try {
      publicInspectionHosts = parseAllowedHosts(record.publicInspectionHosts as string[])
    } catch {
      return { kind: 'invalid', reason: 'publicInspectionHosts must be public host names such as github.com' }
    }
  }
  if (record.research !== undefined && typeof record.research !== 'boolean') {
    return { kind: 'invalid', reason: 'research must be true or false' }
  }
  let researchHosts: string[] = []
  if (record.researchHosts !== undefined) {
    if (!Array.isArray(record.researchHosts) || record.researchHosts.some((host) => typeof host !== 'string')) {
      return { kind: 'invalid', reason: 'researchHosts must be a list of host names' }
    }
    try {
      researchHosts = parseAllowedHosts(record.researchHosts as string[])
    } catch {
      return { kind: 'invalid', reason: 'researchHosts must be public host names such as github.com' }
    }
  }
  let researchSearchEndpoint = ''
  if (record.researchSearchEndpoint !== undefined) {
    const endpoint = record.researchSearchEndpoint
    if (
      typeof endpoint !== 'string' ||
      !/^https:\/\/[^\s"'\\]{1,1000}$/.test(endpoint) ||
      endpoint.split('{query}').length !== 2
    ) {
      return { kind: 'invalid', reason: 'researchSearchEndpoint must be an https URL containing one {query}' }
    }
    researchSearchEndpoint = endpoint
  }
  return {
    kind: 'ok',
    config: {
      databaseUrl: record.databaseUrl,
      clinicSite,
      headless: record.headless !== false,
      publicInspectionHosts,
      research: record.research === true,
      researchHosts,
      researchSearchEndpoint
    }
  }
}

export async function readRuntimeConfig(userDataDir: string): Promise<ConfigResult> {
  const path = join(userDataDir, RUNTIME_CONFIG_FILE)
  let raw: string
  try {
    raw = await readFile(path, 'utf8')
  } catch {
    await writeExample(userDataDir).catch(() => undefined)
    return { kind: 'missing' }
  }
  try {
    return parseRuntimeConfig(JSON.parse(raw.replace(/^\uFEFF/, '')))
  } catch {
    return { kind: 'invalid', reason: 'not valid JSON' }
  }
}

async function writeExample(userDataDir: string): Promise<void> {
  const path = join(userDataDir, RUNTIME_CONFIG_EXAMPLE)
  if (existsSync(path)) return
  await mkdir(userDataDir, { recursive: true })
  await writeFile(path, `${JSON.stringify({
    $comment: 'Copy to agent-runtime.json and set your local PostgreSQL URL. See docs/PACKAGING.md.',
    databaseUrl: 'postgresql+asyncpg://lumi:CHANGE_ME@127.0.0.1:5432/lumi_agent',
    clinicSite: 'demo'
  }, null, 2)}\n`, 'utf8')
}

export async function freeLoopbackPort(): Promise<number> {
  return await new Promise((resolvePort, reject) => {
    const server = createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (address === null || typeof address === 'string') {
        server.close()
        reject(new Error('Could not allocate a loopback port.'))
        return
      }
      server.close((error) => error ? reject(error) : resolvePort(address.port))
    })
  })
}

const SAFE_KEYS = ['SystemRoot', 'WINDIR', 'SystemDrive', 'TEMP', 'TMP'] as const

/** The bundled demonstration clinic site, owned by main in packaged demo mode. */
export class DemoClinicSite {
  private child?: ChildProcess

  constructor(
    private readonly pythonPath: string,
    private readonly agentRoot: string,
    private readonly fetchImpl: typeof globalThis.fetch = globalThis.fetch
  ) {}

  async start(timeoutMs = 60_000): Promise<string> {
    const port = await freeLoopbackPort()
    const environment: NodeJS.ProcessEnv = { PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' }
    for (const key of SAFE_KEYS) {
      if (process.env[key] !== undefined) environment[key] = process.env[key]
    }
    // Fixed module, fixed arguments, no shell, loopback only.
    this.child = spawn(this.pythonPath, ['-m', 'evals.sites.appointments.server', '--port', String(port), '--demo-dates', '--parent-pid', String(process.pid)], {
      cwd: this.agentRoot, env: environment, shell: false, windowsHide: true, stdio: 'ignore'
    })
    const origin = `http://127.0.0.1:${port}`
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (this.child.exitCode !== null) throw new Error('The demo clinic site exited during startup.')
      try {
        const response = await this.fetchImpl(`${origin}/`, { redirect: 'error', signal: AbortSignal.timeout(1_000) })
        if (response.ok) return origin
      } catch {
        // Not listening yet.
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 200))
    }
    this.stop()
    throw new Error('The demo clinic site did not start.')
  }

  stop(): void {
    if (this.child && this.child.exitCode === null) this.child.kill()
    this.child = undefined
  }
}
