import { describe, expect, it } from 'vitest'
import {
  MAX_CONTAINS_TEXT_LENGTH,
  MAX_PEOPLE_COUNT,
  normalizeContainsText,
  normalizePeopleFilter,
  normalizeSearchQuery,
  SearchQueryValidationError
} from './search-query'

describe('contains_text validation', () => {
  it('normalizes through the same path the indexer uses', () => {
    expect(normalizeContainsText('  Degree   CERTIFICATE ')).toBe('degree certificate')
  })

  it('treats an absent or blank value as no text signal', () => {
    expect(normalizeContainsText(undefined)).toBe('')
    expect(normalizeContainsText(null)).toBe('')
    expect(normalizeContainsText('   ')).toBe('')
  })

  it('accepts text at the length limit and rejects text beyond it', () => {
    // A real phrase rather than a run of one letter: a long single-character
    // string is also hex, and would trip the identifier guard for that reason
    // instead of the length one.
    const atLimit = 'degree certificate awarded to the named student on this date '
      .repeat(3)
      .slice(0, MAX_CONTAINS_TEXT_LENGTH)
    expect(atLimit).toHaveLength(MAX_CONTAINS_TEXT_LENGTH)
    expect(normalizeContainsText(atLimit).length).toBeGreaterThan(0)
    expect(() => normalizeContainsText(`${atLimit}x`)).toThrow(SearchQueryValidationError)
  })

  it.each([
    ['a Windows path', 'C:\\Users\\satis\\taxes.pdf'],
    ['a POSIX path', '/etc/passwd'],
    ['a relative path', '../../secrets'],
    ['a long hex identifier', 'a'.repeat(24)],
    ['a JSON fragment', '{"role":"system"}'],
    ['an array fragment', '[1,2,3]']
  ])('rejects %s', (_label, candidate) => {
    expect(() => normalizeContainsText(candidate)).toThrow(SearchQueryValidationError)
  })

  it('rejects a non-string rather than coercing it', () => {
    for (const value of [42, true, {}, []]) {
      expect(() => normalizeContainsText(value)).toThrow(SearchQueryValidationError)
    }
  })

  it('rejects text with no searchable characters left after normalization', () => {
    expect(() => normalizeContainsText('!!! ***')).toThrow(SearchQueryValidationError)
  })

  it('keeps digits, which is the point of an ID query', () => {
    expect(normalizeContainsText('1234')).toBe('1234')
  })
})

describe('people filter validation', () => {
  it('accepts each supported operator', () => {
    expect(normalizePeopleFilter({ op: 'eq', n: 2 })).toEqual({ op: 'eq', n: 2 })
    expect(normalizePeopleFilter({ op: 'gte', n: 3 })).toEqual({ op: 'gte', n: 3 })
    expect(normalizePeopleFilter({ op: 'none' })).toEqual({ op: 'none' })
  })

  it('treats an absent filter as no constraint', () => {
    expect(normalizePeopleFilter(undefined)).toBeUndefined()
    expect(normalizePeopleFilter(null)).toBeUndefined()
  })

  it('rejects an unknown key rather than ignoring it', () => {
    // Silently dropping an unrecognized field is how an unvalidated option
    // later becomes an honoured one.
    expect(() => normalizePeopleFilter({ op: 'eq', n: 1, name: 'Father' })).toThrow(
      SearchQueryValidationError
    )
    expect(() => normalizePeopleFilter({ op: 'eq', n: 1, identity: 'x' })).toThrow(
      SearchQueryValidationError
    )
  })

  it('rejects an unknown operator', () => {
    expect(() => normalizePeopleFilter({ op: 'lte', n: 2 })).toThrow(SearchQueryValidationError)
    expect(() => normalizePeopleFilter({ op: 'is', n: 2 })).toThrow(SearchQueryValidationError)
  })

  it('refuses a count on the none operator, so "none, 3" cannot be expressed', () => {
    expect(() => normalizePeopleFilter({ op: 'none', n: 3 })).toThrow(SearchQueryValidationError)
  })

  it.each([
    ['a missing count', { op: 'eq' }],
    ['a negative count', { op: 'eq', n: -1 }],
    ['a fractional count', { op: 'eq', n: 1.5 }],
    ['a count beyond the practical range', { op: 'gte', n: MAX_PEOPLE_COUNT + 1 }],
    ['a string count', { op: 'eq', n: '2' }],
    ['a non-finite count', { op: 'eq', n: Number.NaN }]
  ])('rejects %s', (_label, candidate) => {
    expect(() => normalizePeopleFilter(candidate)).toThrow(SearchQueryValidationError)
  })

  it('rejects a non-object', () => {
    for (const value of ['eq', 2, [], true]) {
      expect(() => normalizePeopleFilter(value)).toThrow(SearchQueryValidationError)
    }
  })

  it('accepts the boundary counts', () => {
    expect(normalizePeopleFilter({ op: 'eq', n: 0 })).toEqual({ op: 'eq', n: 0 })
    expect(normalizePeopleFilter({ op: 'gte', n: MAX_PEOPLE_COUNT })).toEqual({
      op: 'gte',
      n: MAX_PEOPLE_COUNT
    })
  })
})

describe('the whole query contract', () => {
  it('normalizes the worked example for a screenshot text search', () => {
    const query = normalizeSearchQuery({
      queryTerms: 'degree certificate',
      kind: 'screenshot',
      containsText: 'degree certificate'
    })
    expect(query.kind).toBe('screenshot')
    expect(query.containsText).toBe('degree certificate')
    expect(query.containsTextTokens).toEqual(['degree', 'certificate'])
    expect(query.people).toBeUndefined()
  })

  it('normalizes the worked example for recent birthday photos with two people', () => {
    const query = normalizeSearchQuery({
      queryTerms: 'birthday',
      kind: 'photo',
      concepts: ['birthday'],
      people: { op: 'eq', n: 2 },
      recency: 'latest'
    })
    expect(query.kind).toBe('photo')
    expect(query.concepts).toEqual(['birthday'])
    expect(query.people).toEqual({ op: 'eq', n: 2 })
    expect(query.recency).toBe('latest')
    expect(query.containsTextTokens).toEqual([])
  })

  it('leaves an existing Phase-1 query completely unchanged in behaviour', () => {
    const query = normalizeSearchQuery({ queryTerms: 'my latest resume' })
    expect(query.terms).toContain('resume')
    expect(query.recency).toBe('latest')
    // The new fields are inert rather than absent, so ranking needs no branch.
    expect(query.containsText).toBe('')
    expect(query.containsTextTokens).toEqual([])
    expect(query.people).toBeUndefined()
  })

  it('rejects the whole query when a Phase-2 field is invalid', () => {
    expect(() =>
      normalizeSearchQuery({ queryTerms: 'photos', containsText: 'C:\\Users\\satis' })
    ).toThrow(SearchQueryValidationError)
    expect(() =>
      normalizeSearchQuery({ queryTerms: 'photos', people: { op: 'eq', n: 99 } })
    ).toThrow(SearchQueryValidationError)
  })

  it('stays frozen, so no later stage can mutate a validated query', () => {
    const query = normalizeSearchQuery({ queryTerms: 'photos', containsText: 'invoice' })
    expect(Object.isFrozen(query)).toBe(true)
    expect(() => {
      ;(query as { containsText: string }).containsText = 'something else'
    }).toThrow()
  })
})
