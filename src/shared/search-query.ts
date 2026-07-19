// One deterministic normalization path for stored-file search. Live Realtime,
// mock voice, typed panel search, and transcribed speech all pass through here,
// so ranking sees the same tokens regardless of how the request arrived.

export const SEARCH_KINDS = ['document', 'photo', 'screenshot', 'any'] as const
export type SearchKind = (typeof SEARCH_KINDS)[number]

export const SEARCH_RECENCIES = ['latest', 'any'] as const
export type SearchRecency = (typeof SEARCH_RECENCIES)[number]

export interface SearchQueryInput {
  queryTerms: string
  kind?: SearchKind
  recency?: SearchRecency
  concepts?: string[]
}

export interface NormalizedSearchQuery {
  /** The normalized request text, retained for display and intent matching. */
  raw: string
  /** Token-joined form used for whole-phrase filename comparison. */
  phrase: string
  /** Meaningful query tokens after stopword and recency removal. */
  terms: string[]
  /** Category synonyms of the query tokens that are not already terms. */
  synonyms: string[]
  kind: SearchKind
  recency: SearchRecency
  /** Short user-authored visual concepts. Empty means filename/date search. */
  concepts: string[]
}

const MAX_QUERY_LENGTH = 250
const MAX_TERMS = 8
export const MAX_SEARCH_CONCEPTS = 3
export const MAX_SEARCH_CONCEPT_LENGTH = 64

const STOPWORDS = new Set([
  'a', 'an', 'and', 'any', 'are', 'at', 'can', 'do', 'find', 'fetch', 'for', 'from', 'get', 'give',
  'have', 'i', 'in', 'is', 'it', 'locate', 'look', 'me', 'mine', 'my', 'need', 'of', 'on', 'one',
  'open', 'our', 'please', 'saved', 'search', 'show', 'some', 'stored', 'that', 'the', 'their',
  'these', 'this', 'those', 'to', 'up', 'want', 'was', 'where', 'with', 'you', 'your',
  'file', 'files', 'folder', 'folders'
])

const RECENCY_WORDS = new Set(['latest', 'newest', 'recent', 'recently', 'last', 'most', 'current'])

// Small category vocabularies, deliberately not a dictionary of whole utterances.
const SYNONYM_GROUPS: Record<string, readonly string[]> = {
  resume: ['resume', 'resumes', 'cv', 'cvs', 'curriculum', 'vitae'],
  certificate: ['certificate', 'certificates', 'cert', 'certs', 'certification', 'certifications'],
  photo: ['photo', 'photos', 'image', 'images', 'picture', 'pictures', 'pic', 'pics', 'img'],
  screenshot: ['screenshot', 'screenshots', 'screengrab', 'screencapture', 'snip'],
  invoice: ['invoice', 'invoices', 'bill', 'bills', 'receipt', 'receipts']
}

const SYNONYM_LOOKUP = new Map<string, readonly string[]>()
for (const members of Object.values(SYNONYM_GROUPS)) {
  for (const member of members) {
    SYNONYM_LOOKUP.set(member, members)
  }
}

const DOCUMENT_EXTENSIONS = new Set([
  '.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.md', '.pages',
  '.ppt', '.pptx', '.odp', '.xls', '.xlsx', '.ods', '.csv'
])

const PHOTO_EXTENSIONS = new Set([
  '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif', '.tiff', '.tif', '.avif'
])

const DOCUMENT_WORDS = new Set(['document', 'documents', 'doc', 'docs', 'pdf', 'pdfs', 'docx', 'txt', 'letter', 'letters'])
const PHOTO_WORDS = new Set(SYNONYM_GROUPS.photo.concat(['jpg', 'jpeg', 'png', 'heic']))
const SCREENSHOT_WORDS = new Set(SYNONYM_GROUPS.screenshot)
const SCREENSHOT_SECOND_WORDS = new Set(['shot', 'shots', 'capture', 'captures', 'grab', 'grabs'])

/** Image formats Lumi will preview and, when explicitly chosen, analyse. */
export function isImageExtension(extension: string): boolean {
  return PHOTO_EXTENSIONS.has(extension.toLocaleLowerCase('en-US'))
}

export class SearchQueryValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SearchQueryValidationError'
  }
}

/**
 * Splits request text into comparable tokens on case and separators only.
 * Spoken and typed requests use ordinary words, so `ReSuMe` must stay one
 * token rather than being split apart like a camel-case filename.
 */
export function tokenizeText(value: string): string[] {
  if (typeof value !== 'string' || value.length === 0) {
    return []
  }

  return value
    .toLocaleLowerCase('en-US')
    .replace(/'s\b/g, '')
    .split(/[^a-z0-9]+/)
    .filter((token) => token.length > 1)
}

/**
 * Splits a filename into tokens, additionally breaking camel case and
 * letter/digit boundaries, and keeping the whole segment too. So
 * `Satish_Resume-2026.pdf` and `satishResume2026.pdf` both yield `resume`.
 */
export function tokenizeName(value: string): string[] {
  if (typeof value !== 'string' || value.length === 0) {
    return []
  }

  const tokens: string[] = []
  for (const segment of value.split(/[^A-Za-z0-9]+/)) {
    if (segment.length === 0) {
      continue
    }

    const whole = segment.toLocaleLowerCase('en-US')
    if (whole.length > 1) {
      tokens.push(whole)
    }

    const parts = segment
      .replace(/([a-z])([A-Z])/g, '$1 $2')
      .replace(/([A-Za-z])(\d)/g, '$1 $2')
      .replace(/(\d)([A-Za-z])/g, '$1 $2')
      .toLocaleLowerCase('en-US')
      .split(' ')
      .filter((part) => part.length > 1 && part !== whole)
    tokens.push(...parts)
  }

  return [...new Set(tokens)]
}

export function expandTerm(term: string): readonly string[] {
  return SYNONYM_LOOKUP.get(term) ?? [term]
}

export function isSearchKind(value: unknown): value is SearchKind {
  return typeof value === 'string' && (SEARCH_KINDS as readonly string[]).includes(value)
}

export function isSearchRecency(value: unknown): value is SearchRecency {
  return typeof value === 'string' && (SEARCH_RECENCIES as readonly string[]).includes(value)
}

export function normalizeSearchQuery(input: SearchQueryInput): NormalizedSearchQuery {
  if (typeof input?.queryTerms !== 'string' || input.queryTerms.trim().length === 0) {
    throw new SearchQueryValidationError('A non-empty search request is required.')
  }
  if (input.queryTerms.length > MAX_QUERY_LENGTH) {
    throw new SearchQueryValidationError(`A search request must be shorter than ${MAX_QUERY_LENGTH} characters.`)
  }
  if (input.kind !== undefined && !isSearchKind(input.kind)) {
    throw new SearchQueryValidationError('kind must be document, photo, screenshot, or any.')
  }
  if (input.recency !== undefined && !isSearchRecency(input.recency)) {
    throw new SearchQueryValidationError('recency must be latest or any.')
  }

  const concepts = normalizeConcepts(input.concepts)

  const rawTokens = tokenizeText(input.queryTerms)
  const detectedRecency = rawTokens.some((token) => RECENCY_WORDS.has(token)) ? 'latest' : 'any'
  const meaningful = rawTokens.filter((token) => !RECENCY_WORDS.has(token) && !STOPWORDS.has(token))
  // A request made entirely of stopwords still deserves a search, so fall back
  // to the raw tokens rather than searching for nothing.
  const kept = meaningful.length > 0 ? meaningful : rawTokens.filter((token) => !RECENCY_WORDS.has(token))
  const terms = [...new Set(kept)].slice(0, MAX_TERMS)

  if (terms.length === 0) {
    throw new SearchQueryValidationError('A search request needs at least one meaningful word.')
  }

  const synonyms = [...new Set(terms.flatMap((term) => expandTerm(term)))].filter((term) => !terms.includes(term))

  return Object.freeze({
    raw: rawTokens.join(' '),
    phrase: terms.join(' '),
    terms: Object.freeze([...terms]) as unknown as string[],
    synonyms: Object.freeze([...synonyms]) as unknown as string[],
    kind: input.kind && input.kind !== 'any' ? input.kind : detectKind(rawTokens),
    recency: input.recency ?? detectedRecency,
    concepts: Object.freeze(concepts) as unknown as string[]
  })
}

export function normalizeConcepts(value: unknown): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_SEARCH_CONCEPTS) {
    throw new SearchQueryValidationError(`concepts must contain one to ${MAX_SEARCH_CONCEPTS} short concepts.`)
  }

  const concepts = value.map((candidate) => {
    if (typeof candidate !== 'string') throw new SearchQueryValidationError('Every concept must be text.')
    const concept = candidate.replace(/\s+/g, ' ').trim()
    if (concept.length === 0 || concept.length > MAX_SEARCH_CONCEPT_LENGTH) {
      throw new SearchQueryValidationError(`Every concept must be at most ${MAX_SEARCH_CONCEPT_LENGTH} characters.`)
    }
    if (/[\\/]|^[a-f0-9]{20,}$/i.test(concept) || /[\[\]{}]/.test(concept)) {
      throw new SearchQueryValidationError('A concept must be a short natural-language description.')
    }
    return concept.toLocaleLowerCase('en-US')
  })
  return [...new Set(concepts)]
}

function detectKind(tokens: readonly string[]): SearchKind {
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (SCREENSHOT_WORDS.has(token)) {
      return 'screenshot'
    }
    // "screen capture" and "screen shot" arrive as two tokens.
    if (token === 'screen') {
      const next = tokens[index + 1]
      if (next === 'capture' || next === 'shot' || next === 'grab') {
        return 'screenshot'
      }
    }
  }

  if (tokens.some((token) => PHOTO_WORDS.has(token))) {
    return 'photo'
  }
  if (tokens.some((token) => DOCUMENT_WORDS.has(token))) {
    return 'document'
  }
  return 'any'
}

export type FileKind = 'document' | 'photo' | 'screenshot' | 'other'

export function classifyFileKind(name: string, extension: string, parentFolders: readonly string[]): FileKind {
  const normalizedExtension = extension.toLocaleLowerCase('en-US')
  if (PHOTO_EXTENSIONS.has(normalizedExtension)) {
    return isScreenshotLike(name, parentFolders) ? 'screenshot' : 'photo'
  }
  if (DOCUMENT_EXTENSIONS.has(normalizedExtension)) {
    return 'document'
  }
  return 'other'
}

function isScreenshotLike(name: string, parentFolders: readonly string[]): boolean {
  const nameTokens = tokenizeName(name)
  // Covers screenshot, screen shot, screen_capture, screengrab, and capture.
  if (nameTokens.some((token) => SCREENSHOT_WORDS.has(token) || token === 'capture' || token === 'captures')) {
    return true
  }
  for (let index = 0; index < nameTokens.length - 1; index += 1) {
    if (nameTokens[index] === 'screen' && SCREENSHOT_SECOND_WORDS.has(nameTokens[index + 1]!)) {
      return true
    }
  }

  return parentFolders.some((folder) => {
    const folderTokens = tokenizeName(folder)
    return folderTokens.some((token) => SCREENSHOT_WORDS.has(token) || token === 'capture' || token === 'captures')
  })
}

export function fileKindLabel(kind: FileKind): string {
  switch (kind) {
    case 'document':
      return 'Document'
    case 'photo':
      return 'Photo'
    case 'screenshot':
      return 'Screenshot'
    default:
      return 'File'
  }
}

/** A requested photo search still includes screenshots, which are photos. */
export function kindSatisfies(requested: SearchKind, actual: FileKind): boolean {
  if (requested === 'any') {
    return true
  }
  if (requested === 'photo') {
    return actual === 'photo' || actual === 'screenshot'
  }
  return requested === actual
}

const MINUTE_MS = 60_000
const HOUR_MS = 60 * MINUTE_MS
const DAY_MS = 24 * HOUR_MS

/** Coarse relative age. Deliberately imprecise so no exact timestamp is shared. */
export function formatModifiedAgo(modifiedAtMs: number, nowMs: number): string {
  const elapsed = nowMs - modifiedAtMs
  if (!Number.isFinite(elapsed)) {
    return 'unknown'
  }
  if (elapsed < HOUR_MS) {
    return 'less than an hour ago'
  }
  if (elapsed < DAY_MS) {
    const hours = Math.max(1, Math.floor(elapsed / HOUR_MS))
    return hours === 1 ? '1 hour ago' : `${hours} hours ago`
  }

  const days = Math.floor(elapsed / DAY_MS)
  if (days === 1) {
    return 'yesterday'
  }
  if (days < 30) {
    return `${days} days ago`
  }

  const months = Math.floor(days / 30)
  if (months < 12) {
    return months === 1 ? 'about a month ago' : `about ${months} months ago`
  }

  const years = Math.floor(days / 365)
  return years <= 1 ? 'about a year ago' : `about ${years} years ago`
}
