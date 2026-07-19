import { describe, expect, it } from 'vitest'
import { normalizeSearchQuery } from '../../shared/search-query'
import type { PhotoIndexRecord } from './index-store'
import { conceptPromptEnsemble, dot, normalizedAverage, POSSIBLE_VISUAL_MATCH, rankSemanticPhotos, STRONG_VISUAL_MATCH } from './semantic-search'

describe('semantic photo query and ranking', () => {
  it('uses the app-authored two-prompt ensemble', () => {
    expect(conceptPromptEnsemble('beach')).toEqual(['a photo of beach', 'beach'])
  })

  it('averages and normalizes exact 512-dimensional vectors', () => {
    const a = unit(0)
    const b = unit(1)
    const average = normalizedAverage([a, b])
    expect(average).toHaveLength(512)
    expect(dot(average, average)).toBeCloseTo(1, 5)
    expect(() => normalizedAverage([new Float32Array(511)])).toThrow()
  })

  it('applies honesty thresholds and deterministic tie breaking', () => {
    const query = normalizeSearchQuery({ queryTerms: 'beach photos', kind: 'photo', concepts: ['beach'] })
    const queryVector = unit(0)
    const records = [record('b.jpg', 2), record('a.jpg', 2), record('weak.jpg', 3)]
    const vectors = new Map<string, Float32Array>([
      [records[0]!.imageId, vectorWithCosine(STRONG_VISUAL_MATCH + 0.01)],
      [records[1]!.imageId, vectorWithCosine(STRONG_VISUAL_MATCH + 0.01)],
      [records[2]!.imageId, vectorWithCosine(POSSIBLE_VISUAL_MATCH - 0.01)]
    ])
    const ranked = rankSemanticPhotos(records, vectors, queryVector, query, 10)
    expect(ranked.map((entry) => entry.record.relativePath)).toEqual(['a.jpg', 'b.jpg'])
    expect(ranked[0]?.reason).toBe('Strong visual match: beach')
  })
})

function unit(index: number): Float32Array {
  const vector = new Float32Array(512)
  vector[index] = 1
  return vector
}

function vectorWithCosine(cosine: number): Float32Array {
  const vector = new Float32Array(512)
  vector[0] = cosine
  vector[1] = Math.sqrt(1 - cosine * cosine)
  return vector
}

function record(relativePath: string, mtimeMs: number): PhotoIndexRecord {
  return {
    imageId: relativePath, rootId: 'root', relativePath, name: relativePath, mtimeMs, sizeBytes: 10,
    modelVersion: 1, status: 'indexed', vectorRow: 0, attempts: 1, updatedAtMs: 1
  }
}
