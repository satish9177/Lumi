import { EventEmitter } from 'node:events'
import type { SpawnOptions } from 'node:child_process'
import { PassThrough } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'

import {
  AgentRuntimeSupervisor,
  RuntimeRestartedError,
  RuntimeUnavailableError,
  isAllowedRuntimeRoute,
  validateRuntimeSettings,
  type AgentRuntimeSupervisorOptions
} from './agent-runtime-supervisor'

class FakeChild extends EventEmitter {
  pid = 4343
  exitCode: number | null = null
  signalCode: NodeJS.Signals | null = null
  readonly readyPipe = new PassThrough()
  readonly stdio = [null, null, null, this.readyPipe]

  override once(event: 'exit' | 'error', listener: (...args: unknown[]) => void): this {
    return super.once(event, listener)
  }

  kill(): boolean {
    this.exit(1)
    return true
  }

  exit(code = 0): void {
    this.exitCode = code
    this.emit('exit', code, null)
  }

  ready(port: number): void {
    this.readyPipe.write(`${JSON.stringify({ event: 'lumi-runtime-ready', port })}\n`)
  }
}

const GENERATION = '11111111-1111-4111-8111-111111111111'
const TASK = '00000000-0000-4000-8000-000000000001'
const TOKEN = 'r'.repeat(43)
const healthy = (): Response => Response.json({ status: 'ok', database: 'ok', runtime_generation: GENERATION })

function supervisorWith(
  fetch: (url: string | URL | Request, init?: RequestInit) => Promise<Response>,
  overrides: Partial<AgentRuntimeSupervisorOptions> = {}
): { supervisor: AgentRuntimeSupervisor; children: FakeChild[]; spawn: ReturnType<typeof vi.fn> } {
  const children: FakeChild[] = []
  const spawn = vi.fn((_executable: string, args: readonly string[], _options: SpawnOptions) => {
    const child = new FakeChild()
    children.push(child)
    queueMicrotask(() => child.ready(Number(args[3])))
    return child
  })
  const supervisor = new AgentRuntimeSupervisor({
    agentRoot: 'C:\\trusted\\agent',
    pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
    pathExists: () => true,
    maximumRestarts: 0,
    findPort: async () => 43123,
    mintToken: () => TOKEN,
    spawnRuntime: spawn,
    fetch: fetch as typeof globalThis.fetch,
    hardKillTree: async (child) => { (child as unknown as FakeChild).exit(1) },
    ...overrides
  })
  return { supervisor, children, spawn }
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

describe('runtime route allowlist', () => {
  it('allows only the fixed domain routes', () => {
    const allowed: Array<['GET' | 'POST', string]> = [
      ['POST', '/tasks'],
      ['GET', '/tasks/' + TASK],
      ['GET', '/tasks/' + TASK + '/events?after_sequence=0&limit=200'],
      ['GET', '/tasks/' + TASK + '/actions?limit=100'],
      ['POST', '/tasks/' + TASK + '/booking/search'],
      ['POST', '/tasks/' + TASK + '/booking/prepare'],
      ['GET', '/actions/' + TASK],
      ['POST', '/actions/' + TASK + '/approval-request'],
      ['POST', '/actions/' + TASK + '/approve'],
      ['POST', '/actions/' + TASK + '/reject'],
      ['POST', '/actions/' + TASK + '/browser-execution'],
      ['POST', '/actions/' + TASK + '/browser-reconciliation']
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)

    const refused: Array<['GET' | 'POST', string]> = [
      ['GET', '/health'],
      ['POST', '/lifecycle/shutdown'],
      ['POST', '/actions/' + TASK + '/attempts'],
      ['POST', '/actions/' + TASK + '/attempts/finish'],
      ['POST', '/actions/' + TASK + '/reconciliation'],
      ['POST', '/actions/' + TASK + '/reconciliation/finish'],
      ['POST', '/tasks/' + TASK + '/actions'],
      ['POST', '/tasks/' + TASK + '/cancel'],
      ['GET', '/actions/' + TASK + '/browser-dispatches'],
      ['GET', '/openapi.json'],
      ['GET', '/docs'],
      ['GET', '/tasks/' + TASK + '/eventsXafter_sequence=0&limit=1'],
      ['GET', '/tasks/' + TASK + '/event?after_sequence=0&limit=1'],
      ['GET', '/tasks/' + TASK + '/events?after_sequence=0&limit=1&x=1'],
      ['POST', '/actions/' + TASK + '/approve/../../lifecycle/shutdown'],
      ['GET', '/tasks/0000000A-0000-4000-8000-00000000000B'],
      ['GET', '//evil.example/tasks/' + TASK],
      ['GET', '/actions/' + TASK + '/approve'],
      ['POST', '/tasks/' + TASK]
    ]
    for (const [method, path] of refused) expect(isAllowedRuntimeRoute(method, path), path).toBe(false)
  })

  it('allows exactly the six authenticated routes and nothing beside them (Milestone 8a S3)', () => {
    const allowed: Array<['GET' | 'POST', string]> = [
      ['GET', '/tasks/' + TASK + '/authenticated'],
      ['POST', '/tasks/' + TASK + '/authenticated/prepare'],
      ['POST', '/tasks/' + TASK + '/authenticated/grant'],
      ['POST', '/tasks/' + TASK + '/authenticated/revoke'],
      ['POST', '/tasks/' + TASK + '/authenticated/steps'],
      ['POST', '/tasks/' + TASK + '/authenticated/answer']
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    const refused: Array<['GET' | 'POST', string]> = [
      ['GET', '/tasks/' + TASK + '/authenticated/steps'],
      ['POST', '/tasks/' + TASK + '/authenticated'],
      ['POST', '/tasks/' + TASK + '/authenticated/grant/extra'],
      ['POST', '/tasks/' + TASK + '/authenticated/scope'],
      ['POST', '/tasks/' + TASK + '/authenticated/observations'],
      ['POST', '/tasks/' + TASK + '/authenticated/steps?url=https://evil.example'],
      ['POST', '/tasks/x/authenticated/grant'],
      ['POST', '/tasks/' + TASK + '/authenticated/../research/grant/../../authenticated/steps']
    ]
    for (const [method, path] of refused) expect(isAllowedRuntimeRoute(method, path), path).toBe(false)
  })

  it('allows exactly the form-planning routes, and no route that saves or reads a saved value (Milestone 8b S5)', () => {
    const allowed: Array<['GET' | 'POST', string]> = [
      ['GET', '/tasks/' + TASK + '/authenticated/form'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/prepare-scope'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/grant'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/revoke'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/planning-context'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/propose'],
      ['POST', '/actions/' + TASK + '/field-disclosure/approve'],
      ['POST', '/actions/' + TASK + '/field-disclosure/reject']
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    const refused: Array<['GET' | 'POST' | 'PUT', string]> = [
      ['GET', '/protected-values'], ['PUT', '/protected-values/email'], ['POST', '/protected-values/email'],
      ['POST', '/tasks/' + TASK + '/authenticated/form'], ['GET', '/tasks/' + TASK + '/authenticated/form/propose'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/fill'], ['POST', '/tasks/' + TASK + '/authenticated/form/freeze'],
      ['POST', '/tasks/' + TASK + '/authenticated/form/grant/extra'], ['GET', '/actions/' + TASK + '/field-disclosure/approve'],
      ['POST', '/actions/' + TASK + '/field-disclosure/execute'], ['POST', '/actions/' + TASK + '/field-disclosure']
    ]
    for (const [method, path] of refused) expect(isAllowedRuntimeRoute(method as never, path), path).toBe(false)
  })

  it('validates runtime settings before they reach the child environment', () => {
    expect(validateRuntimeSettings({ browserSiteOrigin: 'http://127.0.0.1:8801' })).toEqual({
      LUMI_BROWSER_SITE_ORIGIN: 'http://127.0.0.1:8801', LUMI_BROWSER_HEADLESS: 'true'
    })
    for (const origin of ['http://localhost:8801', 'https://127.0.0.1:1', 'http://127.0.0.1:0', 'http://127.0.0.1:8801/x', 'http://127.0.0.1:70000']) {
      expect(() => validateRuntimeSettings({ browserSiteOrigin: origin }), origin).toThrow()
    }
    expect(() => validateRuntimeSettings({ databaseUrl: 'postgresql://x' })).toThrow()
    expect(() => validateRuntimeSettings({ databaseUrl: 'postgresql+asyncpg://a b' })).toThrow()
    expect(validateRuntimeSettings({ databaseUrl: 'postgresql+asyncpg://u:p@127.0.0.1/db_test' })).toEqual({
      DATABASE_URL: 'postgresql+asyncpg://u:p@127.0.0.1/db_test'
    })
  })
})

describe('AgentRuntimeSupervisor.request', () => {
  it('sends the credential only to the current runtime, with controlled settings', async () => {
    const calls: Array<[string, RequestInit | undefined]> = []
    const { supervisor, spawn } = supervisorWith(async (url, init) => {
      calls.push([String(url), init])
      if (String(url).endsWith('/health')) return healthy()
      return Response.json({ ok: true }, { status: 201 })
    }, { runtimeSettings: { browserSiteOrigin: 'http://127.0.0.1:8801', browserHeadless: false } })
    await supervisor.start()
    const env = spawn.mock.calls[0][2]?.env
    expect(env).toMatchObject({ LUMI_BROWSER_SITE_ORIGIN: 'http://127.0.0.1:8801', LUMI_BROWSER_HEADLESS: 'false' })
    expect(env).not.toHaveProperty('BROWSER_WORKER_TOKEN')
    expect(env).not.toHaveProperty('DATABASE_URL')

    const reply = await supervisor.request('POST', '/tasks', { request: { type: 'appointment_booking' } }, 1_000)
    expect(reply).toEqual({ status: 201, body: { ok: true }, generation: GENERATION })
    const [url, init] = calls[calls.length - 1]
    expect(url).toBe('http://127.0.0.1:43123/tasks')
    expect(init).toMatchObject({
      method: 'POST',
      redirect: 'error',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: { type: 'appointment_booking' } })
    })
    expect(init?.headers).not.toHaveProperty('Origin')
    await expect(supervisor.request('POST', '/lifecycle/shutdown', undefined, 1_000)).rejects.toThrow('unlisted')
    expect(calls.some(([called]) => called.endsWith('/lifecycle/shutdown'))).toBe(false)
  })

  it('refuses requests before the runtime is running', async () => {
    const fetch = vi.fn(async () => healthy())
    const { supervisor } = supervisorWith(fetch)
    await expect(supervisor.request('POST', '/tasks', {}, 1_000)).rejects.toBeInstanceOf(RuntimeUnavailableError)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('discards a reply that arrives after the runtime process changed', async () => {
    let finish!: (response: Response) => void
    const { supervisor, children } = supervisorWith(async (url) => {
      if (String(url).endsWith('/health')) return healthy()
      return await new Promise<Response>((resolve) => { finish = resolve })
    })
    await supervisor.start()
    const pending = supervisor.request('POST', '/actions/' + TASK + '/browser-execution', { expected_revision: 3 }, 1_000)
    await tick()
    children[0].exit(1)
    await tick()
    finish(Response.json({ status: 'SUCCEEDED' }))
    await expect(pending).rejects.toBeInstanceOf(RuntimeRestartedError)
  })

  it('reports a dropped request as unconfirmed and sends it once', async () => {
    const fetch = vi.fn(async (url: string | URL | Request) => {
      if (String(url).endsWith('/health')) return healthy()
      throw new TypeError('fetch failed')
    })
    const { supervisor } = supervisorWith(fetch)
    await supervisor.start()
    await expect(supervisor.request('POST', '/actions/' + TASK + '/approve', { expected_revision: 2 }, 1_000))
      .rejects.toBeInstanceOf(RuntimeRestartedError)
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/approve'))).toHaveLength(1)
  })

  it('restarts on explicit request after automatic restarts gave up', async () => {
    const { supervisor, children, spawn } = supervisorWith(async () => healthy(), { hardKillTree: async () => undefined })
    await supervisor.start()
    children[0].exit(1)
    await tick()
    expect(supervisor.status().state).toBe('failed')
    await supervisor.restart()
    expect(spawn).toHaveBeenCalledTimes(2)
    expect(supervisor.status()).toEqual({ state: 'running', generation: GENERATION })
    await supervisor.restart()
    expect(spawn).toHaveBeenCalledTimes(2)
  })
})
