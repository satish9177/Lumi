import {
  BROWSER_PROFILE_STATUSES,
  LOGIN_ATTEMPT_STATUSES,
  type AgentActiveTakeoverView,
  type AgentBrowserProfileView,
  type AgentLoginAttemptView,
  type AgentLoginTakeoverView
} from '../../shared/agent-contracts'
import { WireError, isRecord } from './agent-wire'
import type { AgentError } from '../../shared/agent-contracts'

/**
 * Strict parsers from runtime JSON to the closed Milestone 8a S2 desktop DTOs.
 *
 * Same discipline as `agent-wire.ts`: a violation rejects the whole response
 * rather than guessing, and only known, bounded fields are ever copied. What
 * is deliberately never read here, even though the runtime's response may
 * carry it: a profile directory path, a cookie, a token, a Chromium build, a
 * lease detail, or any raw account identity. Only ids, a site name, a status
 * and timestamps cross this boundary.
 */

type Json = Record<string, unknown>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SITE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/
const LABEL = /^.{1,60}$/
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/

function record(value: unknown, what: string): Json {
  if (!isRecord(value)) throw new WireError(what)
  return value
}

function uuid(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) throw new WireError(what)
  return value
}

function integer(value: unknown, what: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) throw new WireError(what)
  return value
}

function instant(value: unknown, what: string): string {
  if (typeof value !== 'string' || !INSTANT.test(value) || Number.isNaN(Date.parse(value))) throw new WireError(what)
  return value
}

function nullableInstant(value: unknown, what: string): string | undefined {
  return value === null || value === undefined ? undefined : instant(value, what)
}

function member<T extends string>(values: readonly T[], value: unknown, what: string): T {
  if (typeof value !== 'string' || !(values as readonly string[]).includes(value)) throw new WireError(what)
  return value as T
}

function text(value: unknown, what: string, pattern: RegExp): string {
  if (typeof value !== 'string' || !pattern.test(value)) throw new WireError(what)
  return value
}

/**
 * The open takeover carried on a profile, if any. Exactly four fields are
 * read and every one is checked: anything else the runtime might put here is
 * dropped, and a malformed value rejects the whole profile rather than
 * degrading to "no takeover" -- which would be the one failure direction
 * that could let a capture through while a sign-in window is on screen.
 */
export function parseActiveTakeover(value: unknown): AgentActiveTakeoverView {
  const takeover = record(value, 'active_takeover')
  return {
    profileId: uuid(takeover.profile_id, 'active_takeover.profile_id'),
    attemptId: uuid(takeover.attempt_id, 'active_takeover.attempt_id'),
    status: member(LOGIN_ATTEMPT_STATUSES, takeover.status, 'active_takeover.status'),
    expiresAt: instant(takeover.expires_at, 'active_takeover.expires_at')
  }
}

export function parseBrowserProfile(value: unknown): AgentBrowserProfileView {
  const profile = record(value, 'browser_profile')
  return {
    profileId: uuid(profile.id, 'browser_profile.id'),
    label: text(profile.label, 'browser_profile.label', LABEL),
    site: text(profile.site, 'browser_profile.site', SITE),
    status: member(BROWSER_PROFILE_STATUSES, profile.status, 'browser_profile.status'),
    revision: integer(profile.revision, 'browser_profile.revision', 1),
    ...(nullableInstant(profile.last_login_completed_at, 'browser_profile.last_login_completed_at')
      ? { lastLoginCompletedAt: nullableInstant(profile.last_login_completed_at, 'browser_profile.last_login_completed_at') }
      : {}),
    ...(nullableInstant(profile.last_observed_at, 'browser_profile.last_observed_at')
      ? { lastObservedAt: nullableInstant(profile.last_observed_at, 'browser_profile.last_observed_at') }
      : {}),
    ...(profile.active_takeover === null || profile.active_takeover === undefined
      ? {}
      : { activeTakeover: parseActiveTakeover(profile.active_takeover) })
  }
}

export function parseBrowserProfileList(value: unknown): AgentBrowserProfileView[] {
  const body = record(value, 'browser_profiles')
  if (!Array.isArray(body.profiles)) throw new WireError('browser_profiles.profiles')
  return body.profiles.map(parseBrowserProfile)
}

export function parseLoginAttempt(value: unknown): AgentLoginAttemptView {
  const attempt = record(value, 'login_attempt')
  return {
    attemptId: uuid(attempt.id, 'login_attempt.id'),
    profileId: uuid(attempt.profile_id, 'login_attempt.profile_id'),
    status: member(LOGIN_ATTEMPT_STATUSES, attempt.status, 'login_attempt.status'),
    startedAt: instant(attempt.started_at, 'login_attempt.started_at'),
    expiresAt: instant(attempt.expires_at, 'login_attempt.expires_at'),
    ...(nullableInstant(attempt.completed_at, 'login_attempt.completed_at')
      ? { completedAt: nullableInstant(attempt.completed_at, 'login_attempt.completed_at') }
      : {}),
    ...(nullableInstant(attempt.cancelled_at, 'login_attempt.cancelled_at')
      ? { cancelledAt: nullableInstant(attempt.cancelled_at, 'login_attempt.cancelled_at') }
      : {})
  }
}

export function parseLoginTakeover(value: unknown): AgentLoginTakeoverView {
  const body = record(value, 'login_takeover')
  const reason = body.refusal_reason
  if (reason !== null && reason !== undefined && !(typeof reason === 'string' && CODE.test(reason))) {
    throw new WireError('login_takeover.refusal_reason')
  }
  return {
    attempt: parseLoginAttempt(body.attempt),
    profile: parseBrowserProfile(body.profile),
    ...(typeof reason === 'string' ? { refusalReason: reason } : {})
  }
}

/** App-authored wording for a stable `browser_profile_refused` / `login_attempt_refused`
 * server reason. Website text never reaches this function -- the reason is
 * always one of the closed codes the runtime emits. */
export function describeProfileRefusal(reason: string | undefined): string {
  switch (reason) {
    case 'profile_not_found':
    case 'profile_deleted':
      return 'That profile no longer exists.'
    case 'stale_revision':
      return 'This profile changed since you last viewed it. Refresh and try again.'
    case 'login_attempt_already_open':
      return 'A sign-in window is already open for this profile.'
    case 'login_attempt_not_found':
      return 'That sign-in attempt no longer exists.'
    case 'login_attempt_not_open':
      return 'That sign-in window has already ended.'
    case 'login_attempt_expired':
      return 'The sign-in window timed out. Try signing in again.'
    case 'login_navigation_failed':
      return 'Lumi could not open the sign-in page. Try again.'
    case 'login_profile_not_open':
      return "The profile's browser window is not open."
    case 'profile_open_mode_mismatch':
      return 'This profile is already open in a different mode. Close it and try again.'
    case 'profile_locked_by_another_process':
    case 'profile_lease_unavailable':
      return 'This profile is in use elsewhere on this computer right now.'
    case 'profile_browser_downgrade_refused':
      return "This profile needs a newer version of Lumi's browser."
    case 'profile_session_limit':
      return 'Lumi can only have one browser profile open at a time right now.'
    case 'profile_directory_unavailable':
    case 'profile_open_failed':
      return "Lumi could not open this profile's browser."
    default:
      return 'That request was refused.'
  }
}

/**
 * The runtime error projection for a browser-profile or login-takeover
 * response. `browser_profile_refused` and `login_attempt_refused` are the
 * only two server-side codes this module's routes can emit; both carry a
 * `reason` sub-code and neither ever carries page text or a path.
 */
export function projectProfileRuntimeError(status: number, value: unknown): AgentError {
  const body = isRecord(value) && isRecord(value.error) ? value.error : undefined
  const runtimeCode = typeof body?.code === 'string' ? body.code : ''
  if (runtimeCode === 'browser_profile_refused' || runtimeCode === 'login_attempt_refused') {
    const reason = typeof body?.reason === 'string' && CODE.test(body.reason) ? body.reason : undefined
    return { code: 'browser_profile_refused', message: describeProfileRefusal(reason) }
  }
  if (runtimeCode === 'invalid_request') {
    return { code: 'invalid_request', message: 'Lumi refused an invalid request.' }
  }
  return {
    code: status === 401 || status === 403 || status === 400 ? 'runtime_unavailable' : 'request_failed',
    message: 'The agent runtime could not complete that request.'
  }
}
