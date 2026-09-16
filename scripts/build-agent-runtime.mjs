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
if (!skipBrowsers) {
  // The worker runs headless, so the headless shell (plus its Windows helper)
  // is all it needs. Revisions matching this Playwright version are copied
  // from the local cache when present; otherwise they are downloaded.
  step('bundling Chromium for the browser worker')
  const target = join(OUT, 'ms-playwright')
  const dryRun = run(python, ['-m', 'playwright', 'install', '--dry-run', 'chromium-headless-shell'], {
    cwd: agentOut, env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: '' }
  })
  const locations = [...dryRun.matchAll(/Install location:\s+(.+)/g)].map((match) => match[1].trim())
  if (locations.length === 0) throw new Error('Could not determine the Chromium revision to bundle.')
  const missing = locations.filter((location) => !existsSync(location))
  if (missing.length === 0) {
    for (const location of locations) copyTree(location, join(target, location.split(/[\\/]/).pop()))
  } else {
    step(`downloading ${missing.length} missing browser component(s)`)
    run(python, ['-m', 'playwright', 'install', 'chromium-headless-shell'], {
      cwd: agentOut, env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: target }
    })
  }
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

const manifest = {
  version: 1,
  builtAt: new Date().toISOString(),
  python: run(python, ['-c', 'import sys; print(sys.version.split()[0])'], { env: bare }),
  playwright: run(python, ['-c', 'import importlib.metadata as m; print(m.version("playwright"))'], { env: bare }),
  browsers: skipBrowsers ? 'not bundled' : 'chromium',
  lockDigest: createHash('sha256').update(readFileSync(join(AGENT, 'uv.lock'))).digest('hex'),
  agentDigest: digestTree(agentOut)
}
writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)
step(`done: ${JSON.stringify(manifest)}`)
