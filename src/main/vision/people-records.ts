/**
 * Reading per-photo match records back into an answer about one person.
 *
 * The index store knows how to persist a match outcome; it deliberately knows
 * nothing about profiles. This module is where the two meet, and it exists as
 * its own file because one rule has to hold in exactly one place:
 *
 *   **An absent, stale, or unreadable record is `not_checked` — never "no".**
 *
 * Every way a record can fail to apply funnels through `resolveMatch` and comes
 * out as `not_checked`: the photo was never scanned, the scan is queued, the
 * embedding model changed, the matching rules changed, the profile gained a
 * reference since, or the row was dropped by the parser. A photo that was never
 * looked at and a photo that was looked at and found nothing are different
 * claims, and only one of them is something Lumi is entitled to say.
 *
 * Coverage is computed the same way, so the progress the user reads and the
 * filtering the search performs cannot disagree: both call the same resolver.
 */

import type { PeopleMatchStatus, PhotoIndexRecord } from './index-store'

/** The minimum a caller must know about a profile to read its records. */
export interface ProfileIdentity {
  id: string
  /** Evidence revision; see StoredPersonProfile.revision. */
  revision: number
}

/** Statuses that mean this photo qualifies as a match for the profile. */
export const QUALIFYING_MATCH_STATUSES: readonly PeopleMatchStatus[] = ['likely', 'possible']

export interface ResolvedMatch {
  status: PeopleMatchStatus
  /** Zero unless the status is a qualifying tier. */
  matchingFaces: number
}

const NOT_CHECKED: ResolvedMatch = Object.freeze({ status: 'not_checked', matchingFaces: 0 })

/**
 * Resolves one photo's stored outcome for one profile.
 *
 * `inFlight` is the coordinator's live view of what it is working on right now.
 * It is passed in rather than stored because "checking" is process state, not
 * index state — a record saying `checking` on disk would survive a crash and
 * become a permanent lie.
 */
export function resolveMatch(
  record: Pick<PhotoIndexRecord, 'peopleStatus' | 'peopleMatches' | 'peopleFailureCode'> | undefined,
  profile: ProfileIdentity,
  inFlight = false
): ResolvedMatch {
  if (inFlight) {
    return { status: 'checking', matchingFaces: 0 }
  }
  if (!record || record.peopleStatus === undefined) {
    return NOT_CHECKED
  }

  // A photo-level failure applies to every profile: the pipeline never got far
  // enough to say anything about any of them.
  if (record.peopleStatus === 'failed') {
    return {
      status: record.peopleFailureCode !== undefined && isRetryable(record.peopleFailureCode) ? 'failed_retryable' : 'failed_permanent',
      matchingFaces: 0
    }
  }
  if (record.peopleStatus === 'pending') {
    return NOT_CHECKED
  }
  // 'skipped' means the photo is ineligible (no decodable image, say). Treating
  // that as a negative would let an unreadable file count as evidence of
  // absence, so it stays "not checked" too.
  if (record.peopleStatus === 'skipped') {
    return NOT_CHECKED
  }

  const stored = record.peopleMatches?.find((match) => match.profileId === profile.id)
  if (!stored) {
    // Absence is *not* a negative, even on a completed scan. A profile created
    // after this photo was checked is nowhere in its outcomes, and reporting
    // that as "no reliable match" would answer a question that was never asked.
    //
    // This is why a scan writes an explicit `checked_no_reliable_match` row for
    // every profile it considered rather than only for the ones that matched:
    // it makes "we checked and it wasn't them" a thing that was *written*, and
    // leaves absence meaning exactly one thing.
    return NOT_CHECKED
  }
  if (stored.profileRevision !== profile.revision) {
    // The profile gained or lost a reference after this outcome was computed.
    // The stored answer describes a profile that no longer exists.
    return NOT_CHECKED
  }
  return { status: stored.status, matchingFaces: stored.matchingFaces }
}

/** Whether a resolved status means this photo should appear for this person. */
export function qualifiesAsMatch(match: ResolvedMatch): boolean {
  return QUALIFYING_MATCH_STATUSES.includes(match.status)
}

/**
 * Coverage for one profile across a set of photos.
 *
 * Deliberately shaped so that `checked + unchecked + failed === total` and a
 * caller cannot report completion without the unchecked count being zero. See
 * `isComplete` below, which is the only sanctioned way to make that claim.
 */
export interface PeopleCoverage {
  total: number
  checked: number
  unchecked: number
  failed: number
  matched: number
}

export function coverageFor(
  records: Iterable<PhotoIndexRecord>,
  profile: ProfileIdentity,
  inFlight?: ReadonlySet<string>
): PeopleCoverage {
  const coverage: PeopleCoverage = { total: 0, checked: 0, unchecked: 0, failed: 0, matched: 0 }

  for (const record of records) {
    if (record.status === 'deleted') {
      continue
    }
    coverage.total += 1
    const resolved = resolveMatch(record, profile, inFlight?.has(record.imageId) ?? false)
    if (resolved.status === 'failed_retryable' || resolved.status === 'failed_permanent') {
      coverage.failed += 1
      continue
    }
    if (resolved.status === 'not_checked' || resolved.status === 'checking') {
      coverage.unchecked += 1
      continue
    }
    coverage.checked += 1
    if (qualifiesAsMatch(resolved)) {
      coverage.matched += 1
    }
  }

  return coverage
}

/**
 * Complete means every eligible photo has an answer. A permanently failed photo
 * counts as answered — it will never have one — but a retryable failure does
 * not, because the work is still outstanding.
 */
export function isComplete(coverage: PeopleCoverage): boolean {
  return coverage.total > 0 && coverage.unchecked === 0
}

function isRetryable(code: string): boolean {
  return RETRYABLE.has(code)
}

// Kept as a local set rather than importing the array, so this module has one
// import from index-store and no chance of a cycle through the coordinator.
const RETRYABLE = new Set<string>([
  'file_locked',
  'detection_failed',
  'embedding_failed',
  'face_model_unavailable',
  'profile_store_unavailable'
])
