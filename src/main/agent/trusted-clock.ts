import { isValidTimeZone } from '../../shared/relative-dates'

/**
 * The clock and time zone used to turn "tomorrow" into a date.
 *
 * Both come from Electron main, never from the renderer or a model:
 *
 * - `LUMI_TIMEZONE` (an IANA name) overrides the operating system's zone.
 * - `LUMI_FIXED_NOW` (ISO instant) pins the *calendar* clock. It is honoured
 *   only in unpackaged builds, so acceptance tests that quote the fixture's
 *   Saturday keep working on any later date. It never affects approval expiry
 *   or any other comparison with runtime timestamps.
 */

export interface CalendarClock {
  now: () => number
  timeZone: () => string
}

export function trustedCalendarClock(options: { allowFixedNow: boolean; environment?: NodeJS.ProcessEnv }): CalendarClock {
  const environment = options.environment ?? process.env
  const configuredZone = environment.LUMI_TIMEZONE?.trim()
  const zone = configuredZone && isValidTimeZone(configuredZone)
    ? configuredZone
    : Intl.DateTimeFormat().resolvedOptions().timeZone
  const fixed = options.allowFixedNow ? Date.parse(environment.LUMI_FIXED_NOW ?? '') : Number.NaN
  if (Number.isFinite(fixed)) {
    const offset = fixed - Date.now()
    return { now: () => Date.now() + offset, timeZone: () => zone }
  }
  return { now: () => Date.now(), timeZone: () => zone }
}
