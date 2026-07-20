/**
 * Deterministic normalization of locally extracted image text.
 *
 * **This module handles untrusted data.** Text recovered from a photograph is
 * attacker-influenced in the ordinary case: anyone who can get an image onto
 * the user's disk chooses what it says. It is therefore treated exactly like a
 * filename — something to match against, never something to act on. Nothing
 * here interprets, executes, or forwards the text; it only reduces it to a
 * comparable form.
 *
 * Determinism matters because the same photo must normalize identically across
 * runs and machines, or an index built yesterday stops matching a query typed
 * today. Every step below is total and order-independent of locale beyond the
 * explicit `en-US` casing.
 *
 * The text never reaches a log, the Realtime session, a model message, a
 * confirmation string, or a Telegram caption. See `ocr-privacy.test.ts`.
 */

/**
 * Hard ceilings on what one image may contribute to the index. Defined here,
 * rather than beside the store, because the query path applies the identical
 * bounds and the two must never disagree.
 */
export const MAX_OCR_TEXT_CHARS = 4_000
export const MAX_OCR_TOKENS = 400
export const MAX_OCR_TOKEN_LENGTH = 32

/**
 * Punctuation kept through normalization because it carries meaning for the
 * things people actually search for in documents: dates (`12/03/2024`,
 * `2024-03-12`), reference numbers (`ABC-123`), and times (`09:30`). Everything
 * else becomes a separator.
 */
const KEPT_PUNCTUATION = "-/:.#'"

/** Tokens shorter than this are noise unless they are numeric. */
const MIN_TOKEN_LENGTH = 2

/**
 * Reduces raw engine output to a stable comparable string.
 *
 * NFKC first, so a full-width or ligature-bearing rendering of the same
 * characters collapses onto the ASCII the query will be typed in. Control
 * characters are dropped outright rather than mapped to spaces, so a text
 * carrying escape sequences cannot survive into anything downstream.
 */
export function normalizeOcrText(raw: unknown): string {
  if (typeof raw !== 'string' || raw.length === 0) {
    return ''
  }

  // Bound before the expensive passes: a pathological engine result must not be
  // able to make normalization itself the slow step.
  const clipped = raw.slice(0, MAX_OCR_TEXT_CHARS * 4)

  let normalized: string
  try {
    normalized = clipped.normalize('NFKC')
  } catch {
    // A lone surrogate can make normalize throw; the raw form is still usable.
    normalized = clipped
  }

  const out: string[] = []
  let pendingSpace = false
  for (const character of normalized.toLocaleLowerCase('en-US')) {
    const code = character.codePointAt(0)!
    const isAlphanumeric =
      (code >= 0x61 && code <= 0x7a) || // a-z
      (code >= 0x30 && code <= 0x39) // 0-9

    if (isAlphanumeric || KEPT_PUNCTUATION.includes(character)) {
      if (pendingSpace && out.length > 0) {
        out.push(' ')
      }
      pendingSpace = false
      out.push(character)
      continue
    }

    // Everything else — whitespace, control characters, symbols, and any
    // non-Latin script this English-first phase cannot compare — becomes a
    // single separator rather than being preserved or deleted silently.
    pendingSpace = true
  }

  return out.join('').slice(0, MAX_OCR_TEXT_CHARS).trim()
}

/**
 * The compact searchable form. Punctuation that survived normalization is a
 * separator here, so `2024-03-12` yields `2024`, `03`, and `12` as well as
 * being findable as a phrase.
 */
export function ocrTokensOf(normalized: string): string[] {
  if (typeof normalized !== 'string' || normalized.length === 0) {
    return []
  }

  const tokens: string[] = []
  for (const candidate of normalized.split(/[^a-z0-9]+/)) {
    if (candidate.length === 0) {
      continue
    }
    // A single digit is noise, but a multi-digit run is exactly what an ID or
    // date query is made of, so numeric tokens are kept at the same floor.
    if (candidate.length < MIN_TOKEN_LENGTH) {
      continue
    }
    tokens.push(candidate.slice(0, MAX_OCR_TOKEN_LENGTH))
    if (tokens.length >= MAX_OCR_TOKENS) {
      break
    }
  }
  return tokens
}

/** Normalization and tokenization together, as the indexer stores them. */
export function prepareOcrText(raw: unknown): { text: string; tokens: string[] } {
  const text = normalizeOcrText(raw)
  return { text, tokens: ocrTokensOf(text) }
}

export function isNumericToken(token: string): boolean {
  return /^[0-9]+$/.test(token)
}
