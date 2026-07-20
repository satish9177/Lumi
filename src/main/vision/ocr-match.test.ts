import { describe, expect, it } from 'vitest'
import {
  boundedEditDistance,
  containsPhrase,
  fuzzyBudgetFor,
  matchesToken,
  matchOcr,
  NO_OCR_MATCH
} from './ocr-match'
import { ocrTokensOf, prepareOcrText } from '../../shared/ocr-text'

/** Mirrors how the indexer and the query path both reach this function. */
function tokens(text: string): string[] {
  return prepareOcrText(text).tokens
}

const DEGREE_CERTIFICATE = tokens(
  'UNIVERSITY OF EXAMPLE\nDegree Certificate\nAwarded to Satish on 12/03/2024\nReference ABC-4471'
)

describe('rung 1: exact normalized phrase', () => {
  it('matches a phrase present in order', () => {
    const match = matchOcr(DEGREE_CERTIFICATE, tokens('degree certificate'))
    expect(match.strength).toBe('exact')
    expect(match.score).toBe(1)
  })

  it('is case-insensitive, because both sides are normalized first', () => {
    expect(matchOcr(DEGREE_CERTIFICATE, tokens('DEGREE CERTIFICATE')).strength).toBe('exact')
  })

  it('matches a single word phrase', () => {
    expect(matchOcr(tokens('this is an interview invitation'), tokens('interview')).strength).toBe('exact')
  })

  it('does not treat a reordered phrase as exact', () => {
    const match = matchOcr(DEGREE_CERTIFICATE, tokens('certificate degree'))
    expect(match.strength).not.toBe('exact')
    expect(match.strength).toBe('all_tokens')
  })
})

describe('containsPhrase', () => {
  it('requires the words to be consecutive and in order', () => {
    expect(containsPhrase(['a', 'b', 'c'], ['a', 'b'])).toBe(true)
    expect(containsPhrase(['a', 'b', 'c'], ['b', 'c'])).toBe(true)
    expect(containsPhrase(['a', 'b', 'c'], ['a', 'c'])).toBe(false)
    expect(containsPhrase(['a', 'b', 'c'], ['c', 'b'])).toBe(false)
  })

  it('handles the degenerate cases without throwing', () => {
    expect(containsPhrase([], ['a'])).toBe(false)
    expect(containsPhrase(['a'], [])).toBe(false)
    expect(containsPhrase(['a'], ['a', 'b'])).toBe(false)
  })
})

describe('rung 2: all significant tokens present', () => {
  it('matches words that appear apart from each other', () => {
    const match = matchOcr(DEGREE_CERTIFICATE, tokens('university satish'))
    expect(match.strength).toBe('all_tokens')
    expect(match.matchedTokens).toBe(2)
  })

  it('scores below an exact phrase, so ranking prefers the stronger evidence', () => {
    const exact = matchOcr(DEGREE_CERTIFICATE, tokens('degree certificate'))
    const all = matchOcr(DEGREE_CERTIFICATE, tokens('certificate university'))
    expect(all.score).toBeLessThan(exact.score)
  })
})

describe('rung 3: bounded fuzzy matching for real OCR error', () => {
  it.each([
    ['a dropped character', 'certifcate', 'certificate'],
    ['a doubled character', 'interviiew', 'interview'],
    ['the classic l/I confusion', 'lnterview', 'interview'],
    ['rn read as m', 'govemment', 'government']
  ])('absorbs %s', (_label, stored, query) => {
    expect(matchOcr(ocrTokensOf(stored), tokens(query)).strength).toBe('fuzzy')
  })

  it('needs no fuzzy budget for trailing punctuation, which normalization already removed', () => {
    expect(matchOcr(ocrTokensOf('invoice.'), tokens('invoice')).strength).toBe('exact')
  })

  it('stops short of matching a heavily corrupted word', () => {
    // `ihnterwiem` for `interview` is three edits. The local engine did produce
    // exactly that, but only on a synthesized bitmap-font probe image rather
    // than on real document text. Widening the budget to three would relate far
    // too many ordinary words to each other, so this stays a deliberate miss:
    // the ladder prefers finding nothing to confidently finding the wrong photo.
    expect(boundedEditDistance('ihnterwiem', 'interview', 3)).toBe(3)
    expect(matchOcr(ocrTokensOf('ihnterwiem'), tokens('interview')).strength).toBe('none')
  })

  it('scores below an all-token match, because it is weaker evidence', () => {
    const fuzzy = matchOcr(ocrTokensOf('certifikate'), tokens('certificate'))
    const all = matchOcr(ocrTokensOf('certificate'), tokens('certificate'))
    expect(fuzzy.strength).toBe('fuzzy')
    expect(fuzzy.score).toBeLessThan(all.score)
  })

  it('refuses to fuzzy-match short words, where an edit relates everything', () => {
    expect(fuzzyBudgetFor('cat')).toBe(0)
    expect(fuzzyBudgetFor('card')).toBe(0)
    expect(matchOcr(ocrTokensOf('car'), tokens('cat')).strength).toBe('none')
  })

  it('allows one edit for medium words and two for long ones', () => {
    expect(fuzzyBudgetFor('photo')).toBe(1)
    expect(fuzzyBudgetFor('invoice')).toBe(1)
    expect(fuzzyBudgetFor('certificate')).toBe(2)
  })

  it('does not match a word that is merely similar in theme', () => {
    expect(matchOcr(ocrTokensOf('passport'), tokens('password')).strength).toBe('fuzzy')
    expect(matchOcr(ocrTokensOf('elephant'), tokens('certificate')).strength).toBe('none')
  })
})

describe('numeric tokens must match exactly', () => {
  it('matches an identifier that is present', () => {
    expect(matchOcr(DEGREE_CERTIFICATE, tokens('4471')).strength).toBe('exact')
  })

  it('never fuzzy-matches a digit run onto a different one', () => {
    // Returning someone else's reference number as though it were the one
    // asked for is not a tolerable failure, so this rung is closed for digits.
    expect(matchesToken('1234', '1284')).toBe('none')
    expect(matchesToken('1234', '12345')).toBe('none')
    expect(matchOcr(ocrTokensOf('reference 1284'), tokens('1234')).strength).toBe('none')
  })

  it('does not match a digit run against a word', () => {
    expect(matchesToken('1234', 'abcd')).toBe('none')
    expect(matchesToken('lll1', '1111')).toBe('none')
  })

  it('finds a number inside a longer document', () => {
    const stored = tokens('Aadhaar 1234 5678 9012 issued 2019')
    expect(matchOcr(stored, tokens('1234')).strength).toBe('exact')
    expect(matchOcr(stored, tokens('1234 5678')).strength).toBe('exact')
  })
})

describe('rung 4: no signal is reported as no signal', () => {
  it('returns none for an image with no stored text', () => {
    expect(matchOcr(undefined, tokens('anything'))).toEqual(NO_OCR_MATCH)
    expect(matchOcr([], tokens('anything'))).toEqual(NO_OCR_MATCH)
  })

  it('returns none for an empty query', () => {
    expect(matchOcr(DEGREE_CERTIFICATE, [])).toEqual(NO_OCR_MATCH)
  })

  it('refuses to call a partial match a match', () => {
    // Two of three words found is not "contains the text you searched for".
    const match = matchOcr(DEGREE_CERTIFICATE, tokens('degree certificate missingword'))
    expect(match.strength).toBe('none')
    expect(match.score).toBe(0)
    expect(match.matchedTokens).toBe(2)
    expect(match.totalTokens).toBe(3)
  })

  it('does not match unrelated text', () => {
    expect(matchOcr(tokens('a photo of a beach at sunset'), tokens('degree certificate')).strength).toBe('none')
  })
})

describe('a weak match is never presented as an exact one', () => {
  it('keeps every rung strictly ordered by score', () => {
    const exact = matchOcr(ocrTokensOf('degree certificate'), tokens('degree certificate'))
    const all = matchOcr(ocrTokensOf('certificate of degree'), tokens('degree certificate'))
    const fuzzy = matchOcr(ocrTokensOf('degrees certifikate'), tokens('degree certificate'))
    const none = matchOcr(ocrTokensOf('holiday beach'), tokens('degree certificate'))

    expect(exact.score).toBeGreaterThan(all.score)
    expect(all.score).toBeGreaterThan(fuzzy.score)
    expect(fuzzy.score).toBeGreaterThan(none.score)
    expect([exact.strength, all.strength, fuzzy.strength, none.strength]).toEqual([
      'exact', 'all_tokens', 'fuzzy', 'none'
    ])
  })
})

describe('bounded edit distance', () => {
  it('computes small distances exactly', () => {
    expect(boundedEditDistance('kitten', 'kitten', 3)).toBe(0)
    expect(boundedEditDistance('kitten', 'sitten', 3)).toBe(1)
    expect(boundedEditDistance('kitten', 'sitting', 3)).toBe(3)
  })

  it('abandons early rather than computing a distance it would discard', () => {
    expect(boundedEditDistance('kitten', 'sitting', 1)).toBeGreaterThan(1)
    expect(boundedEditDistance('short', 'a much longer string', 2)).toBeGreaterThan(2)
  })

  it('is symmetric', () => {
    expect(boundedEditDistance('abcd', 'abxd', 2)).toBe(boundedEditDistance('abxd', 'abcd', 2))
  })

  it('stays bounded on adversarially long tokens', () => {
    const started = Date.now()
    for (let i = 0; i < 500; i += 1) {
      boundedEditDistance('x'.repeat(32), 'y'.repeat(32), 2)
    }
    expect(Date.now() - started).toBeLessThan(1_000)
  })
})

describe('matching bounds its work on a large document', () => {
  it('stays fast against a full page of text', () => {
    const large = ocrTokensOf(Array.from({ length: 400 }, (_, i) => `word${i}`).join(' '))
    const started = Date.now()
    for (let i = 0; i < 50; i += 1) {
      matchOcr(large, tokens('nonexistent phrase entirely'))
    }
    expect(Date.now() - started).toBeLessThan(2_000)
  })
})
