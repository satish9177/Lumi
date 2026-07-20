/**
 * Matching a query against locally extracted image text.
 *
 * The ladder exists to keep Lumi honest. OCR on a photographed document is
 * routinely imperfect — the feasibility probe for this phase read `INTERVIEW`
 * as `IHNTERWIEM` — so a useful search has to tolerate error. But tolerating
 * error means a match can be wrong, and a wrong match presented confidently is
 * worse than no match. Each rung therefore carries its own strength, and the
 * caller may only make the claim that rung supports:
 *
 *   exact   — the phrase is present, in order. "Contains the text you searched for."
 *   all     — every significant word is present, not necessarily together.
 *   fuzzy   — the words are present allowing for bounded character error.
 *   none    — no OCR signal. Never dressed up as a match.
 *
 * A fuzzy hit is never reported with the exact-match wording. See
 * `ocr-match.test.ts`, which pins that separation.
 */

import { isNumericToken } from '../../shared/ocr-text'

export const OCR_MATCH_STRENGTHS = ['exact', 'all_tokens', 'fuzzy', 'none'] as const
export type OcrMatchStrength = (typeof OCR_MATCH_STRENGTHS)[number]

export interface OcrMatch {
  strength: OcrMatchStrength
  /** 0..1, for fusion. Deterministic given the same inputs. */
  score: number
  /** How many query tokens were satisfied, at any rung. */
  matchedTokens: number
  totalTokens: number
}

export const NO_OCR_MATCH: OcrMatch = Object.freeze({
  strength: 'none',
  score: 0,
  matchedTokens: 0,
  totalTokens: 0
})

/**
 * Fuzzy matching is deliberately narrow. Short words are not fuzzy-matched at
 * all, because at three characters an edit distance of one relates most words
 * to most other words — `cat`/`car`/`can` — and the result is confident
 * nonsense. Longer words carry enough signal to absorb an edit or two.
 */
const FUZZY_MIN_LENGTH = 5
const FUZZY_DISTANCE_1_MAX_LENGTH = 7

/** Bounds the comparison work a single query may provoke across the library. */
const MAX_FUZZY_CANDIDATES = 600

export function fuzzyBudgetFor(token: string): number {
  if (token.length < FUZZY_MIN_LENGTH) {
    return 0
  }
  return token.length <= FUZZY_DISTANCE_1_MAX_LENGTH ? 1 : 2
}

/**
 * A digit run must match exactly. An edit-distance-1 match on `1234` would
 * accept `1284`, and quietly returning someone else's reference number as
 * though it were the one they asked for is not a tolerable failure here.
 */
export function matchesToken(queryToken: string, candidate: string): 'exact' | 'fuzzy' | 'none' {
  if (queryToken === candidate) {
    return 'exact'
  }
  if (isNumericToken(queryToken) || isNumericToken(candidate)) {
    return 'none'
  }
  const budget = fuzzyBudgetFor(queryToken)
  if (budget === 0) {
    return 'none'
  }
  return boundedEditDistance(queryToken, candidate, budget) <= budget ? 'fuzzy' : 'none'
}

/**
 * Levenshtein distance, abandoned as soon as it provably exceeds `budget`.
 * Returning `budget + 1` for anything worse keeps the caller from paying for
 * precision it will discard, and bounds the work per comparison.
 */
export function boundedEditDistance(left: string, right: string, budget: number): number {
  if (Math.abs(left.length - right.length) > budget) {
    return budget + 1
  }
  if (left === right) {
    return 0
  }

  let previous = Array.from({ length: right.length + 1 }, (_, index) => index)
  for (let i = 1; i <= left.length; i += 1) {
    const current = new Array<number>(right.length + 1)
    current[0] = i
    let best = current[0]
    for (let j = 1; j <= right.length; j += 1) {
      const substitution = previous[j - 1]! + (left[i - 1] === right[j - 1] ? 0 : 1)
      current[j] = Math.min(substitution, previous[j]! + 1, current[j - 1]! + 1)
      if (current[j]! < best) {
        best = current[j]!
      }
    }
    if (best > budget) {
      // Every cell in this row already exceeds the budget, and distance is
      // non-decreasing down the rows, so no later row can come back under it.
      return budget + 1
    }
    previous = current
  }
  return previous[right.length]!
}

/** True when `queryTokens` appear consecutively, in order, inside `tokens`. */
export function containsPhrase(tokens: readonly string[], queryTokens: readonly string[]): boolean {
  if (queryTokens.length === 0 || queryTokens.length > tokens.length) {
    return false
  }
  for (let start = 0; start <= tokens.length - queryTokens.length; start += 1) {
    let matched = true
    for (let offset = 0; offset < queryTokens.length; offset += 1) {
      if (tokens[start + offset] !== queryTokens[offset]) {
        matched = false
        break
      }
    }
    if (matched) {
      return true
    }
  }
  return false
}

/**
 * Walks the ladder and returns the strongest rung the stored text supports.
 *
 * `storedTokens` is the indexed image's text. `queryTokens` is the normalized
 * `contains_text` request. Both have already passed through `ocr-text.ts`, so
 * this function does no normalization of its own — doing it in two places is
 * how the two drift apart.
 */
export function matchOcr(storedTokens: readonly string[] | undefined, queryTokens: readonly string[]): OcrMatch {
  if (!storedTokens || storedTokens.length === 0 || queryTokens.length === 0) {
    return NO_OCR_MATCH
  }

  const total = queryTokens.length

  if (containsPhrase(storedTokens, queryTokens)) {
    return { strength: 'exact', score: 1, matchedTokens: total, totalTokens: total }
  }

  const haystack = new Set(storedTokens)
  let exactHits = 0
  let fuzzyHits = 0
  let comparisons = 0

  for (const queryToken of queryTokens) {
    if (haystack.has(queryToken)) {
      exactHits += 1
      continue
    }

    // Only spend fuzzy comparisons on tokens that could benefit, and stop once
    // the per-query budget is gone, so a long query over a large library cannot
    // turn into unbounded work.
    if (fuzzyBudgetFor(queryToken) === 0) {
      continue
    }
    let matched = false
    for (const candidate of storedTokens) {
      if (comparisons >= MAX_FUZZY_CANDIDATES) {
        break
      }
      comparisons += 1
      if (matchesToken(queryToken, candidate) === 'fuzzy') {
        matched = true
        break
      }
    }
    if (matched) {
      fuzzyHits += 1
    }
  }

  if (exactHits === total) {
    // Every word is present, but not as a contiguous phrase.
    return { strength: 'all_tokens', score: 0.8, matchedTokens: total, totalTokens: total }
  }

  const satisfied = exactHits + fuzzyHits
  if (satisfied === total) {
    return { strength: 'fuzzy', score: 0.55, matchedTokens: total, totalTokens: total }
  }

  // A partial match is not a match. Reporting "contains your text" because two
  // words of three were found is the dishonesty this ladder exists to prevent.
  return { strength: 'none', score: 0, matchedTokens: satisfied, totalTokens: total }
}
