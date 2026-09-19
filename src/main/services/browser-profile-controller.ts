import {
  OPEN_LOGIN_ATTEMPT_STATUSES,
  type AgentBrowserProfileView,
  type AgentError,
  type AgentLoginAttemptView,
  type AgentLoginTakeoverView,
  type AgentResult
} from '../../shared/agent-contracts'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import {
  parseBrowserProfileList,
  parseLoginTakeover,
  parseLoginAttempt,
  projectProfileRuntimeError
} from './browser-profile-wire'
import { WireError } from './agent-wire'
import { setTakeoverActive } from './capture'

/**
 * The trusted domain client for Milestone 8a S2's manual login takeover.
 *
 * Deliberately its own class, not a method group on `AgentTaskController`:
 * profiles and takeovers are not task-scoped, and keeping this separate is
 * also what makes voice exclusion structural rather than conventional --
 * `VoiceTaskBackend` is `Pick<AgentTaskController, ...>`, so a class this is
 * never a member of cannot be reached from voice by construction.
 *
 * Every mutation takes an id (or two) and the revision the trusted card
 * showed. There is no method here that takes a hostname, a URL, a path, an
 * executable, a credential or a browser argument -- `listBrowserProfiles`
 * takes no argument at all, and there is deliberately no `createBrowserProfile`
 * reachable from the renderer, because that would mean the renderer choosing
 * a site from arbitrary text.
 */

export interface RuntimeRequester {
  request(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply>
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

const TIMEOUTS = {
  read: 10_000,
  // Opening a headed window launches Chromium and navigates it; generous on
  // purpose, the same reasoning as the booking executor's timeouts.
  open: 60_000,
  confirm: 30_000,
  cancel: 30_000
} as const

function fail(message: string): never {
  throw new AgentRequestError({ code: 'invalid_request', message })
}

function parseProfileId(value: unknown): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('That profile reference is invalid.')
  return value
}

function parseAttemptId(value: unknown): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('That sign-in reference is invalid.')
  return value
}

function parseExpectedRevision(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
    fail('That profile revision is invalid.')
  }
  return value
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

export class BrowserProfileController {
  private readonly inFlight = new Set<string>()
  /**
   * Attempt ids this controller currently believes are `OPEN`/`UNCONFIRMED`.
   * Feeds `capture.ts`'s screen-capture exclusion: every result that carries
   * an attempt's status updates this set and `setTakeoverActive`, so a
   * takeover the human is still driving keeps capture refused even between
   * explicit start/confirm/cancel calls, as long as the trusted card is
   * polling `getLoginTakeover` (which it must do anyway to detect expiry).
   */
  private readonly openAttempts = new Set<string>()

  constructor(private readonly runtime: RuntimeRequester) {}

  private track(attempt: AgentLoginAttemptView): void {
    if ((OPEN_LOGIN_ATTEMPT_STATUSES as readonly string[]).includes(attempt.status)) {
      this.openAttempts.add(attempt.attemptId)
    } else {
      this.openAttempts.delete(attempt.attemptId)
    }
    setTakeoverActive(this.openAttempts.size > 0)
  }

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) {
        fail('The Lumi agent runtime is not running. Nothing was sent.')
      }
      if (error instanceof RuntimeRestartedError) {
        fail('Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) {
      throw new AgentRequestError(projectProfileRuntimeError(reply.status, reply.body))
    }
    return reply
  }

  private async exclusive<T>(key: string, work: () => Promise<T>): Promise<AgentResult<T>> {
    if (this.inFlight.has(key)) {
      return { ok: false, error: { code: 'request_failed', message: 'Lumi is already working on that.' } }
    }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  /** Read-only: every Lumi-managed browser profile. Takes no argument. */
  async listBrowserProfiles(): Promise<AgentResult<AgentBrowserProfileView[]>> {
    return this.exclusive('profiles:list', async () => {
      const reply = await this.call('GET', '/browser-profiles', undefined, TIMEOUTS.read)
      return parseBrowserProfileList(reply.body)
    })
  }

  /**
   * The trusted "Sign in manually" click. Opens a headed window and suspends
   * agent automation for the life of the takeover it starts.
   */
  async openLoginWindow(profileIdValue: unknown, expectedRevisionValue: unknown): Promise<AgentResult<AgentLoginTakeoverView>> {
    const profileId = parseProfileId(profileIdValue)
    const expectedRevision = parseExpectedRevision(expectedRevisionValue)
    return this.exclusive(`profile:${profileId}`, async () => {
      const reply = await this.call(
        'POST', `/browser-profiles/${profileId}/takeover`, { expected_revision: expectedRevision }, TIMEOUTS.open
      )
      const takeover = parseLoginTakeover(reply.body)
      this.track(takeover.attempt)
      return takeover
    })
  }

  /**
   * The trusted "I'm signed in" click. Not, by itself, an authentication
   * claim -- it only starts the deterministic post-login check.
   */
  async confirmSignedIn(
    profileIdValue: unknown, attemptIdValue: unknown, expectedRevisionValue: unknown
  ): Promise<AgentResult<AgentLoginTakeoverView>> {
    const profileId = parseProfileId(profileIdValue)
    const attemptId = parseAttemptId(attemptIdValue)
    const expectedRevision = parseExpectedRevision(expectedRevisionValue)
    return this.exclusive(`profile:${profileId}`, async () => {
      const reply = await this.call(
        'POST',
        `/browser-profiles/${profileId}/takeover/${attemptId}/confirm`,
        { expected_revision: expectedRevision },
        TIMEOUTS.confirm
      )
      const takeover = parseLoginTakeover(reply.body)
      this.track(takeover.attempt)
      return takeover
    })
  }

  /** The trusted "Cancel" click. Never a logout. */
  async cancelLogin(
    profileIdValue: unknown, attemptIdValue: unknown, expectedRevisionValue: unknown
  ): Promise<AgentResult<AgentLoginTakeoverView>> {
    const profileId = parseProfileId(profileIdValue)
    const attemptId = parseAttemptId(attemptIdValue)
    const expectedRevision = parseExpectedRevision(expectedRevisionValue)
    return this.exclusive(`profile:${profileId}`, async () => {
      const reply = await this.call(
        'POST',
        `/browser-profiles/${profileId}/takeover/${attemptId}/cancel`,
        { expected_revision: expectedRevision },
        TIMEOUTS.cancel
      )
      const takeover = parseLoginTakeover(reply.body)
      this.track(takeover.attempt)
      return takeover
    })
  }

  /** Read-only: one takeover's bounded interval and how it ended. Also the
   * poll the trusted card uses to detect expiry and to keep the capture
   * exclusion accurate between explicit mutations. */
  async getLoginTakeover(profileIdValue: unknown, attemptIdValue: unknown): Promise<AgentResult<AgentLoginAttemptView>> {
    const profileId = parseProfileId(profileIdValue)
    const attemptId = parseAttemptId(attemptIdValue)
    return this.exclusive(`profile-read:${attemptId}`, async () => {
      const reply = await this.call(
        'GET', `/browser-profiles/${profileId}/takeover/${attemptId}`, undefined, TIMEOUTS.read
      )
      const attempt = parseLoginAttempt(reply.body)
      this.track(attempt)
      return attempt
    })
  }
}
