import { EventEmitter } from 'node:events'
import type { SpawnOptions } from 'node:child_process'
import { PassThrough } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'

import { AgentRuntimeSupervisor, developmentAgentRuntimePaths } from './agent-runtime-supervisor'

class FakeChild extends EventEmitter {
  pid = 4242
  exitCode: number | null = null
  signalCode: NodeJS.Signals | null = null
  readonly readyPipe = new PassThrough()
  readonly stdio = [null, null, null, this.readyPipe]

  override once(event: 'exit' | 'error', listener: (...args: unknown[]) => void): this {
    return super.once(event, listener)
  }

  kill(): boolean {
    this.exitCode = 1
    this.emit('exit', 1, null)
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

function readySpawn(child: FakeChild): (executable: string, args: readonly string[], options: SpawnOptions) => FakeChild {
  return (_executable, args, _options) => {
    queueMicrotask(() => child.ready(Number(args[3])))
    return child
  }
}

const GENERATION_ONE = '11111111-1111-4111-8111-111111111111'
const GENERATION_TWO = '22222222-2222-4222-8222-222222222222'

function healthy(generation = GENERATION_ONE): Response {
  return Response.json({ status: 'ok', database: 'ok', runtime_generation: generation })
}

describe('AgentRuntimeSupervisor', () => {
  it('uses a fixed venv executable, shell false, a controlled environment, and authenticated readiness', async () => {
    const child = new FakeChild()
    const spawnRuntime = vi.fn(readySpawn(child))
    const fetch = vi.fn(async () => healthy())
    process.env.OPENAI_API_KEY = 'must-not-cross'
    process.env.DATABASE_URL = 'must-not-cross'

    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\services\\agent',
      pythonPath: 'C:\\trusted\\services\\agent\\.venv\\Scripts\\python.exe',
      parentPid: 1234,
      pathExists: () => true,
      findPort: async () => 43123,
      mintToken: () => 'a'.repeat(43),
      spawnRuntime,
      fetch
    })
    await supervisor.start()

    expect(spawnRuntime).toHaveBeenCalledOnce()
    const [executable, args, options] = spawnRuntime.mock.calls[0]
    expect(executable).toBe('C:\\trusted\\services\\agent\\.venv\\Scripts\\python.exe')
    expect(args).toEqual(['-m', 'app.server', '--port', '43123'])
    expect(options).toMatchObject({
      cwd: 'C:\\trusted\\services\\agent',
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'ignore', 'ignore', 'pipe']
    })
    expect(options?.env).toMatchObject({
      LUMI_RUNTIME_TOKEN: 'a'.repeat(43),
      LUMI_RUNTIME_PARENT_PID: '1234',
      PYTHONUTF8: '1',
      PYTHONUNBUFFERED: '1'
    })
    expect(options?.env).not.toHaveProperty('OPENAI_API_KEY')
    expect(options?.env).not.toHaveProperty('DATABASE_URL')
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:43123/health', expect.objectContaining({
      headers: { Authorization: `Bearer ${'a'.repeat(43)}` }
    }))
    expect(supervisor.status()).toEqual({ state: 'running', generation: GENERATION_ONE })

    delete process.env.OPENAI_API_KEY
    delete process.env.DATABASE_URL
  })

  it('mints a new credential for a bounded restart', async () => {
    vi.useFakeTimers()
    const children = [new FakeChild(), new FakeChild()]
    const tokens = ['a'.repeat(43), 'b'.repeat(43)]
    let tokenIndex = 0
    let childIndex = 0
    const spawnRuntime = vi.fn((_executable: string, args: readonly string[], _options: SpawnOptions) => {
      const child = children[childIndex++]
      queueMicrotask(() => child.ready(Number(args[3])))
      return child
    })
    const fetch = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      const authorization = (init?.headers as Record<string, string>).Authorization
      return healthy(authorization.endsWith(tokens[0]) ? GENERATION_ONE : GENERATION_TWO)
    })
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      restartBaseDelayMs: 10,
      maximumRestarts: 1,
      findPort: async () => 43123 + childIndex,
      mintToken: () => tokens[tokenIndex++],
      spawnRuntime,
      fetch,
      hardKillTree: async () => undefined
    })
    await supervisor.start()
    children[0].exit(1)
    await vi.advanceTimersByTimeAsync(10)

    expect(spawnRuntime).toHaveBeenCalledTimes(2)
    expect(supervisor.status()).toEqual({ state: 'running', generation: GENERATION_TWO })
    const firstEnv = spawnRuntime.mock.calls[0][2]?.env
    const secondEnv = spawnRuntime.mock.calls[1][2]?.env
    expect(firstEnv?.LUMI_RUNTIME_TOKEN).toBe(tokens[0])
    expect(secondEnv?.LUMI_RUNTIME_TOKEN).toBe(tokens[1])

    children[1].exit(1)
    await vi.runAllTimersAsync()
    expect(spawnRuntime).toHaveBeenCalledTimes(2)
    expect(supervisor.status()).toEqual({ state: 'failed' })
    vi.useRealTimers()
  })

  it('requests authenticated graceful shutdown before a bounded hard kill', async () => {
    const child = new FakeChild()
    const hardKillTree = vi.fn(async () => child.exit(1))
    const fetch = vi.fn(async (url: string | URL | Request) => {
      if (String(url).endsWith('/health')) return healthy()
      return new Response(null, { status: 202 })
    })
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      shutdownTimeoutMs: 1,
      findPort: async () => 43123,
      mintToken: () => 'c'.repeat(43),
      spawnRuntime: readySpawn(child),
      fetch,
      hardKillTree
    })
    await supervisor.start()
    await supervisor.stop()

    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:43123/lifecycle/shutdown', expect.objectContaining({
      method: 'POST',
      headers: { Authorization: `Bearer ${'c'.repeat(43)}` }
    }))
    expect(hardKillTree).toHaveBeenCalledOnce()
    expect(supervisor.status()).toEqual({ state: 'stopped' })
  })

  it('does not spawn if stop wins while port allocation is in flight', async () => {
    let resolvePort: ((port: number) => void) | undefined
    const findPort = () => new Promise<number>((resolve) => { resolvePort = resolve })
    const spawnRuntime = vi.fn((_executable: string, args: readonly string[], _options: SpawnOptions) => {
      const child = new FakeChild()
      queueMicrotask(() => child.ready(Number(args[3])))
      return child
    })
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      findPort,
      mintToken: () => 'd'.repeat(43),
      spawnRuntime,
      fetch: async () => healthy()
    })

    const starting = supervisor.start()
    const stopping = supervisor.stop()
    resolvePort?.(43123)
    await Promise.all([starting, stopping])

    expect(spawnRuntime).not.toHaveBeenCalled()
    expect(supervisor.status()).toEqual({ state: 'stopped' })
  })

  it('does not report stopped when a live child cannot be killed', async () => {
    const child = new FakeChild()
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      shutdownTimeoutMs: 1,
      findPort: async () => 43123,
      mintToken: () => 'e'.repeat(43),
      spawnRuntime: readySpawn(child),
      fetch: async (url) => String(url).endsWith('/health')
        ? healthy()
        : new Response(null, { status: 202 }),
      hardKillTree: async () => undefined
    })
    await supervisor.start()

    await expect(supervisor.stop()).rejects.toThrow('could not be terminated')
    expect(supervisor.status()).toEqual({ state: 'failed', generation: GENERATION_ONE })
  })

  it('derives the only executable path from the trusted application root', () => {
    expect(developmentAgentRuntimePaths('C:\\Lumi')).toEqual({
      agentRoot: 'C:\\Lumi\\services\\agent',
      pythonPath: 'C:\\Lumi\\services\\agent\\.venv\\Scripts\\python.exe'
    })
  })

  it('never sends the bearer credential before the child-owned bind signal', async () => {
    const child = new FakeChild()
    const fetch = vi.fn(async () => healthy())
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      findPort: async () => 43123,
      mintToken: () => 'f'.repeat(43),
      spawnRuntime: (_executable, _args, _options) => child,
      fetch
    })

    const starting = supervisor.start()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(fetch).not.toHaveBeenCalled()
    child.ready(43123)
    await starting
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('does not send the bearer credential when the child exits before binding', async () => {
    const child = new FakeChild()
    const fetch = vi.fn(async () => healthy())
    const supervisor = new AgentRuntimeSupervisor({
      agentRoot: 'C:\\trusted\\agent',
      pythonPath: 'C:\\trusted\\agent\\.venv\\Scripts\\python.exe',
      pathExists: () => true,
      maximumRestarts: 0,
      findPort: async () => 43123,
      mintToken: () => 'g'.repeat(43),
      spawnRuntime: (_executable, _args, _options) => child,
      fetch
    })

    const starting = supervisor.start()
    await new Promise((resolve) => setTimeout(resolve, 0))
    child.exit(1)
    await expect(starting).rejects.toThrow('exited before binding')
    expect(fetch).not.toHaveBeenCalled()
    expect(supervisor.status()).toEqual({ state: 'failed' })
  })

  it('reports whether the runtime files exist at all, without starting anything', () => {
    // Milestone 8a S2: the capture guard needs "there is no runtime here" as a
    // distinct answer from "the runtime is not responding".
    const spawnRuntime = vi.fn()
    const present = new AgentRuntimeSupervisor({
      agentRoot: 'C:\trusted\services\agent',
      pythonPath: 'C:\trusted\services\agent\.venv\Scripts\python.exe',
      pathExists: () => true,
      spawnRuntime: spawnRuntime as never
    })
    const missing = new AgentRuntimeSupervisor({
      agentRoot: 'C:\trusted\services\agent',
      pythonPath: 'C:\trusted\services\agent\.venv\Scripts\python.exe',
      pathExists: (path) => !path.endsWith('python.exe'),
      spawnRuntime: spawnRuntime as never
    })

    expect(present.installed()).toBe(true)
    expect(missing.installed()).toBe(false)
    expect(spawnRuntime).not.toHaveBeenCalled()
  })

})
