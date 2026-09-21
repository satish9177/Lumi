import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS } from '../../shared/agent-contracts'
import { isAllowedRuntimeRoute } from '../services/agent-runtime-supervisor'

/**
 * Milestone 9, slice 2: the desktop firewall, rewritten on purpose.
 *
 * S1 proved that NOTHING in Electron could read a desktop observation. S2 deliberately changes that
 * invariant, so this file does not delete the S1 proof; it narrows it to a structural fact that must
 * stay true:
 *
 *   without a confirmed, exact desktop disclosure, zero provider paths can read an observation; with
 *   one, exactly the reviewed S2 path (runtime claim -> `DesktopReader` -> one provider attempt) can.
 *
 * "Reviewed" is a list of files below. A new file that names a desktop route, a desktop record or the
 * reader fails here until somebody reads it and adds it, on purpose.
 */

const ROOT = join(__dirname, '..', '..', '..')
const SELF = join('src', 'main', 'agent', 'desktop-firewall.test.ts')
const SOURCE_DIRECTORIES = ['src/main', 'src/preload', 'src/renderer', 'src/shared', 'src/features']

function walk(directory: string): string[] {
  let entries: string[]
  try {
    entries = readdirSync(directory)
  } catch {
    return []
  }
  return entries.flatMap((entry) => {
    const path = join(directory, entry)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const rel = (path: string): string => relative(ROOT, path).split(sep).join('/')
const isTest = (path: string): boolean => /\.test\.(ts|tsx)$/.test(path)

const files = SOURCE_DIRECTORIES.flatMap((directory) => walk(join(ROOT, directory)))
  .filter((path) => /\.(ts|tsx|json|mjs|js)$/.test(path))
  .filter((path) => relative(ROOT, path) !== SELF)
/** Production code only: what could ever run. Tests may name anything. */
const production = files.filter((path) => !isTest(path))
const source = (path: string): string => readFileSync(path, 'utf8')
const mentioning = (token: string | RegExp, among = production): string[] =>
  among.filter((path) => typeof token === 'string' ? source(path).includes(token) : token.test(source(path))).map(rel).sort()

describe('the desktop firewall', () => {
  it('scans real source files', () => {
    expect(files.length).toBeGreaterThan(50)
    expect(files.some((path) => path.endsWith(`${sep}agent-wire.ts`))).toBe(true)
  })

  it('never names a raw observation, its table or the S1 planted marker in Electron', () => {
    // Production code only: a test may plant the S2 marker to prove it does not leak (that is its job).
    for (const token of ['DesktopObservation', 'desktop_observations', 'M9_S1_', 'M9_S2_', 'snapshot_digest']) {
      expect(mentioning(token), token).toEqual([])
    }
  })

  it('confines the runtime wire words for a desktop record to the one reviewed parser and client', () => {
    expect(mentioning('surface_epoch')).toEqual([
      'src/main/services/desktop-read-controller.ts',
      'src/main/services/desktop-read-wire.ts'
    ])
    expect(mentioning('desktop_private')).toEqual(['src/main/services/desktop-read-wire.ts'])
    expect(mentioning('/desktop/read-tasks')).toEqual([
      'src/main/services/agent-runtime-supervisor.ts',
      'src/main/services/desktop-read-controller.ts'
    ])
    expect(mentioning('/desktop/surfaces')).toEqual(['src/main/services/desktop-read-controller.ts'])
  })

  it('has exactly one reviewed path from a claimed disclosure to a provider', () => {
    expect(mentioning('DesktopReader')).toEqual([
      'src/main/agent/desktop-reader.ts',
      'src/main/index.ts',
      'src/main/services/desktop-read-controller.ts'
    ])
    expect(mentioning('desktop_planning')).toEqual([
      'src/main/agent/desktop-reader.ts',
      'src/main/models/model-router.ts',
      'src/main/models/scripted-provider.ts',
      'src/shared/model-contracts.ts'
    ])
    // Only the controller calls `.read(`, and only after the runtime claim (proved in its own test).
    const callers = production.filter((path) => /\breader\.read\(/.test(source(path))).map(rel)
    expect(callers).toEqual(['src/main/services/desktop-read-controller.ts'])
  })

  it('gives the model context builder, memory, planners and voice no path to a desktop record', () => {
    const untouched = [
      'src/main/models/context-builder.ts',
      'src/main/agent/agent-memory.ts',
      'src/main/agent/research-planner.ts',
      'src/main/agent/research-answer.ts',
      'src/main/agent/authenticated-planner.ts',
      'src/main/agent/authenticated-answer.ts',
      'src/main/agent/form-planner.ts',
      'src/main/agent/page-answer.ts',
      'src/main/agent/task-request-interpreter.ts',
      'src/main/services/voice-task-controller.ts',
      'src/main/services/agent-tasks.ts',
      'src/shared/voice-task-contracts.ts',
      'src/renderer/src/voice-task-tools.ts'
    ]
    for (const name of untouched) {
      expect(source(join(ROOT, name)), name).not.toMatch(/desktop[_ ]?(observation|surface|uia|read|disclos|snapshot)|DesktopRead|desktop_planning/i)
    }
  })

  it('keeps the desktop controller out of voice and the conversation', () => {
    const controller = source(join(ROOT, 'src', 'main', 'services', 'desktop-read-controller.ts'))
    // It must not import the interpreter, memory, the conversation window or any voice module.
    expect(controller).not.toMatch(/from '.*(task-request-interpreter|agent-memory|voice|context-builder|conversation)/)
    const voice = source(join(ROOT, 'src', 'main', 'services', 'voice-task-controller.ts'))
    expect(voice).not.toMatch(/DesktopReadController|desktop-read/)
  })

  it('exposes exactly six typed desktop methods on the renderer bridge and no desktop verb', () => {
    const bridge = source(join(ROOT, 'src', 'preload', 'index.ts'))
    const methods = [...bridge.matchAll(/^\s{2}(\w*Desktop\w*):/gm)].map((match) => match[1]).sort()
    expect(methods).toEqual([
      'createDesktopRead', 'declineDesktopDisclosure', 'getDesktopRead',
      'grantDesktopDisclosure', 'listDesktopSurfaces', 'runDesktopRead'
    ])
    expect(bridge).not.toMatch(/focus(Desktop|Window|Surface)|invoke(Desktop|Control)|setDesktopValue|scrollDesktop|clickDesktop|typeDesktop|selectDesktop|launch(Desktop|App)/i)
    // No generic pass-through: nothing takes a method name, a route, a channel or a command object.
    expect(bridge).not.toMatch(/desktop\s*\(\s*(command|method|tool|route|channel)/i)
    expect(bridge).not.toMatch(/(executeDesktop|computer)\s*[:(]/)
  })

  it('registers exactly six desktop channels, none of which can act', () => {
    const channels = Object.keys(AGENT_IPC_CHANNELS).filter((name) => /desktop/i.test(name)).sort()
    expect(channels).toEqual([
      'createDesktopRead', 'declineDesktopDisclosure', 'getDesktopRead',
      'grantDesktopDisclosure', 'listDesktopSurfaces', 'runDesktopRead'
    ])
    for (const name of channels) expect(name).not.toMatch(/focus|invoke|type|select|scroll|click|launch|execute/i)
  })

  it('lets main reach only the five desktop routes, never a raw observation or a verb', () => {
    const id = '11111111-2222-4333-8444-555555555555'
    const allowed: Array<['GET' | 'POST', string]> = [
      ['GET', '/desktop/surfaces'],
      ['POST', '/desktop/read-tasks'],
      ['GET', '/desktop/read-tasks/latest'],
      ['GET', `/desktop/read-tasks/${id}`],
      ['POST', `/desktop/read-tasks/${id}/grant`],
      ['POST', `/desktop/read-tasks/${id}/revoke`],
      ['POST', `/desktop/read-tasks/${id}/disclosure`],
      ['POST', `/desktop/read-tasks/${id}/result`]
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    const rejected: Array<['GET' | 'POST', string]> = [
      ['POST', '/desktop/observations'],
      ['GET', '/desktop/observations'],
      ['POST', '/desktop/surfaces'],
      ['GET', `/desktop/read-tasks/${id}/disclosure`],
      ['POST', `/desktop/read-tasks/${id}/focus`],
      ['POST', `/desktop/read-tasks/${id}/invoke`],
      ['POST', `/desktop/read-tasks/${id}/click`],
      ['POST', '/desktop/focus'],
      ['POST', '/desktop/invoke'],
      ['POST', '/desktop/execute'],
      ['POST', '/desktop/read-tasks/latest']
    ]
    for (const [method, path] of rejected) expect(isAllowedRuntimeRoute(method, path), `${method} ${path}`).toBe(false)
  })

  it('names the desktop error codes only in the generated contract and the reviewed wire projection', () => {
    const contract = production.filter((path) => source(path).includes('desktop_refused')).map(rel).sort()
    expect(contract).toEqual([
      'src/main/services/desktop-read-wire.ts',
      'src/shared/agent-contracts.ts',
      'src/shared/agent-runtime-contract.json'
    ])
  })

  it('has no main or renderer path that opens a capture, image or vision call for a desktop read', () => {
    for (const name of ['src/main/agent/desktop-reader.ts', 'src/main/services/desktop-read-controller.ts', 'src/main/services/desktop-read-wire.ts', 'src/renderer/src/components/DesktopReadPanel.tsx']) {
      const text = source(join(ROOT, name))
      expect(text, name).not.toMatch(/desktopCapturer|capture\.ts|screenReasoning|screen-reasoning|\bimage\s*:|thumbnail|getUserMedia|\bocr\b/i)
    }
  })
})
