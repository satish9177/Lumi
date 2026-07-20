/**
 * Resolving the people a request named, and describing what was found.
 *
 * The one place a label becomes a profile id. That direction is deliberate and
 * one-way: text arrives from the renderer or Realtime, ids stay in main. No
 * function here accepts an id from outside, and nothing here returns one to a
 * caller that would forward it onward.
 *
 * The vocabulary is also fixed here. Every phrase Lumi says about a face match
 * is app-authored in this file, built from the tier and the user's own label —
 * never from a model, and never stronger than "likely".
 */

import type { MatchTier } from './face-matching'
import type { PersonProfileStore, StoredPersonProfile } from './person-profiles'

export interface ResolvedPeople {
  /** Profiles that exist, in the order the request named them. */
  found: StoredPersonProfile[]
  /** Labels with no profile, in the user's own casing, for the reply. */
  missing: string[]
}

/**
 * Exact, case-insensitive resolution against enrolled profiles.
 *
 * Deliberately exact: no fuzzy matching, no nearest-neighbour on the label. If
 * someone has both "Mum" and "Mum's sister" enrolled, a near-miss must not
 * silently return the wrong person's photos, so an unrecognised label is
 * reported as missing rather than guessed at.
 */
export function resolvePeopleLabels(profiles: PersonProfileStore, labels: readonly string[]): ResolvedPeople {
  const found: StoredPersonProfile[] = []
  const missing: string[] = []

  for (const label of labels) {
    const profile = profiles.resolveLabel(label)
    if (profile) {
      found.push(profile)
    } else {
      missing.push(label)
    }
  }

  return { found, missing }
}

/**
 * What Lumi says when a requested name was never enrolled.
 *
 * States the fact and stops. It does not offer to create the profile, because
 * enrolment is an explicit flow the user starts, not something a search talks
 * them into.
 */
export function missingProfileMessage(missing: readonly string[]): string {
  if (missing.length === 0) {
    return ''
  }
  if (missing.length === 1) {
    return `You haven’t created a profile called ${missing[0]} yet.`
  }
  const names = missing.slice(0, -1).join(', ')
  return `You haven’t created profiles called ${names} or ${missing[missing.length - 1]} yet.`
}

/**
 * The reason shown on a result card.
 *
 * Built from the tier and the label, and nothing else. There is no branch here
 * that produces "this is X" or "X confirmed": the strongest available phrasing
 * is "Likely match", which is as much as the measurement supports.
 */
export function peopleReason(entries: ReadonlyArray<{ label: string; tier: MatchTier }>): string {
  const likely = entries.filter((entry) => entry.tier === 'likely').map((entry) => entry.label)
  const possible = entries.filter((entry) => entry.tier === 'possible').map((entry) => entry.label)

  if (likely.length > 0 && possible.length === 0) {
    return likely.length === 1
      ? `Likely match for ${likely[0]}`
      : `Likely matches for ${joinNames(likely)}`
  }
  if (possible.length > 0 && likely.length === 0) {
    return possible.length === 1
      ? `Possible match for ${possible[0]}`
      : `Possible matches for ${joinNames(possible)}`
  }
  if (likely.length > 0 && possible.length > 0) {
    // Mixed confidence in one photo. Naming which is which keeps the weaker
    // claim from borrowing the stronger one's credibility.
    return `Likely match for ${joinNames(likely)}, possible match for ${joinNames(possible)}`
  }
  return 'No reliable match found'
}

/** "Not checked yet" is a distinct answer from "not present". */
export function notCheckedMessage(label: string): string {
  return `Not checked for ${label} yet`
}

/**
 * Coverage wording when a scan is incomplete.
 *
 * Said whenever any photo in scope is unscanned, because otherwise an empty
 * result reads as "they are not in your photos" — a claim Lumi is not in a
 * position to make.
 */
export function coverageMessage(labels: readonly string[], uncheckedCount: number): string {
  if (uncheckedCount <= 0 || labels.length === 0) {
    return ''
  }
  return labels.length === 1
    ? `Some photos haven’t been checked for ${labels[0]} yet.`
    : `Some photos haven’t been checked for ${joinNames(labels)} yet.`
}

function joinNames(names: readonly string[]): string {
  if (names.length <= 1) {
    return names[0] ?? ''
  }
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}
