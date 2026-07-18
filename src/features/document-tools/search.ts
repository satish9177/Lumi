import { createHash } from 'node:crypto'
import type { Dirent } from 'node:fs'
import { lstat, readdir, realpath, stat } from 'node:fs/promises'
import { basename, extname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { classifyFileKind, type FileKind, type NormalizedSearchQuery } from '../../shared/search-query'
import { rankCandidates, recentCandidatesOfKind, type CandidateScore, type RankableCandidate } from './ranking'

const DEFAULT_MAX_DEPTH = 6
const DEFAULT_MAX_ENTRIES = 20_000
const DEFAULT_MAX_CANDIDATES = 500
const DEFAULT_MAX_RESULTS = 10
const HARD_MAX_DEPTH = 8
const HARD_MAX_RESULTS = 100

export interface ApprovedDocumentRoot {
  /** A deterministic identifier derived from the canonical folder path. */
  id: string
  /** The resolved, real filesystem path that is safe to use as this root. */
  canonicalPath: string
  /** A display label that does not affect authorization. */
  label: string
}

export interface DocumentSearchOptions {
  /** Root itself is depth 0; descendants deeper than this value are not visited. */
  maxDepth?: number
  /** Enumeration stops after visiting this many directory entries. */
  maxEntries?: number
  /** At most this many files are held for ranking. */
  maxCandidates?: number
  /** Ranked results are truncated to this many entries after ranking. */
  maxResults?: number
  /** Injectable clock so recency scoring is testable. */
  now?: () => number
}

export interface DocumentSearchRecord extends RankableCandidate, CandidateScore {
  /** Stable for a canonical path while it remains under the same approved root. */
  id: string
  rootId: string
  rootPath: string
  /** Canonical filesystem path. Revalidate immediately before opening. */
  path: string
}

export interface DocumentSearchResponse {
  approvedRoots: readonly ApprovedDocumentRoot[]
  results: readonly DocumentSearchRecord[]
  /** True when no filename was plausible and recent files are offered instead. */
  fallback: boolean
  /** Total plausible matches found before truncation. */
  totalMatches: number
  /** True when a traversal cap stopped enumeration early. */
  truncatedTraversal: boolean
}

export class DocumentSearchValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DocumentSearchValidationError'
  }
}

// Directories that only add traversal cost inside an approved folder.
const SKIPPED_DIRECTORIES = new Set([
  'node_modules', '.git', '.svn', '.cache', '$recycle.bin', 'system volume information',
  'appdata', '.venv', 'venv', '__pycache__'
])

// Transient artefacts that are never a file the user asked for.
const SKIPPED_EXTENSIONS = new Set([
  '.tmp', '.temp', '.crdownload', '.part', '.partial', '.download', '.lock', '.swp', '.swo'
])

const SKIPPED_NAMES = new Set(['thumbs.db', 'desktop.ini', '.ds_store'])

/**
 * Resolves user-selected folders once before persisting them as approved roots.
 * Missing paths and files are rejected rather than silently broadening a search.
 */
export async function canonicalizeApprovedRoots(
  selectedPaths: readonly string[]
): Promise<readonly ApprovedDocumentRoot[]> {
  if (!Array.isArray(selectedPaths) || selectedPaths.length === 0) {
    throw new DocumentSearchValidationError('At least one approved folder is required.')
  }

  const roots: ApprovedDocumentRoot[] = []

  for (const selectedPath of selectedPaths) {
    if (typeof selectedPath !== 'string' || selectedPath.trim().length === 0) {
      throw new DocumentSearchValidationError('Each approved folder must be a non-empty path.')
    }

    const canonicalPath = await resolveExistingDirectory(selectedPath)
    roots.push({
      id: stableId(canonicalPath),
      canonicalPath,
      label: basename(canonicalPath) || canonicalPath
    })
  }

  const uniqueRoots = deduplicateRoots(roots)
  return uniqueRoots.filter(
    (root, index) =>
      !uniqueRoots.some(
        (candidate, candidateIndex) => candidateIndex !== index && isPathWithinRoot(root.canonicalPath, candidate.canonicalPath)
      )
  )
}

/**
 * Searches only refreshed canonical approved roots. Enumeration collects every
 * reachable file within the traversal caps, ranking runs over the whole
 * candidate set, and truncation happens last, so the newest plausible match is
 * never lost to an early traversal cut-off.
 */
export async function searchApprovedDocuments(
  approvedRoots: readonly ApprovedDocumentRoot[],
  query: NormalizedSearchQuery,
  options: DocumentSearchOptions = {}
): Promise<DocumentSearchResponse> {
  const maxDepth = normalizeBoundedInteger(options.maxDepth, DEFAULT_MAX_DEPTH, 0, HARD_MAX_DEPTH, 'maxDepth')
  const maxEntries = normalizeBoundedInteger(options.maxEntries, DEFAULT_MAX_ENTRIES, 1, 200_000, 'maxEntries')
  const maxCandidates = normalizeBoundedInteger(options.maxCandidates, DEFAULT_MAX_CANDIDATES, 1, 5_000, 'maxCandidates')
  const maxResults = normalizeBoundedInteger(options.maxResults, DEFAULT_MAX_RESULTS, 1, HARD_MAX_RESULTS, 'maxResults')
  const nowMs = options.now?.() ?? Date.now()

  const refreshedRoots = await refreshApprovedRoots(approvedRoots)
  const budget = { entriesVisited: 0, maxEntries, truncated: false }
  const candidates: EnumeratedFile[] = []

  for (const root of refreshedRoots) {
    const visitedDirectories = new Set<string>()
    await enumerateDirectory(root.canonicalPath, root, 0, maxDepth, budget, visitedDirectories, candidates)
  }

  const ranked = rankCandidates(candidates, query, nowMs)
  const totalMatches = ranked.length
  const fallback = totalMatches === 0
  const selected = fallback
    ? recentCandidatesOfKind(candidates, query)
      .slice(0, maxResults)
      .map((candidate) => ({ ...candidate, score: 0, matchScore: 0, plausible: false }))
    : ranked.slice(0, Math.min(maxResults, maxCandidates))

  return {
    approvedRoots: refreshedRoots,
    results: selected.map(toSearchRecord),
    fallback,
    totalMatches,
    truncatedTraversal: budget.truncated
  }
}

/**
 * Resolves a selected result again immediately before a later main-process
 * open operation. This closes the normal symlink/race window as far as a
 * path-based API can; callers must never open the original unverified string.
 */
export async function resolveApprovedDocumentPath(
  candidatePath: string,
  approvedRoots: readonly ApprovedDocumentRoot[]
): Promise<string | undefined> {
  if (typeof candidatePath !== 'string' || candidatePath.trim().length === 0) {
    return undefined
  }

  const refreshedRoots = await refreshApprovedRoots(approvedRoots)
  const canonicalCandidate = await safelyRealpath(candidatePath)
  if (!canonicalCandidate || !refreshedRoots.some((root) => isPathWithinRoot(canonicalCandidate, root.canonicalPath))) {
    return undefined
  }

  try {
    const candidateStats = await stat(canonicalCandidate)
    return candidateStats.isFile() ? canonicalCandidate : undefined
  } catch {
    return undefined
  }
}

/** Uses segment-aware checks, never a string-prefix authorization check. */
export function isPathWithinRoot(candidatePath: string, canonicalRootPath: string): boolean {
  if (typeof candidatePath !== 'string' || typeof canonicalRootPath !== 'string') {
    return false
  }

  const root = normalizeForComparison(resolve(canonicalRootPath))
  const candidate = normalizeForComparison(resolve(candidatePath))
  const pathFromRoot = relative(root, candidate)

  return (
    pathFromRoot === '' ||
    (pathFromRoot !== '..' && !pathFromRoot.startsWith(`..${sep}`) && !isAbsolute(pathFromRoot))
  )
}

interface EnumeratedFile extends RankableCandidate {
  rootId: string
  rootPath: string
  path: string
}

interface TraversalBudget {
  entriesVisited: number
  maxEntries: number
  truncated: boolean
}

async function enumerateDirectory(
  directoryPath: string,
  root: ApprovedDocumentRoot,
  depth: number,
  maxDepth: number,
  budget: TraversalBudget,
  visitedDirectories: Set<string>,
  candidates: EnumeratedFile[]
): Promise<void> {
  if (budget.entriesVisited >= budget.maxEntries) {
    budget.truncated = true
    return
  }

  const canonicalDirectory = await safelyRealpath(directoryPath)
  if (!canonicalDirectory || !isPathWithinRoot(canonicalDirectory, root.canonicalPath)) {
    return
  }

  const directoryKey = normalizeForComparison(canonicalDirectory)
  if (visitedDirectories.has(directoryKey)) {
    return
  }
  visitedDirectories.add(directoryKey)

  let entries: Dirent[]
  try {
    entries = await readdir(canonicalDirectory, { withFileTypes: true })
  } catch {
    return
  }

  const subdirectories: string[] = []
  for (const entry of [...entries].sort(compareDirectoryEntries)) {
    if (budget.entriesVisited >= budget.maxEntries) {
      budget.truncated = true
      return
    }
    budget.entriesVisited += 1

    // Reparse points, junctions, and symlinks are never traversed or ranked.
    if (entry.isSymbolicLink()) {
      continue
    }

    if (entry.isDirectory()) {
      if (depth < maxDepth && !isSkippedDirectory(entry.name)) {
        subdirectories.push(join(canonicalDirectory, entry.name))
      }
      continue
    }

    if (!entry.isFile() || isSkippedFile(entry.name)) {
      continue
    }

    const candidate = await describeFile(join(canonicalDirectory, entry.name), root)
    if (candidate) {
      candidates.push(candidate)
    }
  }

  for (const subdirectory of subdirectories) {
    await enumerateDirectory(subdirectory, root, depth + 1, maxDepth, budget, visitedDirectories, candidates)
  }
}

async function describeFile(filePath: string, root: ApprovedDocumentRoot): Promise<EnumeratedFile | undefined> {
  try {
    // lstat both confirms the entry is a real file and supplies the size and
    // modification time ranking needs, without following any link.
    const details = await lstat(filePath)
    if (!details.isFile()) {
      return undefined
    }

    const name = basename(filePath)
    const relativePath = toStableRelativePath(root.canonicalPath, filePath)
    const extension = extname(name).toLocaleLowerCase('en-US')
    const parents = relativePath.split('/').slice(0, -1)

    return {
      rootId: root.id,
      rootPath: root.canonicalPath,
      path: filePath,
      relativePath,
      name,
      extension,
      kind: classifyFileKind(name, extension, parents),
      modifiedAtMs: details.mtimeMs,
      sizeBytes: details.size
    }
  } catch {
    // Files can disappear or become inaccessible while a search is running.
    return undefined
  }
}

function toSearchRecord(candidate: EnumeratedFile & CandidateScore): DocumentSearchRecord {
  return {
    id: stableId(`${candidate.rootId}::${candidate.relativePath}`),
    rootId: candidate.rootId,
    rootPath: candidate.rootPath,
    path: candidate.path,
    relativePath: candidate.relativePath,
    name: candidate.name,
    extension: candidate.extension,
    kind: candidate.kind,
    modifiedAtMs: candidate.modifiedAtMs,
    sizeBytes: candidate.sizeBytes,
    score: candidate.score,
    matchScore: candidate.matchScore,
    plausible: candidate.plausible
  }
}

function isSkippedDirectory(name: string): boolean {
  const normalized = name.toLocaleLowerCase('en-US')
  return normalized.startsWith('.') || SKIPPED_DIRECTORIES.has(normalized)
}

function isSkippedFile(name: string): boolean {
  const normalized = name.toLocaleLowerCase('en-US')
  if (SKIPPED_NAMES.has(normalized) || normalized.startsWith('~$') || normalized.startsWith('.~')) {
    return true
  }
  return SKIPPED_EXTENSIONS.has(extname(normalized))
}

async function refreshApprovedRoots(
  approvedRoots: readonly ApprovedDocumentRoot[]
): Promise<readonly ApprovedDocumentRoot[]> {
  if (!Array.isArray(approvedRoots) || approvedRoots.length === 0) {
    throw new DocumentSearchValidationError('At least one approved folder is required.')
  }

  const refreshed: ApprovedDocumentRoot[] = []
  for (const root of approvedRoots) {
    const canonicalPath = await resolveExistingDirectory(
      root && typeof root.canonicalPath === 'string' ? root.canonicalPath : ''
    )
    refreshed.push({ id: root.id, canonicalPath, label: root.label })
  }

  const seen = new Set<string>()
  return refreshed.filter((root, index) => {
    const key = normalizeForComparison(root.canonicalPath)
    if (seen.has(key)) {
      return false
    }
    seen.add(key)
    return !refreshed.some(
      (candidate, candidateIndex) =>
        candidateIndex !== index &&
        normalizeForComparison(candidate.canonicalPath) !== key &&
        isPathWithinRoot(root.canonicalPath, candidate.canonicalPath)
    )
  })
}

async function resolveExistingDirectory(candidatePath: string): Promise<string> {
  let canonicalPath: string
  try {
    canonicalPath = await realpath(candidatePath)
  } catch {
    throw new DocumentSearchValidationError(`Approved folder is unavailable: ${candidatePath}`)
  }

  try {
    const candidateStats = await stat(canonicalPath)
    if (!candidateStats.isDirectory()) {
      throw new DocumentSearchValidationError(`Approved path is not a folder: ${candidatePath}`)
    }
  } catch (error) {
    if (error instanceof DocumentSearchValidationError) {
      throw error
    }
    throw new DocumentSearchValidationError(`Approved folder is unavailable: ${candidatePath}`)
  }

  return canonicalPath
}

async function safelyRealpath(candidatePath: string): Promise<string | undefined> {
  try {
    return await realpath(candidatePath)
  } catch {
    return undefined
  }
}

function deduplicateRoots(roots: readonly ApprovedDocumentRoot[]): ApprovedDocumentRoot[] {
  const sorted = [...roots].sort((left, right) => comparePathStrings(left.canonicalPath, right.canonicalPath))
  const seen = new Set<string>()

  return sorted.filter((root) => {
    const key = normalizeForComparison(root.canonicalPath)
    if (seen.has(key)) {
      return false
    }
    seen.add(key)
    return true
  })
}

function normalizeBoundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  fieldName: string
): number {
  if (value === undefined) {
    return fallback
  }
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new DocumentSearchValidationError(`${fieldName} must be an integer between ${minimum} and ${maximum}.`)
  }
  return value
}

function toStableRelativePath(rootPath: string, candidatePath: string): string {
  return relative(rootPath, candidatePath).split(sep).join('/')
}

function stableId(value: string): string {
  return createHash('sha256').update(normalizeForComparison(value)).digest('hex').slice(0, 20)
}

function normalizeForComparison(value: string): string {
  return process.platform === 'win32' ? value.toLocaleLowerCase('en-US') : value
}

function compareDirectoryEntries(left: { name: string }, right: { name: string }): number {
  return comparePathStrings(left.name, right.name)
}

function comparePathStrings(left: string, right: string): number {
  const normalizedLeft = normalizeForComparison(left)
  const normalizedRight = normalizeForComparison(right)
  if (normalizedLeft < normalizedRight) {
    return -1
  }
  if (normalizedLeft > normalizedRight) {
    return 1
  }
  return left < right ? -1 : left > right ? 1 : 0
}
