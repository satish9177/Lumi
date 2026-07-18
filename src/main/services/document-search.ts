import {
  canonicalizeApprovedRoots,
  searchApprovedDocuments,
  type ApprovedDocumentRoot as SearchRoot
} from '../../features/document-tools/search'
import type { CompactSearchResult, DocumentSearchResult, FileSearchResults } from '../../shared/contracts'
import { formatModifiedAgo, type NormalizedSearchQuery } from '../../shared/search-query'
import type { LocalStore, SearchResultInput } from './store'

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
  now: () => number = () => Date.now()
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
  const stored = await store.saveSearchResults(search.results.map(toSearchResultInput))
  const compactResults = toCompactResults(stored, search.results.map((result) => result.modifiedAtMs), now())

  return {
    results: stored,
    compactResults,
    fallback: search.fallback,
    message: describeOutcome(stored.length, search.fallback, query)
  }
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
    modifiedAgo: formatModifiedAgo(modifiedAtMs[index] ?? Date.parse(result.modifiedAt), nowMs)
  }))
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
