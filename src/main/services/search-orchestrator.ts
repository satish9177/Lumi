import { randomUUID } from 'node:crypto'
import type {
  ApprovedDocumentRoot,
  FileSearchOutcome,
  FileSearchRequest,
  FileSearchResults,
  PendingSearchResolution
} from '../../shared/contracts'
import { normalizeSearchQuery, type NormalizedSearchQuery } from '../../shared/search-query'

const DEFAULT_TTL_MS = 2 * 60 * 1_000

export interface SearchTimer {
  cancel: () => void
}

export interface SearchOrchestratorDependencies {
  listRoots: () => Promise<ApprovedDocumentRoot[]>
  runSearch: (query: NormalizedSearchQuery) => Promise<FileSearchResults>
  /** True only for a fresh, user-originated local-file request that matches. */
  isTrustedIntent: (query: NormalizedSearchQuery) => boolean
  /**
   * Bounded grace for a late voice transcript to register the user's intent
   * before the request falls back to a confirmation card. It resolves when the
   * wait ends (early once trusted, or on timeout); begin() re-checks
   * isTrustedIntent afterwards. Omitted callers fail closed immediately.
   */
  waitForTrust?: (query: NormalizedSearchQuery) => Promise<void>
  /** Delivers the single terminal result for a held Realtime call. */
  emit: (resolution: PendingSearchResolution) => void
  now?: () => number
  schedule?: (callback: () => void, delayMs: number) => SearchTimer
  ttlMs?: number
}

/** Immutable once created; a change of query always creates a new record. */
export interface PendingSearch {
  readonly id: string
  readonly callId?: string
  readonly query: NormalizedSearchQuery
  readonly createdAt: number
  readonly expiresAt: number
}

/**
 * Owns the "search asked for before a folder exists" flow. A model-driven
 * search whose folder is missing is retained here, unanswered, until the user
 * approves a folder; then it resumes on its own. Every pending search reaches
 * exactly one terminal state, and a stale query is never executed.
 */
export class SearchOrchestrator {
  private pending: PendingSearch | undefined
  private timer: SearchTimer | undefined
  private readonly now: () => number
  private readonly schedule: (callback: () => void, delayMs: number) => SearchTimer
  private readonly ttlMs: number

  constructor(private readonly dependencies: SearchOrchestratorDependencies) {
    this.now = dependencies.now ?? (() => Date.now())
    this.schedule = dependencies.schedule ?? defaultSchedule
    this.ttlMs = dependencies.ttlMs ?? DEFAULT_TTL_MS
  }

  pendingSearch(): PendingSearch | undefined {
    return this.pending
  }

  async begin(request: FileSearchRequest): Promise<FileSearchOutcome> {
    let query: NormalizedSearchQuery
    try {
      query = normalizeSearchQuery(request)
    } catch (error) {
      return { status: 'failed', message: error instanceof Error ? error.message : 'That search request is not valid.' }
    }

    // A newer request always wins; the older call is closed out first.
    this.resolvePending('superseded', 'A newer search replaced this one.')

    if (!(await this.isTrusted(request, query))) {
      // The Phase-2 constraints travel with the confirmation, so approving the
      // card runs the search the user actually asked for rather than a broader
      // one that quietly dropped the text or people filter.
      return {
        status: 'needs_confirmation',
        input: {
          queryTerms: query.phrase,
          kind: query.kind,
          recency: query.recency,
          ...(query.containsText ? { containsText: query.containsText } : {}),
          ...(query.people ? { people: query.people } : {})
        }
      }
    }

    const roots = await this.dependencies.listRoots()
    if (roots.length === 0) {
      return { status: 'awaiting_folder', pendingId: this.createPending(query, request.callId).id }
    }

    return this.execute(query)
  }

  /**
   * Folder approval is consent to search that folder, but only a request the
   * user actually made may search without a confirmation card. A model request
   * whose fresh intent has not landed yet is given a brief, bounded chance for a
   * late voice transcript to register before it fails closed.
   */
  private async isTrusted(request: FileSearchRequest, query: NormalizedSearchQuery): Promise<boolean> {
    if (request.origin === 'user' || this.dependencies.isTrustedIntent(query)) {
      return true
    }
    if (this.dependencies.waitForTrust) {
      await this.dependencies.waitForTrust(query)
      return this.dependencies.isTrustedIntent(query)
    }
    return false
  }

  /**
   * Resumes the retained search after the user approves a folder. The original
   * request is never re-asked and the original call receives its one result.
   */
  async notifyFolderApproved(): Promise<void> {
    const pending = this.takeFreshPending()
    if (!pending) {
      return
    }

    try {
      const results = await this.dependencies.runSearch(pending.query)
      this.dependencies.emit({ status: 'completed', callId: pending.callId, ...results })
    } catch (error) {
      this.dependencies.emit({
        status: 'failed',
        callId: pending.callId,
        message: error instanceof Error ? error.message : 'That approved-folder search could not be completed.'
      })
    }
  }

  notifyFolderDeclined(): void {
    this.resolvePending('declined', 'The user did not approve a folder, so no search ran.')
  }

  /** Expiry sweep. Safe to call at any time. */
  sweep(): void {
    if (this.pending && this.pending.expiresAt <= this.now()) {
      this.resolvePending('expired', 'That search request timed out before a folder was approved.')
    }
  }

  /** Disconnect or shutdown: drop silently, since no channel remains to answer. */
  clear(): void {
    this.timer?.cancel()
    this.timer = undefined
    this.pending = undefined
  }

  private async execute(query: NormalizedSearchQuery): Promise<FileSearchOutcome> {
    try {
      const results = await this.dependencies.runSearch(query)
      return { status: 'completed', ...results }
    } catch (error) {
      return {
        status: 'failed',
        message: error instanceof Error ? error.message : 'That approved-folder search could not be completed.'
      }
    }
  }

  private createPending(query: NormalizedSearchQuery, callId?: string): PendingSearch {
    const createdAt = this.now()
    const pending: PendingSearch = Object.freeze({
      id: randomUUID(),
      callId,
      query,
      createdAt,
      expiresAt: createdAt + this.ttlMs
    })

    this.pending = pending
    this.timer = this.schedule(() => this.sweep(), this.ttlMs)
    return pending
  }

  /** Consumes the pending search, refusing to run one that already expired. */
  private takeFreshPending(): PendingSearch | undefined {
    const pending = this.pending
    if (!pending) {
      return undefined
    }

    if (pending.expiresAt <= this.now()) {
      this.resolvePending('expired', 'That search request timed out before a folder was approved.')
      return undefined
    }

    this.timer?.cancel()
    this.timer = undefined
    this.pending = undefined
    return pending
  }

  private resolvePending(status: 'declined' | 'expired' | 'superseded', message: string): void {
    const pending = this.pending
    if (!pending) {
      return
    }

    this.timer?.cancel()
    this.timer = undefined
    this.pending = undefined
    this.dependencies.emit({ status, callId: pending.callId, message })
  }
}

function defaultSchedule(callback: () => void, delayMs: number): SearchTimer {
  const timer = setTimeout(callback, delayMs)
  timer.unref?.()
  return { cancel: () => clearTimeout(timer) }
}
