import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { AGENT_IPC_CHANNELS } from '../../shared/agent-contracts'
import { registerAgentIpc } from './agent-ipc'
import type { AgentTaskController } from './agent-tasks'

const ROOT = join(__dirname, '..', '..')

function sources(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    if (statSync(path).isDirectory()) return sources(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : []
  })
}

describe('renderer and preload trust boundary', () => {
  it('renderer and preload code never address local services or credentials', () => {
    const files = [
      ...sources(join(ROOT, 'renderer')),
      ...sources(join(ROOT, 'preload')),
      join(ROOT, 'shared', 'agent-contracts.ts'),
      join(ROOT, 'shared', 'voice-task-contracts.ts')
    ]
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      const name = relative(ROOT, file)
      for (const forbidden of [/127\.0\.0\.1/, /localhost/i, /LUMI_RUNTIME_TOKEN/, /LUMI_BROWSER_TOKEN/, /DATABASE_URL/, /x-lumi-worker-token/i, /lifecycle\/shutdown/, /\/v1\/dispatch/]) {
        expect(forbidden.test(text), `${name} must not contain ${forbidden}`).toBe(false)
      }
    }
  })

  it('preload exposes only the fixed agent channels with positional arguments', () => {
    const preload = readFileSync(join(ROOT, 'preload', 'index.ts'), 'utf8')
    const used = [...preload.matchAll(/AGENT_IPC_CHANNELS\.(\w+)/g)].map((match) => match[1]).sort()
    expect(new Set(used)).toEqual(new Set(Object.keys(AGENT_IPC_CHANNELS)))
    expect(preload).not.toMatch(/ipcRenderer\.invoke\(\s*(channel|name|method)/)
    const values = Object.values(AGENT_IPC_CHANNELS)
    expect(new Set(values).size).toBe(values.length)
  })

  it('registers exactly the agent channels, checking the sender before any work', async () => {
    const handlers = new Map<string, (event: never, ...args: unknown[]) => unknown>()
    const controller = new Proxy({}, {
      get: (_target, property) => vi.fn(async () => ({ ok: true, value: String(property) }))
    }) as unknown as AgentTaskController
    const assertTrustedSender = vi.fn((event: never) => {
      if ((event as { trusted?: boolean }).trusted !== true) throw new Error('Rejected IPC request from an unexpected frame.')
    })
    const voice = { handle: vi.fn(async () => ({ ok: true, value: 'voiceCommand' })) }
    registerAgentIpc({
      ipcMain: { handle: (channel, listener) => { handlers.set(channel, listener) } },
      assertTrustedSender,
      controller,
      voice: voice as never,
      runtimeStatus: () => ({ state: 'running' }),
      restartRuntime: async () => ({ state: 'running' })
    })
    const channels = Object.entries(AGENT_IPC_CHANNELS).filter(([key]) => key !== 'runtimeStatusChanged').map(([, value]) => value)
    expect([...handlers.keys()].sort()).toEqual([...channels].sort())
    for (const [channel, handler] of handlers) {
      expect(() => handler({ trusted: false } as never, 'x', 1), channel).toThrow('unexpected frame')
    }
    expect(Object.values(controller).length).toBe(0)
    expect(voice.handle).not.toHaveBeenCalled()
    expect(await handlers.get(AGENT_IPC_CHANNELS.approveAction)!({ trusted: true } as never, 'id', 2)).toEqual({ ok: true, value: 'approveAction' })
  })
})
