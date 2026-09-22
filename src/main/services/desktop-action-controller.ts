import {
  DESKTOP_SCROLL_STEPS,
  type AgentDesktopActionView,
  type AgentDesktopScrollStep,
  type AgentDesktopScrollTargetList,
  type AgentError,
  type AgentRegisteredApp,
  type AgentResult
} from '../../shared/agent-contracts'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import type { RuntimeMethod, RuntimeReply } from './agent-runtime-supervisor'
import {
  parseDesktopAction,
  parseLatestDesktopAction,
  parseRegisteredApps,
  parseScrollTargets,
  projectDesktopActionError
} from './desktop-action-wire'

/**
 * The trusted domain client for Milestone 9 S3: trusted focus, semantic scroll and registered-app launch.
 *
 * Its own class, like `DesktopReadController`: `VoiceTaskBackend` is a `Pick<AgentTaskController, ...>`, so a
 * class it is never a member of cannot be reached from voice by construction. It imports no model
 * code, no interpreter and no memory: nothing here talks to an AI. A desktop action is authorized by ONE
 * thing, the trusted click on an exact card; reading a window (S2) is not authority for any of this.
 *
 * What this class cannot do is the design. There is no method that takes a window handle, a process, a path,
 * an argument, a screen position, a key, a selector or a script. Inputs are opaque ids this class re-validates,
 * a closed scroll step, and the action id plus the revision the card showed. Every effect goes through
 * `approveDesktopAction`, which is single-flight here and single-use in the runtime.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SURFACE_REF = /^s(?:[1-9]|1[0-6])$/
const CONTROL_REF = /^u(?:[1-9]\d?|1\d\d|200)$/
const APP_ID = /^[a-z][a-z0-9_-]{0,31}$/

const TIMEOUTS = {
  read: 10_000,
  // Proposals may start the desktop worker cold (measured 16-21 s) and observe one surface.
  propose: 90_000,
  // An approval performs one effect; a launch waits a few seconds for the window, then reads again.
  approve: 90_000,
  write: 30_000
} as const

function fail(code: AgentError['code'], message: string): never {
  throw new AgentRequestError({ code, message })
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

function id(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', `That ${what} reference is invalid.`)
  return value
}

function revision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) fail('invalid_request', 'That approval revision is invalid.')
  return value
}

function surface(refValue: unknown, epochValue: unknown): { surfaceRef: string; surfaceEpoch: number } {
  if (typeof refValue !== 'string' || !SURFACE_REF.test(refValue)) fail('invalid_request', 'That window reference is invalid.')
  if (typeof epochValue !== 'number' || !Number.isSafeInteger(epochValue) || epochValue < 1) fail('invalid_request', 'That window reference is invalid.')
  return { surfaceRef: refValue, surfaceEpoch: epochValue }
}

export class DesktopActionController {
  private readonly inFlight = new Set<string>()

  constructor(private readonly runtime: DesktopRuntimeRequester) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was changed.')
      if (error instanceof RuntimeRestartedError) {
        // The answer to an approval may never arrive. Whatever the runtime recorded is what is true; nothing
        // is retried, and a new card needs a new decision.
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectDesktopActionError(reply.status, reply.body))
    return reply
  }

  private async exclusive<T>(key: string, work: () => Promise<T>): Promise<AgentResult<T>> {
    if (this.inFlight.has(key)) return { ok: false, error: { code: 'busy', message: 'Lumi is already working on that.' } }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  private guarded<T>(key: string, validate: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      validate()
    } catch (error) {
      return Promise.resolve({ ok: false, error: toAgentError(error) })
    }
    return this.exclusive(key, work)
  }

  /** Read-only: the registered applications (ids and labels from trusted configuration). */
  listDesktopApps(): Promise<AgentResult<AgentRegisteredApp[]>> {
    return this.exclusive('desktop-action:apps', async () =>
      parseRegisteredApps((await this.call('GET', '/desktop/actions/apps', undefined, TIMEOUTS.read)).body))
  }

  /** Observe one surface locally and list only its scrollable controls. Nothing is sent anywhere. */
  findDesktopScrollTargets(
    workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown
  ): Promise<AgentResult<AgentDesktopScrollTargetList>> {
    let target: { surfaceRef: string; surfaceEpoch: number }
    let workerGeneration = ''
    return this.guarded('desktop-action:propose', () => {
      workerGeneration = id(workerGenerationValue, 'worker')
      target = surface(surfaceRefValue, surfaceEpochValue)
    }, async () => {
      const reply = await this.call('POST', '/desktop/actions/scroll-targets', {
        worker_generation: workerGeneration, surface_ref: target.surfaceRef, surface_epoch: target.surfaceEpoch
      }, TIMEOUTS.propose)
      return parseScrollTargets(reply.body)
    })
  }

  /** Open the exact approval card for bringing ONE surface to the front. Nothing happens yet. */
  proposeDesktopFocus(
    workerGenerationValue: unknown, surfaceRefValue: unknown, surfaceEpochValue: unknown
  ): Promise<AgentResult<AgentDesktopActionView>> {
    let target: { surfaceRef: string; surfaceEpoch: number }
    let workerGeneration = ''
    return this.guarded('desktop-action:propose', () => {
      workerGeneration = id(workerGenerationValue, 'worker')
      target = surface(surfaceRefValue, surfaceEpochValue)
    }, async () => {
      const reply = await this.call('POST', '/desktop/actions/focus', {
        worker_generation: workerGeneration, surface_ref: target.surfaceRef, surface_epoch: target.surfaceEpoch
      }, TIMEOUTS.propose)
      return parseDesktopAction(reply.body)
    })
  }

  /** Open the exact approval card for ONE semantic scroll by a closed step. Nothing happens yet. */
  proposeDesktopScroll(
    workerGenerationValue: unknown, observationIdValue: unknown, controlRefValue: unknown, stepValue: unknown
  ): Promise<AgentResult<AgentDesktopActionView>> {
    let workerGeneration = ''
    let observationId = ''
    let controlRef = ''
    let step: AgentDesktopScrollStep = 'small_down'
    return this.guarded('desktop-action:propose', () => {
      workerGeneration = id(workerGenerationValue, 'worker')
      observationId = id(observationIdValue, 'observation')
      if (typeof controlRefValue !== 'string' || !CONTROL_REF.test(controlRefValue)) fail('invalid_request', 'That control reference is invalid.')
      controlRef = controlRefValue
      if (typeof stepValue !== 'string' || !(DESKTOP_SCROLL_STEPS as readonly string[]).includes(stepValue)) {
        fail('invalid_request', 'That scroll amount is not one Lumi offers.')
      }
      step = stepValue as AgentDesktopScrollStep
    }, async () => {
      const reply = await this.call('POST', '/desktop/actions/scroll', {
        worker_generation: workerGeneration, observation_id: observationId, control_ref: controlRef, step
      }, TIMEOUTS.propose)
      return parseDesktopAction(reply.body)
    })
  }

  /** Open the exact approval card for opening ONE registered application. Nothing is started yet. */
  proposeDesktopLaunch(appIdValue: unknown): Promise<AgentResult<AgentDesktopActionView>> {
    let appId = ''
    return this.guarded('desktop-action:propose', () => {
      if (typeof appIdValue !== 'string' || !APP_ID.test(appIdValue)) fail('invalid_request', 'That is not an application Lumi can open.')
      appId = appIdValue
    }, async () => {
      const reply = await this.call('POST', '/desktop/actions/launch', { app_id: appId }, TIMEOUTS.propose)
      return parseDesktopAction(reply.body)
    })
  }

  /** Read-only: the newest desktop action, or none. */
  getDesktopAction(): Promise<AgentResult<AgentDesktopActionView | null>> {
    return this.exclusive('desktop-action:read', async () =>
      parseLatestDesktopAction((await this.call('GET', '/desktop/actions/latest', undefined, TIMEOUTS.read)).body))
  }

  /** The trusted "Approve" click. Names the action and the revision the card showed; performs ONE effect. */
  approveDesktopAction(actionIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopActionView>> {
    let actionId = ''
    let expected = 0
    return this.guarded('desktop-action:approve', () => {
      actionId = id(actionIdValue, 'action')
      expected = revision(revisionValue)
    }, async () => {
      const reply = await this.call('POST', `/desktop/actions/${actionId}/approve`, { expected_revision: expected }, TIMEOUTS.approve)
      return parseDesktopAction(reply.body)
    })
  }

  /** The trusted "Cancel" click. A declined action can never run. */
  declineDesktopAction(actionIdValue: unknown, revisionValue: unknown): Promise<AgentResult<AgentDesktopActionView>> {
    let actionId = ''
    let expected = 0
    return this.guarded('desktop-action:decline', () => {
      actionId = id(actionIdValue, 'action')
      expected = revision(revisionValue)
    }, async () => {
      const reply = await this.call('POST', `/desktop/actions/${actionId}/decline`, { expected_revision: expected }, TIMEOUTS.write)
      return parseDesktopAction(reply.body)
    })
  }

  /**
   * S4. Open the SECOND, separate exact execution approval card from one SUCCEEDED plan. This is the
   * moment disclosure authority ends and execution review begins: the runtime independently
   * re-verifies everything about the plan before any card exists, and nothing runs until a further,
   * separate `approveDesktopAction` click.
   */
  proposeDesktopActionFromPlan(planIdValue: unknown): Promise<AgentResult<AgentDesktopActionView>> {
    let planId = ''
    return this.guarded('desktop-action:propose', () => {
      planId = id(planIdValue, 'plan')
    }, async () => {
      const reply = await this.call('POST', '/desktop/actions/from-plan', { plan_id: planId }, TIMEOUTS.propose)
      return parseDesktopAction(reply.body)
    })
  }

  /**
   * The only way out of an unresolved S4 mutation. Names the action, the revision the card showed,
   * and what the person themselves observed -- never retries or re-derives the effect.
   */
  reconcileDesktopAction(
    actionIdValue: unknown, revisionValue: unknown, outcomeValue: unknown
  ): Promise<AgentResult<AgentDesktopActionView>> {
    let actionId = ''
    let expected = 0
    let outcome: 'succeeded' | 'failed' | 'still_unknown' = 'still_unknown'
    return this.guarded('desktop-action:reconcile', () => {
      actionId = id(actionIdValue, 'action')
      expected = revision(revisionValue)
      if (outcomeValue !== 'succeeded' && outcomeValue !== 'failed' && outcomeValue !== 'still_unknown') {
        fail('invalid_request', 'Report exactly what you observed.')
      }
      outcome = outcomeValue
    }, async () => {
      const reply = await this.call(
        'POST', `/desktop/actions/${actionId}/reconcile`, { expected_revision: expected, outcome }, TIMEOUTS.write
      )
      return parseDesktopAction(reply.body)
    })
  }
}
