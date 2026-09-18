import { describe, expect, it } from 'vitest'
import { readFileSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

/**
 * What the packaged runtime must contain, and what it must never contain.
 *
 * Milestone 8a S1 changed the bundled browser: full Chromium instead of
 * `chromium-headless-shell`, because manual sign-in (S2) needs a visible
 * window and shipping both would be a version matrix nobody verified. Two
 * things have to stay true for that to work, and they live in two different
 * files, so this is where they are checked against each other:
 *
 *  - the build installs with `--no-shell` and refuses a shell in the bundle;
 *  - `managed_launch_options` passes `channel: "chromium"`, without which
 *    `headless=True` looks for the shell and fails in a packaged build.
 *
 * A full `npm run package` takes many minutes and downloads hundreds of
 * megabytes, so these are assertions about the build *contract*. The build
 * itself enforces the same things at run time and fails loudly; the measured
 * output of a real packaging run is recorded in
 * `docs/reviews/milestone-8-s1.md`. When a build has been run locally, the
 * manifest assertions below check its actual output too.
 */

const ROOT = resolve(__dirname, '..', '..', '..')
const BUILD_SCRIPT = readFileSync(join(ROOT, 'scripts', 'build-agent-runtime.mjs'), 'utf8')
const BROKER = readFileSync(
  join(ROOT, 'services', 'agent', 'app', 'browser', 'egress_broker.py'),
  'utf8'
)
const MANIFEST_PATH = join(ROOT, 'dist', 'agent-runtime', 'manifest.json')

describe('the bundled browser', () => {
  it('installs full Chromium and not the headless shell', () => {
    expect(BUILD_SCRIPT).toContain("BROWSER_SPEC = ['--no-shell', 'chromium']")
    expect(BUILD_SCRIPT).not.toContain("'chromium-headless-shell'")
  })

  it('fails the build if a headless shell reaches the bundle', () => {
    // Two checks, because Playwright's own listing and the copied output are
    // different opportunities for one to appear.
    expect(BUILD_SCRIPT).toContain('The headless shell must not be bundled')
    expect(BUILD_SCRIPT).toContain('The headless shell reached the bundle.')
  })

  it('requires a full Chromium revision with a real chrome.exe', () => {
    expect(BUILD_SCRIPT).toContain('/^chromium-\\d+$/')
    expect(BUILD_SCRIPT).toContain("'chrome-win64', 'chrome.exe'")
    expect(BUILD_SCRIPT).toContain('No full Chromium revision in the bundle')
  })

  it('launches the bundled browser from the bundle alone before finishing', () => {
    // With only the bundle on PLAYWRIGHT_BROWSERS_PATH, so a developer cache
    // cannot stand in for a component the installer will not carry.
    expect(BUILD_SCRIPT).toContain('PLAYWRIGHT_BROWSERS_PATH: target')
    expect(BUILD_SCRIPT).toContain('channel="chromium"')
  })

  it('is launched through the same channel the build verifies', () => {
    expect(BROKER).toContain('MANAGED_CHROMIUM_CHANNEL = "chromium"')
    expect(BROKER).toContain('"channel": MANAGED_CHROMIUM_CHANNEL')
  })
})

describe('browser profile directories', () => {
  it('cannot enter the runtime bundle', () => {
    expect(BUILD_SCRIPT).toContain("entry.name === 'browser-profiles'")
    expect(BUILD_SCRIPT).toContain('A browser-profiles directory reached the runtime bundle')
  })

  it('is ignored by git, so a relocated dev base cannot be committed', () => {
    const ignore = readFileSync(join(ROOT, '.gitignore'), 'utf8')
    expect(ignore.split(/\r?\n/)).toContain('browser-profiles/')
  })
})

describe('the runtime manifest', () => {
  it('records the browser and Public Suffix List identity', () => {
    expect(BUILD_SCRIPT).toContain('browser: browserSummary')
    expect(BUILD_SCRIPT).toContain('publicSuffixList: publicSuffix')
    expect(BUILD_SCRIPT).toContain('sizeMegabytes')
  })

  it('records no profile id and no profile path', () => {
    const manifestBlock = BUILD_SCRIPT.slice(
      BUILD_SCRIPT.indexOf('const manifest = {'),
      BUILD_SCRIPT.indexOf('writeFileSync(join(OUT,')
    )
    for (const forbidden of ['profileId', 'profilePath', 'browser-profiles', 'LOCALAPPDATA']) {
      expect(manifestBlock).not.toContain(forbidden)
    }
  })

  it.runIf(existsSync(MANIFEST_PATH))('describes a real build when one exists', () => {
    const manifest = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'))
    expect(manifest.version).toBeGreaterThanOrEqual(2)
    expect(manifest.browser.kind).toBe('chromium')
    expect(manifest.browser.revision).toMatch(/^chromium-\d+$/)
    expect(manifest.browser.version).toMatch(/^\d+\.\d+\.\d+\.\d+$/)
    expect(manifest.browser.components.join(' ')).not.toMatch(/headless_shell/i)
    expect(manifest.publicSuffixList.digest).toMatch(/^[0-9a-f]{64}$/)
    expect(manifest.publicSuffixList.version).toMatch(/^\d{4}-\d{2}-\d{2}_/)
    expect(manifest.publicSuffixList.rules).toBeGreaterThan(5_000)
    expect(manifest.sizeMegabytes.browser).toBeGreaterThan(300)
    expect(JSON.stringify(manifest)).not.toMatch(/browser-profiles|LOCALAPPDATA/i)
  })
})
