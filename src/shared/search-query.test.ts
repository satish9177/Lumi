import { describe, expect, it } from 'vitest'
import {
  SearchQueryValidationError,
  classifyFileKind,
  formatModifiedAgo,
  kindSatisfies,
  normalizeSearchQuery,
  tokenizeName,
  tokenizeText
} from './search-query'

describe('tokenizeText', () => {
  it('splits request text on case and separators only', () => {
    expect(tokenizeText('Satish_Resume-2026.PDF')).toEqual(['satish', 'resume', '2026', 'pdf'])
    expect(tokenizeText('my resume')).toEqual(['my', 'resume'])
    // A mixed-case spoken word must survive as one token.
    expect(tokenizeText('ReSuMe')).toEqual(['resume'])
  })
})

describe('tokenizeName', () => {
  it('additionally splits camel case and letter/digit boundaries in filenames', () => {
    expect(tokenizeName('Satish_Resume-2026.pdf')).toEqual(expect.arrayContaining(['satish', 'resume', '2026']))
    expect(tokenizeName('satishResumeFinal')).toEqual(expect.arrayContaining(['resume', 'final']))
    expect(tokenizeName('Resume2026')).toEqual(expect.arrayContaining(['resume', '2026']))
  })
})

describe('normalizeSearchQuery', () => {
  it('drops stopwords and recency words while recording the recency intent', () => {
    const query = normalizeSearchQuery({ queryTerms: 'Find my latest resume' })

    expect(query.terms).toEqual(['resume'])
    expect(query.recency).toBe('latest')
    expect(query.kind).toBe('any')
  })

  it('expands small category synonyms rather than whole utterances', () => {
    expect(normalizeSearchQuery({ queryTerms: 'resume' }).synonyms).toEqual(
      expect.arrayContaining(['cv', 'curriculum', 'vitae'])
    )
    expect(normalizeSearchQuery({ queryTerms: 'cv' }).synonyms).toContain('resume')
    expect(normalizeSearchQuery({ queryTerms: 'certificate' }).synonyms).toContain('cert')
  })

  it('detects the requested file kind from the words the user used', () => {
    expect(normalizeSearchQuery({ queryTerms: 'newest screenshot' }).kind).toBe('screenshot')
    expect(normalizeSearchQuery({ queryTerms: 'screen capture from today' }).kind).toBe('screenshot')
    expect(normalizeSearchQuery({ queryTerms: 'beach photo' }).kind).toBe('photo')
    expect(normalizeSearchQuery({ queryTerms: 'offer letter pdf' }).kind).toBe('document')
  })

  it('lets an explicit field override the detected kind and recency', () => {
    const query = normalizeSearchQuery({ queryTerms: 'resume', kind: 'photo', recency: 'latest' })
    expect(query.kind).toBe('photo')
    expect(query.recency).toBe('latest')
  })

  it('rejects empty, oversized, and closed-schema violations', () => {
    expect(() => normalizeSearchQuery({ queryTerms: '   ' })).toThrow(SearchQueryValidationError)
    expect(() => normalizeSearchQuery({ queryTerms: 'a'.repeat(251) })).toThrow(SearchQueryValidationError)
    expect(() => normalizeSearchQuery({ queryTerms: 'resume', kind: 'video' as never })).toThrow(SearchQueryValidationError)
    expect(() => normalizeSearchQuery({ queryTerms: 'resume', recency: 'soon' as never })).toThrow(SearchQueryValidationError)
  })

  it('keeps a request that is entirely stopwords searchable', () => {
    expect(normalizeSearchQuery({ queryTerms: 'my file' }).terms.length).toBeGreaterThan(0)
  })

  it('produces a frozen query so a retained pending search cannot be mutated', () => {
    const query = normalizeSearchQuery({ queryTerms: 'resume' })
    expect(Object.isFrozen(query)).toBe(true)
  })
})

describe('classifyFileKind', () => {
  it('separates documents, photos, and screenshots', () => {
    expect(classifyFileKind('Resume.pdf', '.pdf', [])).toBe('document')
    expect(classifyFileKind('beach.jpg', '.jpg', ['Pictures'])).toBe('photo')
    expect(classifyFileKind('Screenshot 2026-07-18.png', '.png', ['Pictures'])).toBe('screenshot')
    expect(classifyFileKind('holiday.png', '.png', ['Pictures', 'Screenshots'])).toBe('screenshot')
    expect(classifyFileKind('setup.exe', '.exe', [])).toBe('other')
  })

  it('recognises the common screenshot naming and folder conventions', () => {
    for (const name of [
      'Screenshot 2026-07-18 120000.png',
      'screen shot 5.png',
      'screen_capture_2026.png',
      'Screengrab.png',
      'Capture_001.png'
    ]) {
      expect(classifyFileKind(name, '.png', ['Pictures'])).toBe('screenshot')
    }

    expect(classifyFileKind('holiday.png', '.png', ['Pictures', 'Captures'])).toBe('screenshot')
    expect(classifyFileKind('holiday.png', '.png', ['Pictures', 'Screenshots'])).toBe('screenshot')
    // A plain photo in an ordinary folder stays a photo.
    expect(classifyFileKind('holiday.png', '.png', ['Pictures', 'Goa 2026'])).toBe('photo')
  })

  it('supports the image formats the photo experience promises', () => {
    for (const extension of ['.png', '.jpg', '.jpeg', '.webp', '.bmp']) {
      expect(classifyFileKind(`photo${extension}`, extension, [])).toBe('photo')
    }
  })

  it('treats screenshots as photos for a photo request only', () => {
    expect(kindSatisfies('photo', 'screenshot')).toBe(true)
    expect(kindSatisfies('screenshot', 'photo')).toBe(false)
    expect(kindSatisfies('any', 'other')).toBe(true)
  })
})

describe('formatModifiedAgo', () => {
  it('describes age coarsely without revealing an exact timestamp', () => {
    const now = Date.parse('2026-07-18T12:00:00.000Z')
    expect(formatModifiedAgo(now - 30 * 60_000, now)).toBe('less than an hour ago')
    expect(formatModifiedAgo(now - 3 * 3_600_000, now)).toBe('3 hours ago')
    expect(formatModifiedAgo(now - 24 * 3_600_000, now)).toBe('yesterday')
    expect(formatModifiedAgo(now - 3 * 24 * 3_600_000, now)).toBe('3 days ago')
    expect(formatModifiedAgo(now - 400 * 24 * 3_600_000, now)).toMatch(/year/)
  })
})

describe('visual concepts', () => {
  it('normalizes, deduplicates, bounds, and freezes concepts', () => {
    const query = normalizeSearchQuery({ queryTerms: 'beach photos', kind: 'photo', concepts: [' Beach ', 'beach'] })
    expect(query.concepts).toEqual(['beach'])
    expect(Object.isFrozen(query.concepts)).toBe(true)
    expect(() => normalizeSearchQuery({ queryTerms: 'photo', concepts: ['x'.repeat(65)] })).toThrow(SearchQueryValidationError)
  })
})
