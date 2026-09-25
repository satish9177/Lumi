import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ActiveTaskStore, AgentTaskController, type AuthenticatedSupport } from './agent-tasks'
import { FakeAuthenticatedRuntime } from '../testing/fake-authenticated-runtime'
import type { AuthenticatedPlanOutcome } from '../agent/authenticated-planner'

/**
 * Milestone 12 S3: `continueAccountRead`'s own re-observe nudge, isolated from the rest of `runAuthenticated`
 * (already covered end to end by `agent-authenticated.test.ts`, which this file leaves untouched).
 *
 * The one property under test, directly: **a fresh, forced re-observe never assumes the human's part of a
 * manual handoff happened, and only ever revokes the grant when it is truly, permanently dead -- never
 * merely because the human has not finished yet.** A premature Continue (still `NEEDS_LOGIN`, or a takeover
 * still open) must leave the grant alone so a later, real Continue can still succeed; only a grant the
 * database itself says can never work again (`authenticated_grant_not_usable`/`authenticated_grant_not_found`,
 * which the wire layer maps to `authenticated_not_granted`) may be revoked.
 */

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-continue-account-read-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

/**
 * `pausedForLogin`'s own `runAuthenticated()` setup needs exactly one real planner decision (choose
 * `observe`) to reach the first step that then pauses. Every `continueAccountRead` call in these tests is
 * expected to fail on its OWN forced, fresh re-observe before ever reaching the planner again -- so a second
 * call is a test bug, not a legitimate path, and fails loudly rather than silently taking some other step.
 */
function oneShotObserveSupport(): AuthenticatedSupport {
  let calls = 0
  return {
    planner: {
      recipients: () => ['gemini'],
      next: async (): Promise<AuthenticatedPlanOutcome> => {
        calls += 1
        if (calls > 1) throw new Error('the planner must not be called again: the forced re-observe should fail first')
        return { decision: { kind: 'step', step: { operation: 'observe', tab: 't1' }, reason: 'look' }, provider: 'gemini', model: 'scripted-1' }
      }
    },
    answerer: {
      recipients: () => ['gemini'],
      answer: () => { throw new Error('the answerer must not be called: nothing succeeded') }
    }
  }
}

function controllerWith(runtime: FakeAuthenticatedRuntime): AgentTaskController {
  return new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, undefined, oneShotObserveSupport())
}

async function pausedForLogin(controller: AgentTaskController, runtime: FakeAuthenticatedRuntime): Promise<string> {
  const created = await controller.createAuthenticatedTask(
    'Which of my repositories are private?', '00000000-0000-4000-8000-0000000000cc', 'gemini'
  )
  expect(created.ok).toBe(true)
  if (!created.ok) throw new Error('unreachable')
  const grant = created.value.authenticated?.grant
  expect(grant).toBeDefined()
  const confirmed = await controller.grantAuthenticatedScope(grant!.grantId, grant!.revision)
  expect(confirmed.ok).toBe(true)
  runtime.pauseNext = 'login_required'
  const run = await controller.runAuthenticated()
  expect(run.ok).toBe(true)
  if (!run.ok) throw new Error('unreachable')
  expect(run.value.authenticated?.pauseReason).toBe('login_required')
  return created.value.task.taskId
}

describe('continueAccountRead: a premature Continue never assumes success and never revokes a live grant', () => {
  it('leaves an ACTIVE grant alone when the profile still just needs login, and stays paused', async () => {
    const runtime = new FakeAuthenticatedRuntime()
    const controller = controllerWith(runtime)
    const taskId = await pausedForLogin(controller, runtime)

    // The user pressed Continue, but has not actually finished signing in: a fresh forced observe hits the
    // SAME profile pre-check the real service applies at every step, not just at grant time.
    runtime.refuseNextStepAsProfileUnavailable = 'profile_not_authenticated'
    const continued = await controller.continueAccountRead(taskId)
    expect(continued.ok).toBe(true)

    // The grant must still be ACTIVE -- never revoked for a merely-not-finished-yet state.
    const revokeCalls = runtime.calls.filter((call) => call.method === 'POST' && /\/authenticated\/revoke$/.test(call.path))
    expect(revokeCalls).toEqual([])
    const snapshot = await controller.loadActiveTask(0)
    expect(snapshot.ok).toBe(true)
    if (!snapshot.ok || !snapshot.value) throw new Error('unreachable')
    expect(snapshot.value.authenticated?.grant?.status).toBe('ACTIVE')

    // A second, real re-observe (the human actually finished) must still be possible on the SAME grant --
    // it actually reaches the browser this time, proving the earlier premature Continue left the grant
    // genuinely usable rather than merely not-yet-revoked.
    expect(runtime.pagesServed).toBe(0)
    const second = await controller.continueAccountRead(taskId)
    expect(second.ok).toBe(true)
    expect(runtime.pagesServed).toBe(1)
  })

  it('leaves an ACTIVE grant alone while a login takeover is still open, and stays paused', async () => {
    const runtime = new FakeAuthenticatedRuntime()
    const controller = controllerWith(runtime)
    const taskId = await pausedForLogin(controller, runtime)

    runtime.refuseNextStepAsProfileUnavailable = 'profile_takeover_active'
    const continued = await controller.continueAccountRead(taskId)
    expect(continued.ok).toBe(true)

    const revokeCalls = runtime.calls.filter((call) => call.method === 'POST' && /\/authenticated\/revoke$/.test(call.path))
    expect(revokeCalls).toEqual([])
    const snapshot = await controller.loadActiveTask(0)
    expect(snapshot.ok).toBe(true)
    if (!snapshot.ok || !snapshot.value) throw new Error('unreachable')
    expect(snapshot.value.authenticated?.grant?.status).toBe('ACTIVE')
  })

  it('revokes the grant when a fresh re-observe finds it permanently unusable, and never retries it', async () => {
    const runtime = new FakeAuthenticatedRuntime()
    const controller = controllerWith(runtime)
    const taskId = await pausedForLogin(controller, runtime)

    // The account changed underneath the grant: a fresh, right-now fingerprint/epoch check inside the
    // forced step itself finds it dead, even though the grant still read as ACTIVE a moment ago (the exact
    // race `AuthenticatedReadService.execute_step`'s own pre-check guards against).
    runtime.refuseNextStepAsGrantUnusable = 'the account changed since you allowed this'

    const continued = await controller.continueAccountRead(taskId)
    expect(continued.ok).toBe(true)

    const revokeCalls = runtime.calls.filter((call) => call.method === 'POST' && /\/authenticated\/revoke$/.test(call.path))
    expect(revokeCalls.length).toBeGreaterThanOrEqual(1)
    expect((revokeCalls.at(-1)?.body as { reason?: string } | undefined)?.reason).toBe('grant_unusable')
  })
})
