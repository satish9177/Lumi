/**
 * Fusing the available signals into one ranking.
 *
 * Four things can say something about a photo: what it looks like (CLIP), what
 * is written on it (OCR), what it is called (filename and folder), and how
 * recently it changed. A given search has some subset of those — a text-only
 * query has no concepts, an unscanned image has no OCR, a Phase-1 index has no
 * faces — so the weights are renormalized over whatever is actually present.
 * Without that, a query missing one signal would systematically score lower
 * than one that has it, and the two could not be compared.
 *
 * Visible-face count is deliberately *not* a weighted signal. "Photos with two
 * people" is a constraint, not a preference: a photo of one person is not a
 * slightly worse answer, it is the wrong answer. It filters.
 *
 * Every claim this module attaches to a result is app-authored and chosen from
 * a closed set. Nothing derived from the image — no recognized text, no
 * cosine, no confidence, no box — is ever put into a reason string.
 */

import {
  formatModifiedAgo,
  tokenizeName,
  type NormalizedSearchQuery,
  type PeopleFilter
} from '../../shared/search-query'
import type { PhotoIndexRecord } from './index-store'
import { matchOcr, NO_OCR_MATCH, type OcrMatch } from './ocr-match'
import { qualifiesAsMatch, resolveMatch } from './people-records'
import { peopleReason } from './people-search'
import { CLIP_EMBEDDING_LENGTH } from './protocol'
import { dot, POSSIBLE_VISUAL_MATCH, STRONG_VISUAL_MATCH } from './semantic-search'

/**
 * Base weights when every signal is present. Renormalized over those that are.
 */
export const HYBRID_WEIGHTS = Object.freeze({
  semantic: 0.55,
  ocr: 0.3,
  filename: 0.1,
  recency: 0.05
})

/** How a result is allowed to be described. Closed set, app-authored. */
export const HYBRID_TIERS = [
  'people_likely',
  'people_possible',
  'text_exact',
  'text_fuzzy',
  'strong_visual',
  'possible_visual',
  'filename_only',
  'face_count_only'
] as const
export type HybridTier = (typeof HYBRID_TIERS)[number]

/** One labelled person the request named, resolved to a profile main trusts. */
export interface PeopleLabelRequirement {
  id: string
  /** See StoredPersonProfile.revision; stale stored outcomes must not resolve. */
  revision: number
  /** The user's own casing, for the reason string. */
  label: string
}

export interface PeopleLabelOutcome {
  matches: boolean
  /** True when at least one requested profile has not been checked yet. */
  unchecked: boolean
  /** Present only when `matches`. One entry per requested profile. */
  tiers: Array<{ label: string; tier: 'likely' | 'possible' }>
}

export interface FaceFilterOutcome {
  /** Whether the record satisfies the requested count. */
  matches: boolean
  /** True when the answer relies on a detection the model was unsure about. */
  uncertain: boolean
  /** True when this image has never been scanned, so nothing is known. */
  unchecked: boolean
}

export interface HybridRankedPhoto {
  record: PhotoIndexRecord
  fusedScore: number
  tier: HybridTier
  /** App-authored, safe to show and safe to speak. */
  reason: string
  ocr: OcrMatch
  faces: FaceFilterOutcome
}

export interface HybridCoverage {
  /** Records excluded because their text has not been read yet. */
  ocrUnchecked: number
  /** Records excluded because their faces have not been counted yet. */
  faceUnchecked: number
  /** Records excluded because a requested profile has not been checked yet. */
  peopleUnchecked: number
}

export interface HybridSearchResult {
  ranked: HybridRankedPhoto[]
  coverage: HybridCoverage
}

/**
 * Applies a people constraint to one record.
 *
 * An unscanned image is never treated as zero faces. "Find photos without
 * people" over a half-scanned library must not return every image nobody has
 * looked at yet — so `unchecked` is its own outcome, reported as coverage
 * rather than silently answered.
 */
export function applyFaceFilter(record: PhotoIndexRecord, filter: PeopleFilter): FaceFilterOutcome {
  if (record.faceStatus !== 'done') {
    return { matches: false, uncertain: false, unchecked: true }
  }

  const visible = record.visibleFaceCount ?? 0
  const uncertainCount = record.uncertainFaceCount ?? 0

  if (filter.op === 'none') {
    // Both counts must be zero. A hedged detection is still evidence of a
    // person, and claiming a photo has nobody in it on that basis is the one
    // answer here that is actively misleading.
    return { matches: visible === 0 && uncertainCount === 0, uncertain: false, unchecked: false }
  }

  const n = filter.n ?? 0

  if (filter.op === 'gte') {
    if (visible >= n) {
      return { matches: true, uncertain: false, unchecked: false }
    }
    // The uncertain detections would carry it over the line.
    return { matches: visible + uncertainCount >= n, uncertain: visible + uncertainCount >= n, unchecked: false }
  }

  if (visible === n) {
    return { matches: true, uncertain: false, unchecked: false }
  }
  // An exact count can also be reached by counting the unsure detections, but
  // only ever as a hedged match.
  const withUncertain = visible + uncertainCount
  return {
    matches: visible < n && withUncertain >= n,
    uncertain: visible < n && withUncertain >= n,
    unchecked: false
  }
}

/**
 * Applies a labelled-person constraint to one record.
 *
 * AND semantics by default: naming two people asks for photos with *both*, not
 * either. A firm miss on any one of them ends the check immediately — the
 * request cannot be satisfied regardless of what the others say. Only when
 * nothing has firmly failed does an unresolved profile change the answer to
 * "not checked", because that is the one circumstance where the true answer
 * might still be yes.
 */
export function applyPeopleFilter(
  record: PhotoIndexRecord,
  profiles: readonly PeopleLabelRequirement[],
  inFlight?: ReadonlySet<string>
): PeopleLabelOutcome {
  let firmMiss = false
  let anyUnchecked = false
  const tiers: Array<{ label: string; tier: 'likely' | 'possible' }> = []

  for (const profile of profiles) {
    const resolved = resolveMatch(record, profile, inFlight?.has(record.imageId) ?? false)
    if (resolved.status === 'not_checked' || resolved.status === 'checking') {
      anyUnchecked = true
      continue
    }
    if (qualifiesAsMatch(resolved)) {
      tiers.push({ label: profile.label, tier: resolved.status as 'likely' | 'possible' })
      continue
    }
    // checked_no_reliable_match, failed_retryable, failed_permanent, or
    // profile_unavailable: a real answer, and it is no.
    firmMiss = true
  }

  if (firmMiss) {
    return { matches: false, unchecked: false, tiers: [] }
  }
  if (anyUnchecked) {
    return { matches: false, unchecked: true, tiers: [] }
  }
  return { matches: true, unchecked: false, tiers }
}

/**
 * Ranks the index against a query.
 *
 * `vectors` may be empty and `queryVector` absent — a `contains_text` or
 * `people` query is perfectly valid with no visual concept at all.
 */
export function rankHybridPhotos(
  records: readonly PhotoIndexRecord[],
  vectors: ReadonlyMap<string, Float32Array>,
  queryVector: Float32Array | undefined,
  query: NormalizedSearchQuery,
  nowMs: number,
  peopleProfiles: readonly PeopleLabelRequirement[] = [],
  peopleInFlight?: ReadonlySet<string>
): HybridSearchResult {
  const wantsSemantic = queryVector !== undefined && query.concepts.length > 0
  const wantsOcr = query.containsTextTokens.length > 0
  const wantsPeople = peopleProfiles.length > 0
  const conceptLabel = query.concepts.join(' / ')

  const ranked: HybridRankedPhoto[] = []
  const coverage: HybridCoverage = { ocrUnchecked: 0, faceUnchecked: 0, peopleUnchecked: 0 }

  for (const record of records) {
    // --- the hard filters come first, so nothing else is computed for a
    // record that cannot be returned at all.
    let faces: FaceFilterOutcome = { matches: true, uncertain: false, unchecked: false }
    if (query.people) {
      faces = applyFaceFilter(record, query.people)
      if (faces.unchecked) {
        coverage.faceUnchecked += 1
        continue
      }
      if (!faces.matches) {
        continue
      }
    }

    let people: PeopleLabelOutcome = { matches: true, unchecked: false, tiers: [] }
    if (wantsPeople) {
      people = applyPeopleFilter(record, peopleProfiles, peopleInFlight)
      if (people.unchecked) {
        coverage.peopleUnchecked += 1
        continue
      }
      if (!people.matches) {
        continue
      }
    }

    // --- text
    let ocr: OcrMatch = NO_OCR_MATCH
    if (wantsOcr) {
      if (record.ocrStatus !== 'done') {
        coverage.ocrUnchecked += 1
        continue
      }
      ocr = matchOcr(record.ocrTokens, query.containsTextTokens)
      if (ocr.strength === 'none') {
        // Text was asked for and is genuinely absent. Returning this photo
        // anyway on visual similarity alone would answer a different question.
        continue
      }
    }

    // --- vision
    let cosine: number | undefined
    if (wantsSemantic) {
      const vector = vectors.get(record.imageId)
      if (vector && vector.length === CLIP_EMBEDDING_LENGTH) {
        const value = dot(queryVector, vector)
        if (Number.isFinite(value)) {
          cosine = value
        }
      }
      // A concept-only query with nothing above the honesty floor is not a
      // match. With a text, face-count, or labelled-person constraint also
      // present, the other evidence carries it and the visual signal simply
      // contributes to ranking rather than gating admission.
      if (!wantsOcr && !query.people && !wantsPeople && (cosine === undefined || cosine < POSSIBLE_VISUAL_MATCH)) {
        continue
      }
    }

    const filenameSignal = filenameFolderSignal(record, query)
    const ageDays = Math.max(0, (nowMs - record.mtimeMs) / 86_400_000)
    const recencySignal = Number.isFinite(ageDays) ? Math.pow(2, -ageDays / 365) : 0

    // --- fusion over whatever is present
    let weighted = 0
    let available = 0

    if (wantsSemantic && cosine !== undefined) {
      const semanticSignal = clamp((cosine - POSSIBLE_VISUAL_MATCH) / (0.4 - POSSIBLE_VISUAL_MATCH))
      weighted += HYBRID_WEIGHTS.semantic * semanticSignal
      available += HYBRID_WEIGHTS.semantic
    }
    if (wantsOcr) {
      weighted += HYBRID_WEIGHTS.ocr * ocr.score
      available += HYBRID_WEIGHTS.ocr
    }
    weighted += HYBRID_WEIGHTS.filename * filenameSignal
    available += HYBRID_WEIGHTS.filename
    weighted += HYBRID_WEIGHTS.recency * recencySignal
    available += HYBRID_WEIGHTS.recency

    const fusedScore = available > 0 ? weighted / available : 0
    const tier = tierFor({
      ocr,
      cosine,
      faces,
      filenameSignal,
      hasPeopleFilter: query.people !== undefined,
      peopleTiers: wantsPeople ? people.tiers : undefined
    })

    ranked.push({
      record,
      fusedScore,
      tier,
      reason: reasonFor(tier, { conceptLabel, record, faces, nowMs, peopleTiers: people.tiers }),
      ocr,
      faces
    })
  }

  // Deterministic ordering: a labelled-person likely match ranks above a
  // possible one regardless of the other signals — that is what "likely
  // matches rank above possible matches" means structurally. Every other tier
  // ranks equally on this key, so a search with no people constraint sorts
  // exactly as it did before. Semantic score, recency and filename still order
  // results *within* a tier, which is how they may rank a qualifying match
  // without ever being able to manufacture one.
  ranked.sort(
    (left, right) =>
      peopleTierRank(right.tier) - peopleTierRank(left.tier) ||
      right.fusedScore - left.fusedScore ||
      right.record.mtimeMs - left.record.mtimeMs ||
      compareRelative(left.record.relativePath, right.record.relativePath)
  )

  return { ranked, coverage }
}

function peopleTierRank(tier: HybridTier): number {
  if (tier === 'people_likely') return 2
  if (tier === 'people_possible') return 1
  return 0
}

function tierFor(context: {
  ocr: OcrMatch
  cosine: number | undefined
  faces: FaceFilterOutcome
  filenameSignal: number
  hasPeopleFilter: boolean
  peopleTiers?: ReadonlyArray<{ label: string; tier: 'likely' | 'possible' }>
}): HybridTier {
  // A labelled-person request is the headline claim whenever it is present: it
  // is the strongest, most specific thing Lumi can say about a photo, and it
  // takes priority over text or visual similarity in what gets said, even
  // though those signals still shape the ranking through fusedScore.
  if (context.peopleTiers && context.peopleTiers.length > 0) {
    return context.peopleTiers.every((entry) => entry.tier === 'likely') ? 'people_likely' : 'people_possible'
  }
  // Text found verbatim is the strongest and least ambiguous evidence there is.
  if (context.ocr.strength === 'exact' || context.ocr.strength === 'all_tokens') {
    return 'text_exact'
  }
  if (context.ocr.strength === 'fuzzy') {
    return 'text_fuzzy'
  }
  if (context.cosine !== undefined && context.cosine >= STRONG_VISUAL_MATCH) {
    return 'strong_visual'
  }
  if (context.cosine !== undefined && context.cosine >= POSSIBLE_VISUAL_MATCH) {
    return 'possible_visual'
  }
  if (context.hasPeopleFilter) {
    return 'face_count_only'
  }
  return 'filename_only'
}

/**
 * The complete set of things Lumi may say about why a photo was returned.
 *
 * Each one is written here, in the application, from index metadata alone.
 * None is derived from recognized text or from a model's own words, which is
 * what keeps a photographed instruction from ever being repeated back as
 * though Lumi meant it.
 */
function reasonFor(
  tier: HybridTier,
  context: {
    conceptLabel: string
    record: PhotoIndexRecord
    faces: FaceFilterOutcome
    nowMs: number
    peopleTiers?: ReadonlyArray<{ label: string; tier: 'likely' | 'possible' }>
  }
): string {
  switch (tier) {
    case 'people_likely':
    case 'people_possible':
      return context.peopleTiers ? peopleReason(context.peopleTiers) : 'No reliable match found'
    case 'text_exact':
      return 'Contains the text you searched for'
    case 'text_fuzzy':
      return 'Contains text closely matching your search'
    case 'strong_visual':
      return `Strong visual match: ${context.conceptLabel}`
    case 'possible_visual':
      return `Possible visual match: ${context.conceptLabel}`
    case 'face_count_only':
      return faceReason(context.record, context.faces)
    default:
      return `Filename match, ${formatModifiedAgo(context.record.mtimeMs, context.nowMs)}`
  }
}

/**
 * Face wording stays literally true. It says "visible faces", never "people":
 * someone turned away or behind another guest is present but undetected, and
 * the copy must not quietly promise otherwise.
 */
export function faceReason(record: PhotoIndexRecord, outcome: FaceFilterOutcome): string {
  if (outcome.unchecked || record.faceStatus !== 'done') {
    return 'Not checked for visible faces yet'
  }
  const visible = record.visibleFaceCount ?? 0
  if (outcome.uncertain) {
    return 'Possible visible-face-count match'
  }
  if (visible === 0) {
    return 'No visible faces detected'
  }
  return visible === 1 ? '1 visible face detected' : `${visible} visible faces detected`
}

function filenameFolderSignal(record: PhotoIndexRecord, query: NormalizedSearchQuery): number {
  const tokens = new Set(record.relativePath.split('/').flatMap((segment) => tokenizeName(segment)))
  const terms = query.terms.length > 0 ? query.terms : [query.phrase]
  let matched = 0
  for (const term of terms) {
    if (tokens.has(term) || query.synonyms.some((synonym) => tokens.has(synonym))) {
      matched += 1
    }
  }
  return clamp(matched / Math.max(1, terms.length))
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function compareRelative(left: string, right: string): number {
  const a = left.toLocaleLowerCase('en-US')
  const b = right.toLocaleLowerCase('en-US')
  return a < b ? -1 : a > b ? 1 : left < right ? -1 : left > right ? 1 : 0
}
