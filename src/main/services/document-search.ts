import {
  canonicalizeApprovedRoots,
  searchApprovedDocuments,
  type ApprovedDocumentRoot as SearchRoot
} from '../../features/document-tools/search'
import type { CompactSearchResult, DocumentSearchResult, FileSearchResults } from '../../shared/contracts'
import { formatModifiedAgo, type NormalizedSearchQuery } from '../../shared/search-query'
import type { LocalStore, SearchResultInput } from './store'
import type { SemanticSearchResult } from '../vision/coordinator'

/** The redacted view sent to the model is capped independently of the UI list. */
const MAX_MODEL_RESULTS = 5
const MAX_UI_RESULTS = 10

/**
 * Runs one deterministic search across every approved root and stores the
 * trusted results. The returned compact view is the only shape a caller may
 * hand to the model.
 */
export async function runDocumentSearch(
  store: LocalStore,
  query: NormalizedSearchQuery,
  now: () => number = () => Date.now(),
  semanticSearch?: (query: NormalizedSearchQuery) => Promise<SemanticSearchResult>
): Promise<FileSearchResults> {
  const roots = await resolveSearchRoots(store)
  if (roots.length === 0) {
    return {
      results: [],
      compactResults: [],
      fallback: false,
      message: 'No approved folder is available to search. Ask the user to approve a folder.'
    }
  }

  const search = await searchApprovedDocuments(roots, query, { maxResults: MAX_UI_RESULTS, now })
  const semantic = query.concepts.length > 0 && semanticSearch && (query.kind === 'photo' || query.kind === 'screenshot')
    ? await semanticSearch(query)
    : undefined
  const selected = semantic ? mergeSemanticResults(semantic, search.results) : search.results.map((result) => ({
    ...toSearchResultInput(result),
    modifiedAtMs: result.modifiedAtMs
  }))
  const stored = await store.saveSearchResults(selected.map(({ modifiedAtMs: _modifiedAtMs, ...result }) => result))
  const compactResults = toCompactResults(stored, selected.map((result) => result.modifiedAtMs), now())
  const fallback = semantic ? semantic.candidates.length === 0 : search.fallback

  return {
    results: stored,
    compactResults,
    fallback,
    message: semantic
      ? describeSemanticOutcome(stored.length, semantic, query)
      : describeOutcome(stored.length, search.fallback, query)
  }
}

function mergeSemanticResults(
  semantic: SemanticSearchResult,
  filenameResults: ReadonlyArray<{
    rootId: string
    name: string
    relativePath: string
    modifiedAtMs: number
    kind: DocumentSearchResult['kind']
    path: string
  }>
): Array<SearchResultInput & { modifiedAtMs: number }> {
  const merged: Array<SearchResultInput & { modifiedAtMs: number }> = semantic.candidates.map((result) => ({
    rootId: result.rootId,
    name: result.name,
    relativePath: result.relativePath,
    modifiedAt: new Date(result.modifiedAtMs).toISOString(),
    modifiedAtMs: result.modifiedAtMs,
    kind: 'photo',
    absolutePath: result.absolutePath,
    reason: result.reason
  }))
  const seen = new Set(merged.map((result) => `${result.rootId}:${result.relativePath.toLocaleLowerCase('en-US')}`))
  for (const result of filenameResults) {
    if (merged.length >= MAX_UI_RESULTS) break
    const key = `${result.rootId}:${result.relativePath.toLocaleLowerCase('en-US')}`
    if (seen.has(key)) continue
    merged.push({ ...toSearchResultInput(result), modifiedAtMs: result.modifiedAtMs, reason: 'Recent filename match only' })
    seen.add(key)
  }
  return merged.slice(0, MAX_UI_RESULTS)
}

/**
 * Maps stored roots onto canonical search roots, keeping the store's own root
 * identifier so results stay linked to the folder the user approved. A root
 * that has been deleted or unmounted is skipped rather than failing the search.
 */
async function resolveSearchRoots(store: LocalStore): Promise<SearchRoot[]> {
  const storedRoots = await store.listStoredDocumentRoots()
  const roots: SearchRoot[] = []

  for (const storedRoot of storedRoots) {
    try {
      const [canonical] = await canonicalizeApprovedRoots([storedRoot.path])
      if (!canonical) {
        continue
      }
      roots.push({ id: storedRoot.id, canonicalPath: canonical.canonicalPath, label: storedRoot.label })
    } catch {
      // An approved folder that is no longer reachable simply drops out.
    }
  }

  return roots.filter((root, index) => roots.findIndex((candidate) => candidate.id === root.id) === index)
}

function toSearchResultInput(result: {
  rootId: string
  name: string
  relativePath: string
  modifiedAtMs: number
  kind: DocumentSearchResult['kind']
  path: string
}): SearchResultInput {
  return {
    rootId: result.rootId,
    name: result.name,
    relativePath: result.relativePath,
    modifiedAt: new Date(result.modifiedAtMs).toISOString(),
    kind: result.kind,
    absolutePath: result.path
  }
}

function toCompactResults(
  stored: readonly DocumentSearchResult[],
  modifiedAtMs: readonly number[],
  nowMs: number
): CompactSearchResult[] {
  return stored.slice(0, MAX_MODEL_RESULTS).map((result, index) => ({
    ordinal: index + 1,
    name: result.name,
    modifiedAgo: formatModifiedAgo(modifiedAtMs[index] ?? Date.parse(result.modifiedAt), nowMs),
    ...(result.reason ? { reason: result.reason } : {})
  }))
}

function describeSemanticOutcome(count: number, semantic: SemanticSearchResult, query: NormalizedSearchQuery): string {
  const coverage = `${semantic.indexed} of ${semantic.total} photos have been indexed.`
  const incomplete = semantic.incomplete ? ` Photo indexing is incomplete, so some matches may be missing. ${coverage}` : ''
  if (!semantic.available) {
    return `${semantic.message ?? 'Intelligent photo search is unavailable. I searched filenames and dates only.'}${incomplete}`
  }
  if (semantic.candidates.length === 0) {
    return `Nothing reliably matched "${query.concepts.join(' / ')}". Here are filename and recent-photo possibilities.${incomplete}`
  }
  return `Found ${count} local photo ${count === 1 ? 'result' : 'results'} using on-device visual search. Offer to open one by its number.${incomplete}`
}

function describeOutcome(count: number, fallback: boolean, query: NormalizedSearchQuery): string {
  const imageSearch = query.kind === 'photo' || query.kind === 'screenshot'
  // Every image result carries the honest limit with it, so the spoken answer
  // can never imply Lumi recognised what is inside the pictures.
  const capability = imageSearch
    ? ' This search matched only file names, folders, and dates. You did not look inside any image and you cannot recognise people, faces, or objects. Say so plainly, and tell the user they can pick one photo for you to look at.'
    : ''

  if (count === 0) {
    return `No files were found in the approved folders for "${query.phrase}". Tell the user plainly and offer to search a different folder.${capability}`
  }

  if (fallback) {
    return `No filename matched "${query.phrase}". These are the ${count} most recent possible matches, newest first, shown to the user as possibilities. Present them as possibilities and offer to open one by its number. Do not ask the user for an exact filename.${capability}`
  }

  return `Found ${count} matching ${count === 1 ? 'file' : 'files'}, listed newest first and already shown to the user. Offer to open one by its number.${capability}`
}
