/**
 * Ranking and filtering with a labelled-person constraint.
 *
 * These tests exercise `applyPeopleFilter` and `rankHybridPhotos` directly with
 * fabricated `peopleMatches` rows — no real embeddings, no coordinator, no
 * scan. That is deliberate: the property under test is the ranking logic's
 * *policy* (AND semantics, likely-above-possible, coverage honesty), which
 * should hold regardless of what produced the underlying match records.
 */

import { describe, expect, it } from 'vitest'
import { normalizeSearchQuery } from '../../shared/search-query'
import { applyPeopleFilter, rankHybridPhotos, type PeopleLabelRequirement } from './hybrid-search'
import { computeImageId, type PeopleMatchRecord, type PhotoIndexRecord } from './index-store'

const NOW = 1_800_000_000_000
const DAY = 86_400_000

const FATHER: PeopleLabelRequirement = { id: 'profile-father', revision: 1, label: 'Father' }
const MOTHER: PeopleLabelRequirement = { id: 'profile-mother', revision: 1, label: 'Mother' }

function record(overrides: Partial<PhotoIndexRecord> = {}): PhotoIndexRecord {
  const rootId = overrides.rootId ?? 'root-a'
  const relativePath = overrides.relativePath ?? 'photos/one.jpg'
  return {
    imageId: computeImageId(rootId, relativePath),
    rootId,
    relativePath,
    name: relativePath.split('/').pop()!,
    mtimeMs: NOW - 10 * DAY,
    sizeBytes: 1_024,
    modelVersion: 1,
    status: 'indexed',
    vectorRow: 0,
    attempts: 1,
    updatedAtMs: NOW,
    ...overrides
  }
}

function match(overrides: Partial<PeopleMatchRecord> = {}): PeopleMatchRecord {
  return { profileId: FATHER.id, status: 'likely', matchingFaces: 1, profileRevision: 1, ...overrides }
}

function scanned(matches: PeopleMatchRecord[], overrides: Partial<PhotoIndexRecord> = {}): PhotoIndexRecord {
  return record({ peopleStatus: 'done', peopleMatches: matches, ...overrides })
}

function query(peopleLabels: string[], extra: Partial<Parameters<typeof normalizeSearchQuery>[0]> = {}) {
  return normalizeSearchQuery({ queryTerms: 'photos', peopleLabels, ...extra })
}

describe('applyPeopleFilter: AND semantics', () => {
  it('matches when every requested profile qualifies', () => {
    const outcome = applyPeopleFilter(
      scanned([match({ profileId: FATHER.id, status: 'likely' }), match({ profileId: MOTHER.id, status: 'possible' })]),
      [FATHER, MOTHER]
    )
    expect(outcome.matches).toBe(true)
    expect(outcome.tiers).toEqual([
      { label: 'Father', tier: 'likely' },
      { label: 'Mother', tier: 'possible' }
    ])
  })

  it('excludes a photo with only one of two requested people', () => {
    const outcome = applyPeopleFilter(scanned([match({ profileId: FATHER.id, status: 'likely' })]), [FATHER, MOTHER])
    expect(outcome.matches).toBe(false)
  })

  it('a firm miss on any profile excludes the record without claiming it is unchecked', () => {
    const outcome = applyPeopleFilter(
      scanned([
        match({ profileId: FATHER.id, status: 'likely' }),
        match({ profileId: MOTHER.id, status: 'checked_no_reliable_match', matchingFaces: 0 })
      ]),
      [FATHER, MOTHER]
    )
    expect(outcome.matches).toBe(false)
    expect(outcome.unchecked).toBe(false)
  })

  it('an unresolved profile with no firm miss reads as unchecked, not as a non-match', () => {
    // Father was scanned and matched; Mother has never been checked for this
    // photo. The true answer for Mother might still be yes.
    const outcome = applyPeopleFilter(scanned([match({ profileId: FATHER.id, status: 'likely' })]), [
      FATHER,
      { id: 'profile-unscanned', revision: 1, label: 'Unscanned' }
    ])
    expect(outcome.matches).toBe(false)
    expect(outcome.unchecked).toBe(true)
  })

  it('a single unchecked profile is unchecked, never a non-match', () => {
    const outcome = applyPeopleFilter(record(), [FATHER])
    expect(outcome).toEqual({ matches: false, unchecked: true, tiers: [] })
  })

  it('a stale record (profile revision moved on) is treated as unchecked', () => {
    const outcome = applyPeopleFilter(scanned([match({ profileRevision: 99 })]), [FATHER])
    expect(outcome.unchecked).toBe(true)
  })
})

describe('rankHybridPhotos: filtering and coverage', () => {
  it('returns only photos matching every requested person', () => {
    const both = scanned(
      [match({ profileId: FATHER.id }), match({ profileId: MOTHER.id })],
      { relativePath: 'both.jpg' }
    )
    const fatherOnly = scanned([match({ profileId: FATHER.id })], { relativePath: 'father.jpg' })
    const neither = scanned([], { relativePath: 'neither.jpg' })

    const { ranked } = rankHybridPhotos([both, fatherOnly, neither], new Map(), undefined, query(['Father', 'Mother']), NOW, [
      FATHER,
      MOTHER
    ])

    expect(ranked.map((entry) => entry.record.relativePath)).toEqual(['both.jpg'])
  })

  it('counts an unresolved photo as coverage, not as a search miss', () => {
    const notChecked = record({ relativePath: 'unscanned.jpg' })
    const { ranked, coverage } = rankHybridPhotos([notChecked], new Map(), undefined, query(['Father']), NOW, [FATHER])

    expect(ranked).toHaveLength(0)
    expect(coverage.peopleUnchecked).toBe(1)
  })

  it('does not count a firm non-match as unchecked coverage', () => {
    const noMatch = scanned([match({ status: 'checked_no_reliable_match', matchingFaces: 0 })], {
      relativePath: 'not-father.jpg'
    })
    const { coverage } = rankHybridPhotos([noMatch], new Map(), undefined, query(['Father']), NOW, [FATHER])

    expect(coverage.peopleUnchecked).toBe(0)
  })

  it('ranks a likely match above a possible one regardless of other signals', () => {
    const possible = scanned([match({ status: 'possible' })], {
      relativePath: 'possible.jpg',
      // Newer and lexically earlier, so without the people-tier priority this
      // would sort first on the existing tie-breakers.
      mtimeMs: NOW
    })
    const likely = scanned([match({ status: 'likely' })], {
      relativePath: 'zzz-likely.jpg',
      mtimeMs: NOW - 300 * DAY
    })

    const { ranked } = rankHybridPhotos([possible, likely], new Map(), undefined, query(['Father']), NOW, [FATHER])

    expect(ranked.map((entry) => entry.record.relativePath)).toEqual(['zzz-likely.jpg', 'possible.jpg'])
  })

  it('reports the correct app-authored reason for one likely match', () => {
    const photo = scanned([match({ status: 'likely' })])
    const { ranked } = rankHybridPhotos([photo], new Map(), undefined, query(['Father']), NOW, [FATHER])

    expect(ranked[0]?.reason).toBe('Likely match for Father')
    expect(ranked[0]?.tier).toBe('people_likely')
  })

  it('reports a possible-match reason distinctly from likely', () => {
    const photo = scanned([match({ status: 'possible' })])
    const { ranked } = rankHybridPhotos([photo], new Map(), undefined, query(['Father']), NOW, [FATHER])

    expect(ranked[0]?.reason).toBe('Possible match for Father')
    expect(ranked[0]?.tier).toBe('people_possible')
  })

  it('reports a mixed-confidence reason for two people', () => {
    const photo = scanned([
      match({ profileId: FATHER.id, status: 'likely' }),
      match({ profileId: MOTHER.id, status: 'possible' })
    ])
    const { ranked } = rankHybridPhotos([photo], new Map(), undefined, query(['Father', 'Mother']), NOW, [FATHER, MOTHER])

    expect(ranked[0]?.reason).toBe('Likely match for Father, possible match for Mother')
  })

  it('never says "confirmed", "definitely", or "this is" anywhere in a reason', () => {
    const photo = scanned([match({ status: 'likely' })])
    const { ranked } = rankHybridPhotos([photo], new Map(), undefined, query(['Father']), NOW, [FATHER])

    const reason = ranked[0]!.reason.toLowerCase()
    expect(reason).not.toContain('confirmed')
    expect(reason).not.toContain('definitely')
    expect(reason).not.toContain('certain')
    expect(reason).not.toContain('this is')
  })
})

describe('filenames and OCR text can never stand in for a biometric match', () => {
  it('a filename containing the label does not qualify without a scanned match', () => {
    const namedButUnscanned = record({ relativePath: 'father-birthday-party.jpg' })
    const { ranked } = rankHybridPhotos(
      [namedButUnscanned],
      new Map(),
      undefined,
      query(['Father']),
      NOW,
      [FATHER]
    )
    expect(ranked).toHaveLength(0)
  })

  it('OCR text containing the label does not qualify without a scanned match', () => {
    const withOcrLabel = record({
      ocrStatus: 'done',
      ocrVersion: 1,
      ocrText: 'happy birthday father',
      ocrTokens: ['happy', 'birthday', 'father']
    })
    const { ranked } = rankHybridPhotos([withOcrLabel], new Map(), undefined, query(['Father']), NOW, [FATHER])
    expect(ranked).toHaveLength(0)
  })

  it('a firm biometric miss excludes the photo even when the filename matches the label', () => {
    const namedAndScanned = scanned([match({ status: 'checked_no_reliable_match', matchingFaces: 0 })], {
      relativePath: 'father-and-friends.jpg'
    })
    const { ranked } = rankHybridPhotos([namedAndScanned], new Map(), undefined, query(['Father']), NOW, [FATHER])
    expect(ranked).toHaveLength(0)
  })
})

describe('semantic relevance ranks qualifying matches but cannot manufacture one', () => {
  it('a weak visual match does not admit a photo the person filter rejected', () => {
    const rejected = scanned([], { relativePath: 'rejected.jpg' })
    const vectors = new Map([[rejected.imageId, new Float32Array(512).fill(0.9)]])
    const queryVector = new Float32Array(512).fill(0.9)

    const { ranked } = rankHybridPhotos(
      [rejected],
      vectors,
      queryVector,
      query(['Father'], { concepts: ['beach'] }),
      NOW,
      [FATHER]
    )
    expect(ranked).toHaveLength(0)
  })

  it('does not require a visual match to admit a qualifying person match', () => {
    // A concept was given, but this record has no vector at all — an ordinary
    // concept-only search would exclude it. A qualifying person match must not
    // depend on the visual signal being present.
    const photo = scanned([match({ status: 'likely' })])
    const { ranked } = rankHybridPhotos(
      [photo],
      new Map(),
      new Float32Array(512).fill(0.9),
      query(['Father'], { concepts: ['beach'] }),
      NOW,
      [FATHER]
    )
    expect(ranked).toHaveLength(1)
  })
})
