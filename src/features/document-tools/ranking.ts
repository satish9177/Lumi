// Deterministic scoring for stored-file candidates. No model is involved: the
// same query and the same folder always produce the same order.

import {
  kindSatisfies,
  tokenizeName,
  type FileKind,
  type NormalizedSearchQuery
} from '../../shared/search-query'

export interface RankableCandidate {
  /** File name including extension. */
  name: string
  /** Lowercase extension including the leading dot. */
  extension: string
  /** Slash-separated path relative to the approved root. */
  relativePath: string
  kind: FileKind
  modifiedAtMs: number
  sizeBytes: number
}

export interface CandidateScore {
  score: number
  /** Name-derived relevance only, used for the plausibility threshold. */
  matchScore: number
  /** True when the filename genuinely relates to the query. */
  plausible: boolean
}

const EXACT_NAME_WEIGHT = 3
const PHRASE_WEIGHT = 2
const COVERAGE_WEIGHT = 2
const SYNONYM_WEIGHT = 1.5
const FUZZY_WEIGHT = 1
const KIND_WEIGHT = 1
const PATH_HINT_WEIGHT = 0.75
const RECENCY_WEIGHT = 1.25
const RECENCY_HALF_LIFE_DAYS = 30
const JUNK_PENALTY = 2
const PLAUSIBLE_THRESHOLD = 1

const DAY_MS = 24 * 60 * 60 * 1_000

const PENALIZED_EXTENSIONS = new Set(['.ini', '.db', '.lnk', '.url', '.bak', '.old'])

export function scoreCandidate(
  candidate: RankableCandidate,
  query: NormalizedSearchQuery,
  nowMs: number
): CandidateScore {
  const nameTokens = tokenizeName(stripExtension(candidate.name))
  const nameText = nameTokens.join(' ')
  const nameTokenSet = new Set(nameTokens)
  const folderTokens = new Set(parentFolderTokens(candidate.relativePath))

  let direct = 0
  let synonym = 0
  let fuzzy = 0

  for (const term of query.terms) {
    if (nameTokenSet.has(term)) {
      direct += 1
      continue
    }
    if (matchesSynonym(term, nameTokenSet, query)) {
      synonym += 1
      continue
    }
    if (matchesFuzzy(term, nameTokens)) {
      fuzzy += 1
    }
  }

  const termCount = query.terms.length || 1
  let matchScore =
    COVERAGE_WEIGHT * (direct / termCount) +
    SYNONYM_WEIGHT * (synonym / termCount) +
    FUZZY_WEIGHT * (fuzzy / termCount)

  if (nameText === query.phrase) {
    matchScore += EXACT_NAME_WEIGHT
  } else if (query.terms.length > 1 && nameText.includes(query.phrase)) {
    matchScore += PHRASE_WEIGHT
  }

  const pathHint = [...query.terms, ...query.synonyms].some((term) => folderTokens.has(term)) ? PATH_HINT_WEIGHT : 0
  const kindBonus = query.kind !== 'any' && kindSatisfies(query.kind, candidate.kind) ? KIND_WEIGHT : 0
  const penalty = junkPenalty(candidate)
  const score = matchScore + pathHint + kindBonus + recencyBoost(candidate.modifiedAtMs, nowMs) - penalty

  return {
    score,
    matchScore,
    plausible: matchScore >= PLAUSIBLE_THRESHOLD && penalty === 0
  }
}

/**
 * Ranks pre-collected candidates. Enumeration happens before this call and
 * truncation after it, so a newer match is never lost to a traversal cap.
 */
export function rankCandidates<T extends RankableCandidate>(
  candidates: readonly T[],
  query: NormalizedSearchQuery,
  nowMs: number
): Array<T & CandidateScore> {
  const scored = candidates
    .map((candidate) => ({ ...candidate, ...scoreCandidate(candidate, query, nowMs) }))
    .filter((candidate) => candidate.plausible)

  return scored.sort((left, right) =>
    query.recency === 'latest'
      ? right.modifiedAtMs - left.modifiedAtMs || right.score - left.score || comparePaths(left, right)
      : right.score - left.score || right.modifiedAtMs - left.modifiedAtMs || comparePaths(left, right))
}

/** Newest files of the requested kind, used when no filename is plausible. */
export function recentCandidatesOfKind<T extends RankableCandidate>(
  candidates: readonly T[],
  query: NormalizedSearchQuery
): T[] {
  const requested = query.kind === 'any' ? 'document' : query.kind
  const matching = candidates.filter(
    (candidate) => kindSatisfies(requested, candidate.kind) && junkPenalty(candidate) === 0
  )
  const pool = matching.length > 0 || query.kind !== 'any'
    ? matching
    : candidates.filter((candidate) => junkPenalty(candidate) === 0)

  return [...pool].sort((left, right) => right.modifiedAtMs - left.modifiedAtMs || comparePaths(left, right))
}

function matchesSynonym(term: string, nameTokens: ReadonlySet<string>, query: NormalizedSearchQuery): boolean {
  return query.synonyms.some((synonym) => synonym !== term && nameTokens.has(synonym))
}

function matchesFuzzy(term: string, nameTokens: readonly string[]): boolean {
  if (term.length < 4) {
    return false
  }
  return nameTokens.some((nameToken) => {
    if (nameToken.length < 4) {
      return false
    }
    if (nameToken.startsWith(term) || term.startsWith(nameToken)) {
      return true
    }
    return Math.abs(nameToken.length - term.length) <= 1 && editDistanceWithinOne(term, nameToken)
  })
}

/** Bounded Levenshtein: returns true only for a single edit. */
function editDistanceWithinOne(left: string, right: string): boolean {
  if (left === right) {
    return true
  }

  const [shorter, longer] = left.length <= right.length ? [left, right] : [right, left]
  if (longer.length - shorter.length > 1) {
    return false
  }

  let shortIndex = 0
  let longIndex = 0
  let edits = 0
  while (shortIndex < shorter.length && longIndex < longer.length) {
    if (shorter[shortIndex] === longer[longIndex]) {
      shortIndex += 1
      longIndex += 1
      continue
    }
    edits += 1
    if (edits > 1) {
      return false
    }
    if (shorter.length === longer.length) {
      shortIndex += 1
    }
    longIndex += 1
  }

  return edits + (longer.length - longIndex) + (shorter.length - shortIndex) <= 1
}

function recencyBoost(modifiedAtMs: number, nowMs: number): number {
  const ageDays = Math.max(0, (nowMs - modifiedAtMs) / DAY_MS)
  if (!Number.isFinite(ageDays)) {
    return 0
  }
  return RECENCY_WEIGHT * Math.pow(2, -ageDays / RECENCY_HALF_LIFE_DAYS)
}

function junkPenalty(candidate: RankableCandidate): number {
  if (candidate.sizeBytes <= 0) {
    return JUNK_PENALTY
  }
  return PENALIZED_EXTENSIONS.has(candidate.extension) ? JUNK_PENALTY : 0
}

function parentFolderTokens(relativePath: string): string[] {
  const segments = relativePath.split('/')
  segments.pop()
  return segments.flatMap((segment) => tokenizeName(segment))
}

function stripExtension(name: string): string {
  const lastDot = name.lastIndexOf('.')
  return lastDot > 0 ? name.slice(0, lastDot) : name
}

function comparePaths(left: RankableCandidate, right: RankableCandidate): number {
  return left.relativePath < right.relativePath ? -1 : left.relativePath > right.relativePath ? 1 : 0
}
