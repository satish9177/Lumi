/**
 * Reading match records back.
 *
 * Almost every test here is a variation on one question: *when is Lumi allowed
 * to say "not them"?* The answer is "only when it looked and found nothing",
 * and every other circumstance — never scanned, mid-scan, model changed, rules
 * changed, profile changed, scan failed, file unreadable — has to come back as
 * `not_checked`. Getting this wrong does not crash anything; it quietly tells
 * someone their photos of a person do not exist.
 */

import { describe, expect, it } from 'vitest'
import { computeImageId, type PeopleMatchRecord, type PhotoIndexRecord } from './index-store'
import { coverageFor, isComplete, qualifiesAsMatch, resolveMatch } from './people-records'

const FATHER = { id: 'profile-father', revision: 4 }

function record(overrides: Partial<PhotoIndexRecord> = {}): PhotoIndexRecord {
  const relativePath = overrides.relativePath ?? 'a.jpg'
  return {
    imageId: computeImageId('root', relativePath),
    rootId: 'root',
    relativePath,
    name: relativePath,
    mtimeMs: 1,
    sizeBytes: 1,
    modelVersion: 1,
    status: 'indexed',
    attempts: 1,
    updatedAtMs: 1,
    ...overrides
  }
}

function scanned(matches: PeopleMatchRecord[], overrides: Partial<PhotoIndexRecord> = {}): PhotoIndexRecord {
  return record({ peopleStatus: 'done', peopleMatches: matches, ...overrides })
}

function matched(status: PeopleMatchRecord['status'], overrides: Partial<PeopleMatchRecord> = {}): PeopleMatchRecord {
  return { profileId: FATHER.id, status, matchingFaces: 1, profileRevision: FATHER.revision, ...overrides }
}

describe('an answer is only given when one was actually computed', () => {
  it('reads a stored likely match', () => {
    expect(resolveMatch(scanned([matched('likely')]), FATHER)).toEqual({ status: 'likely', matchingFaces: 1 })
  })

  it('reads a written negative as a negative', () => {
    expect(resolveMatch(scanned([matched('checked_no_reliable_match', { matchingFaces: 0 })]), FATHER).status).toBe(
      'checked_no_reliable_match'
    )
  })

  it('treats a photo with no record at all as not checked', () => {
    expect(resolveMatch(record(), FATHER).status).toBe('not_checked')
    expect(resolveMatch(undefined, FATHER).status).toBe('not_checked')
  })

  it('treats a scanned photo that never considered this profile as not checked', () => {
    // The profile was created after this photo was scanned. Absence of a row is
    // not evidence of absence of the person.
    const other = scanned([{ profileId: 'someone-else', status: 'likely', matchingFaces: 1, profileRevision: 1 }])
    expect(resolveMatch(other, FATHER).status).toBe('not_checked')
  })

  it('treats a queued photo as not checked', () => {
    expect(resolveMatch(record({ peopleStatus: 'pending' }), FATHER).status).toBe('not_checked')
  })

  it('treats an in-flight photo as checking, not as a negative', () => {
    expect(resolveMatch(record(), FATHER, true).status).toBe('checking')
    expect(resolveMatch(scanned([matched('likely')]), FATHER, true).status).toBe('checking')
  })

  it('treats a skipped photo as not checked rather than as a negative', () => {
    // An undecodable file is not evidence that someone is not in it.
    expect(resolveMatch(record({ peopleStatus: 'skipped' }), FATHER).status).toBe('not_checked')
  })
})

describe('a stale record is not an answer', () => {
  it('ignores a record computed before the profile gained a reference', () => {
    const stale = scanned([matched('likely', { profileRevision: FATHER.revision - 1 })])
    expect(resolveMatch(stale, FATHER).status).toBe('not_checked')
  })

  it('ignores a record from a later revision too', () => {
    // A rolled-back profile store is corrupt, not authoritative.
    const future = scanned([matched('likely', { profileRevision: FATHER.revision + 1 })])
    expect(resolveMatch(future, FATHER).status).toBe('not_checked')
  })

  it('does not let one profile read another profile’s record', () => {
    const mother = { id: 'profile-mother', revision: 1 }
    expect(resolveMatch(scanned([matched('likely')]), mother).status).toBe('not_checked')
  })
})

describe('failures are distinguished from negatives', () => {
  it('reports a retryable failure as retryable', () => {
    const failed = record({ peopleStatus: 'failed', peopleFailureCode: 'file_locked' })
    expect(resolveMatch(failed, FATHER).status).toBe('failed_retryable')
  })

  it('reports an unreadable image as permanently failed', () => {
    const failed = record({ peopleStatus: 'failed', peopleFailureCode: 'unsupported_format' })
    expect(resolveMatch(failed, FATHER).status).toBe('failed_permanent')
  })

  it('never reports a failure as a match', () => {
    for (const code of ['file_locked', 'unsupported_format', 'profile_store_unavailable'] as const) {
      const failed = record({ peopleStatus: 'failed', peopleFailureCode: code })
      expect(qualifiesAsMatch(resolveMatch(failed, FATHER))).toBe(false)
    }
  })
})

describe('only likely and possible qualify', () => {
  it('qualifies both tiers and nothing else', () => {
    expect(qualifiesAsMatch({ status: 'likely', matchingFaces: 1 })).toBe(true)
    expect(qualifiesAsMatch({ status: 'possible', matchingFaces: 1 })).toBe(true)
    for (const status of [
      'not_checked',
      'checking',
      'checked_no_reliable_match',
      'failed_retryable',
      'failed_permanent',
      'profile_unavailable'
    ] as const) {
      expect(qualifiesAsMatch({ status, matchingFaces: 0 })).toBe(false)
    }
  })
})

describe('coverage cannot overstate what was checked', () => {
  it('counts checked, unchecked and failed separately', () => {
    const records = [
      scanned([matched('likely')], { relativePath: 'a.jpg' }),
      scanned([matched('checked_no_reliable_match')], { relativePath: 'b.jpg' }),
      record({ relativePath: 'c.jpg' }),
      record({ relativePath: 'd.jpg', peopleStatus: 'failed', peopleFailureCode: 'file_locked' })
    ]
    const coverage = coverageFor(records, FATHER)

    expect(coverage).toEqual({ total: 4, checked: 2, unchecked: 1, failed: 1, matched: 1 })
    expect(coverage.checked + coverage.unchecked + coverage.failed).toBe(coverage.total)
  })

  it('is not complete while anything is unchecked', () => {
    const records = [scanned([matched('likely')], { relativePath: 'a.jpg' }), record({ relativePath: 'b.jpg' })]
    expect(isComplete(coverageFor(records, FATHER))).toBe(false)
  })

  it('is complete when every photo has an answer', () => {
    const records = [
      scanned([matched('likely')], { relativePath: 'a.jpg' }),
      scanned([matched('checked_no_reliable_match')], { relativePath: 'b.jpg' })
    ]
    expect(isComplete(coverageFor(records, FATHER))).toBe(true)
  })

  it('is not complete for an empty library', () => {
    // Nothing checked out of nothing is not a coverage claim worth making.
    expect(isComplete(coverageFor([], FATHER))).toBe(false)
  })

  it('counts in-flight photos as unchecked', () => {
    const records = [scanned([matched('likely')], { relativePath: 'a.jpg' })]
    const inFlight = new Set([records[0]!.imageId])
    const coverage = coverageFor(records, FATHER, inFlight)

    expect(coverage.unchecked).toBe(1)
    expect(coverage.matched).toBe(0)
    expect(isComplete(coverage)).toBe(false)
  })

  it('a whole library that has never been scanned reads as zero checked', () => {
    const records = [record({ relativePath: 'a.jpg' }), record({ relativePath: 'b.jpg' })]
    const coverage = coverageFor(records, FATHER)

    expect(coverage.checked).toBe(0)
    expect(coverage.matched).toBe(0)
    expect(coverage.unchecked).toBe(2)
  })

  it('ignores deleted photos entirely', () => {
    const records = [
      scanned([matched('likely')], { relativePath: 'a.jpg' }),
      record({ relativePath: 'b.jpg', status: 'deleted' })
    ]
    expect(coverageFor(records, FATHER).total).toBe(1)
  })
})
