/**
 * Milestone 8a S2 closure: a live takeover keeps screen capture refused
 * across an Electron-main restart.
 *
 * The gap this covers, stated as the original S2 report found it: both
 * `capture.ts`'s guard and `BrowserProfileController`'s open-attempt set are
 * in-memory, so a fresh main process came back believing no takeover existed
 * while a headed, credential-bearing browser could still be on screen. The
 * fix is that the *runtime* holds the answer -- `active_takeover` on each
 * profile -- and that capture is refused from the first instruction of a new
 * main process until that answer has actually been read.
 *
 * "Reconstructing main state" here means exactly what it means in the
 * product: a brand-new `BrowserProfileController` and a capture guard that
 * has never been told anything, talking to a runtime that never restarted.
 * Nothing in these tests hands the new process an attempt id it was supposed
 * to have remembered, and no renderer poll happens anywhere in them.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CaptureRefusedError,
  captureScreen,
  clearTakeoverReconciliation,
  listCaptureSources,
  setTakeoverActive,
  takeoverGuardState
} from './capture'
import { BrowserProfileController, type RuntimeRequester } from './browser-profile-controller'
import { parseActiveTakeover, parseBrowserProfile } from './browser-profile-wire'
import { TakeoverCaptureGuard } from './takeover-capture-guard'
import { RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'

const PROFILE = '00000000-0000-4000-8000-000000000009'
const ATTEMPT = '00000000-0000-4000-8000-00000000000a'
const OTHER_PROFILE = '00000000-0000-4000-8000-00000000000b'
const OTHER_ATTEMPT = '00000000-0000-4000-8000-00000000000c'

const contract = JSON.parse(
  readFileSync(join(__dirname, '../../shared/agent-runtime-contract.json'), 'utf8')
) as {
  schemas: Record<string, { properties: Record<string, unknown>; additionalProperties?: boolean }>
  examples: Record<string, Record<string, unknown>>
}

function profileBody(activeTakeover: unknown, id = PROFILE): Record<string, unknown> {
  return {
    ...contract.examples.browser_profile_needs_login,
    id,
    active_takeover: activeTakeover
  }
}

function openTakeover(attemptId = ATTEMPT, profileId = PROFILE, status = 'OPEN'): Record<string, unknown> {
  return {
    profile_id: profileId,
    attempt_id: attemptId,
    status,
    expires_at: '2026-09-16T10:15:05Z'
  }
}

/** A runtime that answers `GET /browser-profiles` and nothing else. */
class FakeRuntime implements RuntimeRequester {
  profiles: Array<Record<string, unknown>> = []
  unavailable = false
  calls = 0

  async request(method: RuntimeMethod, path: string, _body: unknown, _timeoutMs: number): Promise<RuntimeReply> {
    this.calls += 1
    if (this.unavailable) throw new RuntimeUnavailableError()
    if (method !== 'GET' || path !== '/browser-profiles') throw new Error(`unexpected ${method} ${path}`)
    return { status: 200, body: { profiles: this.profiles }, generation: 'g1' }
  }
}

/** One fresh main process: nothing remembered, capture refused. */
function restartMain(runtime: RuntimeRequester): { controller: BrowserProfileController; guard: TakeoverCaptureGuard } {
  clearTakeoverReconciliation()
  const controller = new BrowserProfileController(runtime)
  const guard = new TakeoverCaptureGuard({
    reconcile: () => controller.reconcileTakeovers(),
    agentRuntimeAbsent: () => false,
    setTimer: () => undefined,
    clearTimer: () => undefined
  })
  return { controller, guard }
}

const WORKING_CAPTURE_RUNTIME = {
  getPrimaryDisplay: () => ({ id: 1, size: { width: 1_600, height: 900 }, scaleFactor: 1 }),
  getSources: vi.fn(async () => [{
    id: 'screen:1:0',
    display_id: '1',
    name: 'Primary screen',
    thumbnail: {
      toJPEG: () => Buffer.alloc(1_000),
      getSize: () => ({ width: 1_600, height: 900 }),
      resize() { return this },
      isEmpty: () => false
    }
  }])
}

describe('capture after an Electron-main restart during a takeover', () => {
  beforeEach(() => {
    clearTakeoverReconciliation()
    WORKING_CAPTURE_RUNTIME.getSources.mockClear()
  })

  it('refuses capture immediately after main state is reconstructed, before anything is reconciled', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(openTakeover())]
    restartMain(runtime)

    // Step 7 of the restart sequence: capture is attempted before any
    // renderer action and before reconciliation has had a chance to run.
    expect(takeoverGuardState()).toBe('unreconciled')
    await expect(captureScreen(undefined, WORKING_CAPTURE_RUNTIME)).rejects.toBeInstanceOf(CaptureRefusedError)
    await expect(listCaptureSources()).rejects.toBeInstanceOf(CaptureRefusedError)
    expect(WORKING_CAPTURE_RUNTIME.getSources).not.toHaveBeenCalled()
    // Nothing was asked of the runtime yet: the refusal is the starting state,
    // not the result of a lookup that could have failed open.
    expect(runtime.calls).toBe(0)
  })

  it('discovers the still-open attempt from durable runtime state and keeps capture refused', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(openTakeover())]
    const { guard } = restartMain(runtime)

    expect(await guard.reconcileNow()).toBe(true)

    expect(takeoverGuardState()).toBe('active')
    const refusal = await captureScreen(undefined, WORKING_CAPTURE_RUNTIME).catch((error: unknown) => error)
    expect(refusal).toBeInstanceOf(CaptureRefusedError)
    expect((refusal as CaptureRefusedError).code).toBe('capture_refused_takeover_active')
  })

  it('re-enables capture once the runtime reports the attempt settled', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(openTakeover())]
    const { guard } = restartMain(runtime)
    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('active')

    for (const settled of ['COMPLETED', 'CANCELLED', 'EXPIRED', 'INTERRUPTED']) {
      // A settled attempt is reported by the runtime as no active takeover at
      // all; the status is checked too, so either shape releases the guard.
      runtime.profiles = [profileBody(openTakeover(ATTEMPT, PROFILE, settled))]
      await guard.reconcileNow()
      expect(takeoverGuardState()).toBe('clear')
      const capture = await captureScreen(undefined, WORKING_CAPTURE_RUNTIME)
      expect(capture.sourceId).toBe('screen:1:0')
      setTakeoverActive(true)
    }

    runtime.profiles = [profileBody(null)]
    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('clear')
  })

  it('keeps capture refused when durable takeover state cannot be read at all', async () => {
    const runtime = new FakeRuntime()
    runtime.unavailable = true
    const { guard } = restartMain(runtime)

    expect(await guard.reconcileNow()).toBe(false)
    expect(takeoverGuardState()).toBe('unreconciled')
    const refusal = await captureScreen(undefined, WORKING_CAPTURE_RUNTIME).catch((error: unknown) => error)
    expect(refusal).toBeInstanceOf(CaptureRefusedError)
    expect((refusal as CaptureRefusedError).code).toBe('capture_refused_takeover_unknown')

    // The outage is not read as "no takeover": only the runtime answering is.
    runtime.unavailable = false
    runtime.profiles = [profileBody(null)]
    expect(await guard.reconcileNow()).toBe(true)
    expect(takeoverGuardState()).toBe('clear')
  })

  it('refuses again when a later reconciliation cannot reach the runtime', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(null)]
    const { guard } = restartMain(runtime)
    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('clear')

    runtime.unavailable = true
    expect(await guard.reconcileNow()).toBe(false)
    expect(takeoverGuardState()).toBe('unreconciled')
  })

  it('rejects a malformed active takeover rather than degrading to "no takeover"', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody({ ...openTakeover(), status: 'SIGNED_IN' })]
    const { guard } = restartMain(runtime)

    expect(await guard.reconcileNow()).toBe(false)
    expect(takeoverGuardState()).toBe('unreconciled')
  })

  it('is idempotent: reconciling repeatedly reaches the same state', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(openTakeover())]
    const { guard } = restartMain(runtime)

    await guard.reconcileNow()
    await guard.reconcileNow()
    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('active')

    runtime.profiles = [profileBody(null)]
    await guard.reconcileNow()
    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('clear')
    await expect(captureScreen(undefined, WORKING_CAPTURE_RUNTIME)).resolves.toBeTruthy()
  })

  it('blocks capture for any open takeover, not only the profile a renderer would be showing', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [
      profileBody(null),
      profileBody(openTakeover(OTHER_ATTEMPT, OTHER_PROFILE, 'UNCONFIRMED'), OTHER_PROFILE)
    ]
    const { guard } = restartMain(runtime)

    await guard.reconcileNow()
    expect(takeoverGuardState()).toBe('active')
  })

  it('protects capture without any renderer poll: no IPC, no attempt id, no card', async () => {
    const runtime = new FakeRuntime()
    runtime.profiles = [profileBody(openTakeover())]
    const { controller, guard } = restartMain(runtime)

    await guard.reconcileNow()

    // The only runtime call made was the profile list; `getLoginTakeover` --
    // the poll the trusted card drives -- was never reached, and nothing here
    // ever named an attempt id.
    expect(runtime.calls).toBe(1)
    expect(takeoverGuardState()).toBe('active')
    void controller
  })

  it('treats an installation with no agent runtime as an answer, not an outage', async () => {
    clearTakeoverReconciliation()
    const runtime = new FakeRuntime()
    runtime.unavailable = true
    const controller = new BrowserProfileController(runtime)
    const guard = new TakeoverCaptureGuard({
      reconcile: () => controller.reconcileTakeovers(),
      agentRuntimeAbsent: () => true,
      setTimer: () => undefined,
      clearTimer: () => undefined
    })

    expect(await guard.reconcileNow()).toBe(true)
    expect(takeoverGuardState()).toBe('clear')
    // No request was made: there is nothing to ask.
    expect(runtime.calls).toBe(0)
  })

  it('retries on its own schedule until an answer arrives', async () => {
    const runtime = new FakeRuntime()
    runtime.unavailable = true
    clearTakeoverReconciliation()
    const controller = new BrowserProfileController(runtime)
    const timers: Array<() => void> = []
    const guard = new TakeoverCaptureGuard({
      reconcile: () => controller.reconcileTakeovers(),
      agentRuntimeAbsent: () => false,
      setTimer: (callback) => { timers.push(callback); return timers.length },
      clearTimer: () => undefined
    })

    await guard.reconcileNow()
    expect(timers).toHaveLength(1)
    expect(takeoverGuardState()).toBe('unreconciled')

    runtime.profiles = [profileBody(openTakeover())]
    runtime.unavailable = false
    timers[0]?.()
    await vi.waitFor(() => expect(takeoverGuardState()).toBe('active'))
  })
})

describe('the active-takeover boundary', () => {
  it('carries four fields and no fifth', () => {
    const schema = contract.schemas.ActiveTakeoverResponse
    expect(Object.keys(schema.properties).sort()).toEqual(
      ['attempt_id', 'expires_at', 'profile_id', 'status']
    )
    expect(schema.additionalProperties).toBe(false)
  })

  it('copies only those four fields, whatever else a response carries', () => {
    const parsed = parseActiveTakeover({
      ...openTakeover(),
      url: 'https://github.com/login',
      page_title: 'Sign in to GitHub',
      page_text: 'Password',
      profile_path: 'C:/Users/someone/AppData/Local/Lumi/browser-profiles/abc',
      credential_signals: ['PASSWORD_FIELD'],
      account_fingerprint: 'a'.repeat(64)
    })

    expect(Object.keys(parsed).sort()).toEqual(['attemptId', 'expiresAt', 'profileId', 'status'])
    expect(JSON.stringify(parsed)).not.toMatch(/github\.com|Password|browser-profiles|PASSWORD_FIELD|aaaa/)
  })

  it('keeps the profile view itself free of paths and page content', () => {
    const parsed = parseBrowserProfile(profileBody(openTakeover()))
    expect(parsed.activeTakeover?.attemptId).toBe(ATTEMPT)
    const serialized = JSON.stringify(parsed)
    expect(serialized).not.toMatch(/browser-profiles|userDataDir|user_data_dir|storage_state|cookie|password|http/i)
  })

  it('is absent, not guessed, when the runtime reports no open takeover', () => {
    expect(parseBrowserProfile(profileBody(null)).activeTakeover).toBeUndefined()
    const withoutField = { ...contract.examples.browser_profile_needs_login }
    delete withoutField.active_takeover
    expect(parseBrowserProfile(withoutField).activeTakeover).toBeUndefined()
  })

  it('matches the runtime contract example the runtime can actually emit', () => {
    const parsed = parseBrowserProfile(contract.examples.browser_profile_takeover_open)
    expect(parsed.activeTakeover).toEqual({
      profileId: PROFILE,
      attemptId: ATTEMPT,
      status: 'OPEN',
      expiresAt: '2026-09-16T10:15:05Z'
    })
  })
})
