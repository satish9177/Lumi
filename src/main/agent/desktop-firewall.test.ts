import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Milestone 9, slice 1: Windows desktop observation is local and private.
 *
 * The runtime can read a Windows application semantically, and nothing in Electron
 * consumes it. These tests prove that structurally: no code in main, preload, the
 * renderer or the shared contracts can call the desktop routes, name a desktop
 * observation, or carry the planted marker, so no planner payload, answer, voice
 * context, memory record or task summary can contain desktop text. A later slice that
 * defines an explicit disclosure scope must change these tests on purpose.
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

const files = SOURCE_DIRECTORIES.flatMap((directory) => walk(join(ROOT, directory)))
  .filter((path) => /\.(ts|tsx|json|mjs|js)$/.test(path))
  .filter((path) => relative(ROOT, path) !== SELF)

const FORBIDDEN = [
  '/desktop/surfaces',
  '/desktop/observations',
  'DesktopObservation',
  'desktop_observations',
  'desktop_private',
  'M9_S1_',
  'listDesktopSurfaces',
  'observeDesktopSurface',
  'LUMI_DESKTOP',
  'surface_epoch'
]

describe('desktop observation firewall', () => {
  it('scans real source files', () => {
    expect(files.length).toBeGreaterThan(50)
    expect(files.some((path) => path.endsWith(`${sep}agent-wire.ts`))).toBe(true)
  })

  it.each(FORBIDDEN)('nothing in Electron or the shared contracts mentions %s', (token) => {
    const offenders = files.filter((path) => readFileSync(path, 'utf8').includes(token))
    expect(offenders.map((path) => relative(ROOT, path))).toEqual([])
  })

  it('exposes no desktop function on the renderer bridge', () => {
    const bridge = readFileSync(join(ROOT, 'src', 'preload', 'index.ts'), 'utf8')
    expect(bridge).not.toMatch(/desktop(Surfaces?|Observation|Automation)/i)
    expect(bridge).not.toMatch(/observeDesktop|listDesktop|focusWindow|invokeControl/i)
  })

  it('gives the model context builder and memory no path to a desktop record', () => {
    for (const name of ['src/main/models/context-builder.ts', 'src/main/agent/agent-memory.ts', 'src/main/agent/research-planner.ts', 'src/main/agent/authenticated-planner.ts']) {
      const source = readFileSync(join(ROOT, name), 'utf8')
      expect(source).not.toMatch(/desktop[_ ]?(observation|surface|uia)/i)
    }
  })

  it('may mention the one runtime error code, and only in the generated contract', () => {
    const mentions = files.filter((path) => readFileSync(path, 'utf8').includes('desktop_refused')).map((path) => relative(ROOT, path).split(sep).join('/'))
    expect(mentions).toEqual(['src/shared/agent-runtime-contract.json'])
  })
})
