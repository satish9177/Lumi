#!/usr/bin/env node
/**
 * Assembles the self-contained Python agent runtime that the packaged Lumi app
 * ships as an electron-builder extra resource.
 *
 *   dist/agent-runtime/
 *     python/          relocatable CPython 3.12 (uv-managed standalone build)
 *                      with the locked, production-only dependencies installed
 *     agent/           app/, alembic/, alembic.ini and the demo clinic site
 *     ms-playwright/   Chromium for the isolated browser worker
 *     manifest.json    versions and a content digest, for support and checks
 *
 * What is deliberately excluded: `.env` files, tests, caches and anything
 * else from the developer checkout. Credentials are never bundled; the
 * packaged app reads its database URL from the user's own configuration.
 *
 * Usage: node scripts/build-agent-runtime.mjs [--skip-browsers]
 */

import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const AGENT = join(ROOT, 'services', 'agent')
const OUT = join(ROOT, 'dist', 'agent-runtime')
const skipBrowsers = process.argv.includes('--skip-browsers')

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: ['ignore', 'pipe', 'inherit'], encoding: 'utf8', ...options })
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with ${result.status}`)
  }
  return result.stdout.trim()
}

function step(message) {
  console.log(`[agent-runtime] ${message}`)
}

const EXCLUDED = new Set(['__pycache__', '.pytest_cache', '.mypy_cache', 'tests', '.venv'])

function copyTree(from, to) {
  cpSync(from, to, {
    recursive: true,
    dereference: true,
    filter: (source) => {
      const name = source.split(/[\\/]/).pop() ?? ''
      if (EXCLUDED.has(name)) return false
      if (name === '.env' || name.startsWith('.env.')) return false
      return !name.endsWith('.pyc')
    }
  })
}

/** Bytes on disk under a directory. Reported so a size claim is measured. */
function measureTree(directory) {
  if (!existsSync(directory)) return 0
  let total = 0
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    total += entry.isDirectory() ? measureTree(path) : statSync(path).size
  }
  return total
}

const megabytes = (bytes) => Math.round((bytes / 1_048_576) * 10) / 10

function digestTree(directory) {
  const hash = createHash('sha256')
  const walk = (current) => {
    for (const name of readdirSync(current).sort()) {
      const path = join(current, name)
      if (statSync(path).isDirectory()) walk(path)
      else hash.update(relative(directory, path).replaceAll('\\', '/')).update(readFileSync(path))
    }
  }
  walk(directory)
  return hash.digest('hex')
}

// 1. A relocatable interpreter: the standalone CPython uv manages.
step('locating the uv-managed CPython 3.12')
const venvConfig = readFileSync(join(AGENT, '.venv', 'pyvenv.cfg'), 'utf8')
const home = /^home\s*=\s*(.+)$/m.exec(venvConfig)?.[1]?.trim()
if (!home || !existsSync(join(home, 'python.exe'))) {
  throw new Error('services/agent/.venv is not based on a uv-managed CPython. Run `uv sync` first.')
}
rmSync(OUT, { recursive: true, force: true })
mkdirSync(OUT, { recursive: true })
step(`copying ${home}`)
copyTree(home, join(OUT, 'python'))
const python = join(OUT, 'python', 'python.exe')
// A standalone build may carry an EXTERNALLY-MANAGED marker for uv; this copy
// is Lumi's private interpreter, so dependencies go into it directly.
rmSync(join(OUT, 'python', 'Lib', 'EXTERNALLY-MANAGED'), { force: true })

// 2. Exactly the locked production dependencies.
step('installing locked production dependencies')
const requirements = join(OUT, 'requirements.lock.txt')
run('uv', ['export', '--frozen', '--no-dev', '--no-hashes', '--no-emit-project', '-o', requirements], { cwd: AGENT })
run('uv', ['pip', 'install', '--python', python, '--no-cache', '--break-system-packages', '-r', requirements], { cwd: AGENT })

// 3. The application code, and the demo clinic site used by demo mode.
step('copying the runtime application')
const agentOut = join(OUT, 'agent')
mkdirSync(agentOut, { recursive: true })
copyTree(join(AGENT, 'app'), join(agentOut, 'app'))
copyTree(join(AGENT, 'alembic'), join(agentOut, 'alembic'))
cpSync(join(AGENT, 'alembic.ini'), join(agentOut, 'alembic.ini'))
mkdirSync(join(agentOut, 'evals'), { recursive: true })
cpSync(join(AGENT, 'evals', '__init__.py'), join(agentOut, 'evals', '__init__.py'))
copyTree(join(AGENT, 'evals', 'sites'), join(agentOut, 'evals', 'sites'))

// 4. Chromium for the isolated worker, inside the bundle.
//
// Milestone 8a S1 changed *which* browser this is. Until S0 the bundle carried
// `chromium-headless-shell` only, because the worker ran headless and nothing
// needed a window. A manual sign-in (S2) does need one, so the bundle now
// carries **full Chromium and not the shell** — one binary, one revision, one
// version matrix, used headless for M7a/M7b and headed later for sign-in.
//
// `--no-shell` is what makes that "instead of" rather than "as well as":
// `playwright install chromium` would install the shell too, and shipping both
// would mean ~270 MB of a binary nothing launches. The launch side matches:
// `managed_launch_options` passes `channel: "chromium"`, without which
// `launch(headless=True)` looks for `chromium_headless_shell-<revision>` and
// fails in a packaged build that does not have it.
const BROWSER_SPEC = ['--no-shell', 'chromium']
let browserSummary = 'not bundled'
if (!skipBrowsers) {
  step('bundling full Chromium for the browser worker')
  const target = join(OUT, 'ms-playwright')
  const dryRun = run(python, ['-m', 'playwright', 'install', '--dry-run', ...BROWSER_SPEC], {
    cwd: agentOut, env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: '' }
  })
  const locations = [...dryRun.matchAll(/Install location:\s+(.+)/g)].map((match) => match[1].trim())
  if (locations.length === 0) throw new Error('Could not determine the Chromium revision to bundle.')
  // Refuse to ship the headless shell even if a future Playwright starts
  // listing it under `--no-shell`: two browsers is a version matrix nobody
  // verified, and the packaged worker would have no way to say which it used.
  const shell = locations.find((location) => /headless_shell/i.test(location))
  if (shell) throw new Error(`The headless shell must not be bundled: ${shell}`)
  const missing = locations.filter((location) => !existsSync(location))
  if (missing.length === 0) {
    for (const location of locations) copyTree(location, join(target, location.split(/[\\/]/).pop()))
  } else {
    step(`downloading ${missing.length} missing browser component(s)`)
    run(python, ['-m', 'playwright', 'install', ...BROWSER_SPEC], {
      cwd: agentOut, env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: target }
    })
  }
  // The packaged worker must find its browser from the bundle alone, with no
  // developer cache to fall back on. A revision directory that is not there is
  // a build failure, not a run-time surprise on somebody else's machine.
  const bundled = readdirSync(target, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()
  const chromium = bundled.find((name) => /^chromium-\d+$/.test(name))
  if (!chromium) throw new Error(`No full Chromium revision in the bundle: ${bundled.join(', ')}`)
  if (bundled.some((name) => /headless_shell/i.test(name))) {
    throw new Error('The headless shell reached the bundle.')
  }
  const executable = join(target, chromium, 'chrome-win64', 'chrome.exe')
  if (!existsSync(executable)) throw new Error(`Bundled Chromium has no chrome.exe: ${executable}`)
  browserSummary = { kind: 'chromium', revision: chromium, components: bundled }
  // And it actually launches from the bundle, through the same channel the
  // worker uses, with nothing but the bundle on PLAYWRIGHT_BROWSERS_PATH.
  step('verifying the bundled Chromium launches headless from the bundle alone')
  const launched = run(python, ['-c', [
    'import asyncio',
    'from playwright.async_api import async_playwright',
    'async def main():',
    '    async with async_playwright() as p:',
    '        b = await p.chromium.launch(channel="chromium", headless=True)',
    '        print(b.version)',
    '        await b.close()',
    'asyncio.run(main())'
  ].join('\n')], {
    cwd: agentOut,
    env: {
      SystemRoot: process.env.SystemRoot ?? 'C:\\Windows',
      TEMP: process.env.TEMP ?? 'C:\\Windows\\Temp',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONUTF8: '1',
      PLAYWRIGHT_BROWSERS_PATH: target
    }
  })
  browserSummary.version = launched.split('\n').pop().trim()
  step(`bundled Chromium ${browserSummary.version} (${chromium})`)

  // Milestone 8a S2: manual login needs a visible window, so the bundle's
  // Chromium must also launch headed, not only headless -- from the bundle
  // alone, same channel, same scrubbed environment as the check above. This
  // is the visible mode that motivated shipping full Chromium instead of the
  // headless shell in S1; S1 only proved the headless path.
  step('verifying the bundled Chromium also launches headed from the bundle alone')
  const launchedHeaded = run(python, ['-c', [
    'import asyncio',
    'from playwright.async_api import async_playwright',
    'async def main():',
    '    async with async_playwright() as p:',
    '        b = await p.chromium.launch(channel="chromium", headless=False)',
    '        page = await b.new_page()',
    '        await page.goto("about:blank")',
    '        print(b.version)',
    '        await b.close()',
    'asyncio.run(main())'
  ].join('\n')], {
    cwd: agentOut,
    env: {
      SystemRoot: process.env.SystemRoot ?? 'C:\\Windows',
      TEMP: process.env.TEMP ?? 'C:\\Windows\\Temp',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONUTF8: '1',
      PLAYWRIGHT_BROWSERS_PATH: target
    }
  })
  browserSummary.headedVerified = launchedHeaded.split('\n').pop().trim() === browserSummary.version
  step(`bundled Chromium headed launch verified (${launchedHeaded.split('\n').pop().trim()})`)
}

// 4b. A browser profile directory must never reach an artifact.
//
// Impossible by construction — profiles live under %LOCALAPPDATA% — but a
// future change that relocated the base "just for a test" would otherwise be
// discovered by a user finding their session cookies inside an installer.
step('checking that no browser profile directory is in the bundle')
const profileDirectories = []
const findProfiles = (directory) => {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    if (entry.name === 'browser-profiles') profileDirectories.push(join(directory, entry.name))
    else findProfiles(join(directory, entry.name))
  }
}
findProfiles(OUT)
if (profileDirectories.length > 0) {
  throw new Error(`A browser-profiles directory reached the runtime bundle: ${profileDirectories.join(', ')}`)
}

// 4c. Milestone 9 S1: the Windows UI Automation backend (pywinauto + comtypes + pywin32).
//
// Verified from the bundle alone, before byte-compiling, because importing the backend makes
// comtypes generate its UIAutomationClient wrappers into the bundled `comtypes/gen`, and those
// files should exist (and be compiled) inside the artifact rather than being written beside an
// installed copy. Native extensions are imported explicitly: Windows Application Control can block
// an unsigned `.pyd` that a plain import would only touch later. Nothing here opens a window or
// reads another application: it constructs the backend and reads this process's own integrity level.
step('verifying the bundled Windows UI Automation backend imports from the bundle alone')
const desktopEnv = { SystemRoot: process.env.SystemRoot ?? 'C:\\Windows', PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' }
const desktop = JSON.parse(run(python, ['-c', [
  'import importlib.metadata as m, json',
  // The backend goes first: it selects COM's multithreaded apartment before anything initialises COM
  // on this thread (importing pythoncom first would make it a single-threaded apartment and the
  // backend's own initialisation would then fail). The pywin32 natives follow, explicitly.
  'from app.desktop.uia_backend import PywinautoBackend',
  'from app.desktop.win32 import WindowsSystemProbe',
  'import _ctypes, comtypes.gen',
  'import pythoncom, pywintypes, win32api, win32gui, win32process, win32event',
  'PywinautoBackend()',
  'integrity = WindowsSystemProbe().own_integrity_level()',
  'assert integrity is not None',
  'print(json.dumps({',
  '    "backend": "pywinauto-uia",',
  '    "pywinauto": m.version("pywinauto"), "comtypes": m.version("comtypes"), "pywin32": m.version("pywin32"),',
  '    "ownIntegrityLevel": integrity}))'
].join('\n')], { cwd: agentOut, env: desktopEnv }))
const generated = readdirSync(join(OUT, 'python', 'Lib', 'site-packages', 'comtypes', 'gen')).filter((name) => name.endsWith('.py'))
if (!generated.includes('UIAutomationClient.py')) {
  throw new Error('The comtypes UI Automation wrappers were not generated into the bundle.')
}
desktop.generatedWrappers = generated.filter((name) => name !== '__init__.py')

// The desktop package ships as source in the existing Python. It must not bring a helper executable
// or a native binary of Lumi's own (those would need signing and are exactly what the release
// gate's suffix policy does not expect), and no test fixture may be bundled.
const listNames = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
  entry.isDirectory() ? [entry.name, ...listNames(join(directory, entry.name))] : [entry.name])
const bundledNames = listNames(agentOut)
const ownBinaries = bundledNames.filter((name) => /\.(exe|dll|pyd|sys|msi|scr)$/i.test(name))
if (ownBinaries.length > 0) {
  throw new Error(`A Lumi-owned native binary reached the runtime bundle: ${ownBinaries.join(', ')}`)
}
if (bundledNames.some((name) => /desktop_fixture_app|desktop_harness|desktop_fakes|^tests?$/.test(name))) {
  throw new Error('The desktop test fixture reached the runtime bundle.')
}
if (!existsSync(join(agentOut, 'app', 'desktop', 'worker.py'))) {
  throw new Error('The desktop worker is missing from the runtime bundle.')
}
if (!readdirSync(join(agentOut, 'alembic', 'versions')).some((name) => name.startsWith('0012_'))) {
  throw new Error('Migration 0012 is missing from the runtime bundle.')
}
// Milestone 9 S2: the exact desktop disclosure tables and the widened grant kind.
if (!readdirSync(join(agentOut, 'alembic', 'versions')).some((name) => name.startsWith('0013_'))) {
  throw new Error('Migration 0013 is missing from the runtime bundle.')
}
if (!existsSync(join(agentOut, 'app', 'services', 'desktop_disclosure.py'))) {
  throw new Error('The desktop disclosure service is missing from the runtime bundle.')
}
// Milestone 9 S3: trusted focus, semantic scroll and registered-app launch, and their durable dispatch table.
if (!readdirSync(join(agentOut, 'alembic', 'versions')).some((name) => name.startsWith('0014_'))) {
  throw new Error('Migration 0014 is missing from the runtime bundle.')
}
for (const file of [
  ['app', 'desktop', 'effects.py'], ['app', 'desktop', 'effects_win32.py'], ['app', 'desktop', 'registry.py'],
  ['app', 'services', 'desktop_actions.py'], ['app', 'domain', 'desktop_actions.py']
]) {
  if (!existsSync(join(agentOut, ...file))) throw new Error(`${file.join('/')} is missing from the runtime bundle.`)
}
// Milestone 9 S4: bounded set-value/select/invoke, their widened dispatch table, and the parallel
// planning-disclosure path (disclosure authority only -- never execution authority on its own).
if (!readdirSync(join(agentOut, 'alembic', 'versions')).some((name) => name.startsWith('0015_'))) {
  throw new Error('Migration 0015 is missing from the runtime bundle.')
}
for (const file of [
  ['app', 'services', 'desktop_planning.py'], ['app', 'domain', 'desktop_planning.py'],
  ['app', 'repositories', 'desktop_planning.py'], ['app', 'api', 'desktop_planning_schemas.py']
]) {
  if (!existsSync(join(agentOut, ...file))) throw new Error(`${file.join('/')} is missing from the runtime bundle.`)
}
// Milestone 9 S5: the scoped visual fallback (capture + a separate vision-disclosure approval). No new
// native dependency: `capture_win32.py`/`dpi.py`/`png_encode.py` are ctypes/stdlib only.
if (!readdirSync(join(agentOut, 'alembic', 'versions')).some((name) => name.startsWith('0016_'))) {
  throw new Error('Migration 0016 is missing from the runtime bundle.')
}
for (const file of [
  ['app', 'desktop', 'capture_win32.py'], ['app', 'desktop', 'dpi.py'], ['app', 'desktop', 'png_encode.py'],
  ['app', 'services', 'desktop_vision.py'], ['app', 'domain', 'desktop_vision.py'],
  ['app', 'repositories', 'desktop_vision.py'], ['app', 'api', 'desktop_vision_schemas.py']
]) {
  if (!existsSync(join(agentOut, ...file))) throw new Error(`${file.join('/')} is missing from the runtime bundle.`)
}

// 5. Byte-compile once, so an installed copy never needs to write beside itself.
step('byte-compiling')
run(python, ['-m', 'compileall', '-q', '-j', '0', join(agentOut, 'app'), join(agentOut, 'alembic'), join(agentOut, 'evals'), join(OUT, 'python', 'Lib')], {})

// 6. Smoke test from the bundle alone, with no developer environment.
step('verifying the bundle imports and has no secrets')
const bare = { SystemRoot: process.env.SystemRoot ?? 'C:\\Windows', PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' }
// Native extensions are imported explicitly: Windows Application Control can
// block an unsigned .pyd that a plain module import would only touch later.
run(python, ['-c', [
  'import app.main, app.server, app.migrate, app.browser.main, evals.sites.appointments.server',
  'import greenlet._greenlet, asyncpg.protocol.protocol, pydantic_core._pydantic_core',
  'from sqlalchemy.util import greenlet_spawn',
  'print("ok")'
].join('; ')], { cwd: agentOut, env: bare })
const walkNames = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
  entry.isDirectory() ? walkNames(join(directory, entry.name)) : [entry.name])
if (walkNames(agentOut).some((name) => name === '.env' || name.startsWith('.env.'))) {
  throw new Error('A .env file reached the runtime bundle.')
}

// Enough to answer a support or version question from the bundle alone: which
// browser, which revision, which Playwright, and which Public Suffix List
// decided the site boundaries a profile was bound with. Deliberately **no**
// profile id and no path: the manifest ships inside the installer, and a
// profile is neither built nor bundled.
const publicSuffix = JSON.parse(run(python, ['-c', [
  'import json',
  'from app.domain.public_suffix import (',
  '    PUBLIC_SUFFIX_COMMIT, PUBLIC_SUFFIX_DIGEST, PUBLIC_SUFFIX_SOURCE, PUBLIC_SUFFIX_VERSION,',
  '    public_suffix_list)',
  'psl = public_suffix_list()',
  'print(json.dumps({',
  '    "version": PUBLIC_SUFFIX_VERSION, "commit": PUBLIC_SUFFIX_COMMIT,',
  '    "digest": PUBLIC_SUFFIX_DIGEST, "source": PUBLIC_SUFFIX_SOURCE,',
  '    "rules": len(psl.rules) + len(psl.wildcards) + len(psl.exceptions)}))'
].join('\n')], { cwd: agentOut, env: bare }))

const manifest = {
  version: 3,
  builtAt: new Date().toISOString(),
  python: run(python, ['-c', 'import sys; print(sys.version.split()[0])'], { env: bare }),
  playwright: run(python, ['-c', 'import importlib.metadata as m; print(m.version("playwright"))'], { env: bare }),
  browsers: skipBrowsers ? 'not bundled' : 'chromium',
  browser: browserSummary,
  publicSuffixList: publicSuffix,
  desktop,
  // Measured, never estimated: the S1 report quotes these numbers.
  sizeMegabytes: {
    total: megabytes(measureTree(OUT)),
    python: megabytes(measureTree(join(OUT, 'python'))),
    agent: megabytes(measureTree(agentOut)),
    browser: megabytes(measureTree(join(OUT, 'ms-playwright')))
  },
  lockDigest: createHash('sha256').update(readFileSync(join(AGENT, 'uv.lock'))).digest('hex'),
  agentDigest: digestTree(agentOut)
}
writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)
step(`done: ${JSON.stringify(manifest)}`)
