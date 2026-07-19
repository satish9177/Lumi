import { readFile } from 'node:fs/promises'
import { tokenizeName, type NormalizedSearchQuery } from '../../shared/search-query'
import type { VisionEngine } from './engine'
import type { PhotoIndexRecord } from './index-store'
import { resolveAssetPath } from './model-pack'
import { CLIP_EMBEDDING_LENGTH } from './protocol'
import { createClipTokenizer, type ClipTokenizer } from './tokenizer'

export const SEMANTIC_WEIGHT = 0.8
export const FILENAME_WEIGHT = 0.15
export const RECENCY_WEIGHT = 0.05
export const STRONG_VISUAL_MATCH = 0.27
export const POSSIBLE_VISUAL_MATCH = 0.2
export const SEMANTIC_HONESTY_TIERS = Object.freeze({
  strong: 'strong_visual_match',
  possible: 'possible_visual_match',
  filenameOnly: 'filename_only_fallback',
  unindexed: 'unindexed',
  none: 'no_reliable_match'
} as const)
export type SemanticHonestyTier = (typeof SEMANTIC_HONESTY_TIERS)[keyof typeof SEMANTIC_HONESTY_TIERS]

export interface SemanticRankedPhoto {
  record: PhotoIndexRecord
  cosine: number
  fusedScore: number
  tier: SemanticHonestyTier
  reason: string
}

export function conceptPromptEnsemble(concept: string): readonly string[] {
  return [`a photo of ${concept}`, concept]
}

export class LocalQueryEmbedder {
  private tokenizer: ClipTokenizer | undefined

  constructor(
    private readonly userDataDir: string,
    private readonly engine: () => VisionEngine
  ) {}

  async embed(concepts: readonly string[]): Promise<Float32Array> {
    if (concepts.length === 0 || concepts.length > 3) throw new Error('A semantic query needs one to three concepts.')
    const tokenizer = await this.getTokenizer()
    const vectors: Float32Array[] = []
    for (const concept of concepts) {
      for (const prompt of conceptPromptEnsemble(concept)) {
        const encoded = tokenizer.encode(prompt)
        const vector = await this.engine().embedText(encoded.tokenIds, encoded.tokenCount)
        assertEmbedding(vector)
        vectors.push(vector)
      }
    }
    return normalizedAverage(vectors)
  }

  clear(): void {
    this.tokenizer = undefined
  }

  private async getTokenizer(): Promise<ClipTokenizer> {
    if (this.tokenizer) return this.tokenizer
    const [vocabulary, merges] = await Promise.all([
      readFile(resolveAssetPath(this.userDataDir, 'vocabulary'), 'utf8'),
      readFile(resolveAssetPath(this.userDataDir, 'merges'), 'utf8')
    ])
    this.tokenizer = createClipTokenizer(vocabulary, merges)
    return this.tokenizer
  }
}

export function rankSemanticPhotos(
  records: readonly PhotoIndexRecord[],
  vectors: ReadonlyMap<string, Float32Array>,
  queryVector: Float32Array,
  query: NormalizedSearchQuery,
  nowMs: number
): SemanticRankedPhoto[] {
  assertEmbedding(queryVector)
  const conceptLabel = query.concepts.join(' / ')
  const ranked: SemanticRankedPhoto[] = []

  for (const record of records) {
    const vector = vectors.get(record.imageId)
    if (!vector || vector.length !== CLIP_EMBEDDING_LENGTH) continue
    const cosine = dot(queryVector, vector)
    if (!Number.isFinite(cosine) || cosine < POSSIBLE_VISUAL_MATCH) continue
    const semanticSignal = clamp((cosine - POSSIBLE_VISUAL_MATCH) / (0.4 - POSSIBLE_VISUAL_MATCH))
    const filenameSignal = filenameFolderSignal(record, query)
    const ageDays = Math.max(0, (nowMs - record.mtimeMs) / 86_400_000)
    const recencySignal = Number.isFinite(ageDays) ? Math.pow(2, -ageDays / 365) : 0
    const fusedScore = SEMANTIC_WEIGHT * semanticSignal + FILENAME_WEIGHT * filenameSignal + RECENCY_WEIGHT * recencySignal
    const tier = cosine >= STRONG_VISUAL_MATCH ? SEMANTIC_HONESTY_TIERS.strong : SEMANTIC_HONESTY_TIERS.possible
    ranked.push({
      record,
      cosine,
      fusedScore,
      tier,
      reason: tier === SEMANTIC_HONESTY_TIERS.strong
        ? `Strong visual match: ${conceptLabel}`
        : `Possible visual match: ${conceptLabel}`
    })
  }

  return ranked.sort((left, right) =>
    right.fusedScore - left.fusedScore ||
    right.record.mtimeMs - left.record.mtimeMs ||
    compareRelative(left.record.relativePath, right.record.relativePath))
}

export function normalizedAverage(vectors: readonly Float32Array[]): Float32Array {
  if (vectors.length === 0) throw new Error('Cannot average an empty embedding set.')
  const average = new Float32Array(CLIP_EMBEDDING_LENGTH)
  for (const vector of vectors) {
    assertEmbedding(vector)
    for (let index = 0; index < average.length; index += 1) average[index] += vector[index]! / vectors.length
  }
  let squared = 0
  for (const value of average) squared += value * value
  const magnitude = Math.sqrt(squared)
  if (!Number.isFinite(magnitude) || magnitude === 0) throw new Error('The local query embedding was empty.')
  for (let index = 0; index < average.length; index += 1) average[index] /= magnitude
  return average
}

export function dot(left: Float32Array, right: Float32Array): number {
  if (left.length !== CLIP_EMBEDDING_LENGTH || right.length !== CLIP_EMBEDDING_LENGTH) return Number.NaN
  let total = 0
  for (let index = 0; index < left.length; index += 1) total += left[index]! * right[index]!
  return total
}

function assertEmbedding(vector: Float32Array): void {
  if (!(vector instanceof Float32Array) || vector.length !== CLIP_EMBEDDING_LENGTH) {
    throw new Error('The local model returned an unexpected embedding size.')
  }
  let squared = 0
  for (const value of vector) {
    if (!Number.isFinite(value)) throw new Error('The local model returned a non-finite embedding.')
    squared += value * value
  }
  if (squared === 0) throw new Error('The local model returned an empty embedding.')
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function filenameFolderSignal(record: PhotoIndexRecord, query: NormalizedSearchQuery): number {
  const tokens = new Set(record.relativePath.split('/').flatMap((segment) => tokenizeName(segment)))
  const terms = query.terms.length > 0 ? query.terms : [query.phrase]
  let matched = 0
  for (const term of terms) {
    if (tokens.has(term) || query.synonyms.some((synonym) => tokens.has(synonym))) matched += 1
  }
  return clamp(matched / Math.max(1, terms.length))
}

function compareRelative(left: string, right: string): number {
  const a = left.toLocaleLowerCase('en-US')
  const b = right.toLocaleLowerCase('en-US')
  return a < b ? -1 : a > b ? 1 : left < right ? -1 : left > right ? 1 : 0
}
