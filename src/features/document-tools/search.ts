import { createHash } from 'node:crypto'
import type { Dirent } from 'node:fs'
import { lstat, readdir, realpath, stat } from 'node:fs/promises'
import { basename, extname, isAbsolute, join, relative, resolve, sep } from 'node:path'

const DEFAULT_MAX_DEPTH = 4
const DEFAULT_MAX_RESULTS = 20
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
  /** The search stops after this many matches. */
  maxResults?: number
}

export interface DocumentSearchRecord {
  /** Stable for a canonical path while it remains under the same approved root. */
  id: string
  rootId: string
  rootPath: string
  /** Canonical filesystem path. Revalidate immediately before opening. */
  path: string
  /** Slash-separated path relative to root, suitable for stable display. */
  relativePath: string
  name: string
  extension: string
}

export interface DocumentSearchResponse {
  approvedRoots: readonly ApprovedDocumentRoot[]
  results: readonly DocumentSearchRecord[]
}

export class DocumentSearchValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DocumentSearchValidationError'
  }
}

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
 * Searches only refreshed canonical approved roots. The query must be non-empty
 * to avoid treating this narrow, user-approved operation as a folder listing.
 */
export async function searchApprovedDocuments(
  approvedRoots: readonly ApprovedDocumentRoot[],
  query: string,
  options: DocumentSearchOptions = {}
): Promise<DocumentSearchResponse> {
  const normalizedQuery = normalizeQuery(query)
  const maxDepth = normalizeBoundedInteger(options.maxDepth, DEFAULT_MAX_DEPTH, 0, HARD_MAX_DEPTH, 'maxDepth')
  const maxResults = normalizeBoundedInteger(options.maxResults, DEFAULT_MAX_RESULTS, 1, HARD_MAX_RESULTS, 'maxResults')
  const refreshedRoots = await refreshApprovedRoots(approvedRoots)
  const results: DocumentSearchRecord[] = []

  for (const root of refreshedRoots) {
    if (results.length >= maxResults) {
      break
    }

    const visitedDirectories = new Set<string>()
    await searchDirectory(root.canonicalPath, root, 0, normalizedQuery, maxDepth, maxResults, visitedDirectories, results)
  }

  return { approvedRoots: refreshedRoots, results }
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

async function searchDirectory(
  directoryPath: string,
  root: ApprovedDocumentRoot,
  depth: number,
  normalizedQuery: string,
  maxDepth: number,
  maxResults: number,
  visitedDirectories: Set<string>,
  results: DocumentSearchRecord[]
): Promise<void> {
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

  for (const entry of [...entries].sort(compareDirectoryEntries)) {
    if (results.length >= maxResults) {
      return
    }

    const candidatePath = join(canonicalDirectory, entry.name)
    const candidate = await inspectCandidate(candidatePath, root.canonicalPath)
    if (!candidate) {
      continue
    }

    if (candidate.kind === 'directory') {
      if (depth < maxDepth) {
        await searchDirectory(
          candidate.canonicalPath,
          root,
          depth + 1,
          normalizedQuery,
          maxDepth,
          maxResults,
          visitedDirectories,
          results
        )
      }
      continue
    }

    if (candidate.kind === 'file' && candidate.name.toLocaleLowerCase('en-US').includes(normalizedQuery)) {
      const relativePath = toStableRelativePath(root.canonicalPath, candidate.canonicalPath)
      results.push({
        id: stableId(`${root.id}\u0000${relativePath}`),
        rootId: root.id,
        rootPath: root.canonicalPath,
        path: candidate.canonicalPath,
        relativePath,
        name: candidate.name,
        extension: extname(candidate.name).toLocaleLowerCase('en-US')
      })
    }
  }
}

async function inspectCandidate(
  candidatePath: string,
  canonicalRootPath: string
): Promise<{ canonicalPath: string; kind: 'directory' | 'file'; name: string } | undefined> {
  try {
    // Skip symlinks/reparse points rather than traversing a mutable alias. We
    // still resolve every ordinary candidate to protect against junctions.
    const linkStats = await lstat(candidatePath)
    if (linkStats.isSymbolicLink()) {
      return undefined
    }

    const canonicalPath = await realpath(candidatePath)
    if (!isPathWithinRoot(canonicalPath, canonicalRootPath)) {
      return undefined
    }

    const candidateStats = await stat(canonicalPath)
    if (candidateStats.isDirectory()) {
      return { canonicalPath, kind: 'directory', name: basename(canonicalPath) }
    }
    if (candidateStats.isFile()) {
      return { canonicalPath, kind: 'file', name: basename(canonicalPath) }
    }
  } catch {
    // Files can disappear or become inaccessible while a search is running.
  }

  return undefined
}

async function refreshApprovedRoots(
  approvedRoots: readonly ApprovedDocumentRoot[]
): Promise<readonly ApprovedDocumentRoot[]> {
  if (!Array.isArray(approvedRoots) || approvedRoots.length === 0) {
    throw new DocumentSearchValidationError('At least one approved folder is required.')
  }

  return canonicalizeApprovedRoots(
    approvedRoots.map((root) => (root && typeof root.canonicalPath === 'string' ? root.canonicalPath : ''))
  )
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

function normalizeQuery(query: string): string {
  if (typeof query !== 'string' || query.trim().length === 0) {
    throw new DocumentSearchValidationError('A non-empty document search query is required.')
  }
  return query.trim().toLocaleLowerCase('en-US')
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
