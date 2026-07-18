import {
  classifyUserIntent,
  evaluateGuardedToolRequest,
  type ClassifiedIntent,
  type GuardedTool,
  type ToolPolicyDecision,
  type UserIntent
} from '../../shared/intent'
import { expandTerm, tokenizeText, type NormalizedSearchQuery } from '../../shared/search-query'

const INTENT_TTL_MS = 2 * 60 * 1_000

interface TrackedIntent extends ClassifiedIntent {
  notedAt: number
}

export class IntentTracker {
  private latest: TrackedIntent | undefined

  constructor(
    private readonly now: () => number = () => Date.now(),
    private readonly ttlMs = INTENT_TTL_MS
  ) {}

  noteUserRequest(request: string): ClassifiedIntent {
    const classified = classifyUserIntent(request)
    this.latest = { ...classified, notedAt: this.now() }
    return classified
  }

  currentIntent(): UserIntent {
    return this.freshIntent()?.intent ?? 'unknown'
  }

  evaluateToolRequest(toolName: GuardedTool, hasApprovedFolder: boolean): ToolPolicyDecision {
    return evaluateGuardedToolRequest(toolName, { intent: this.currentIntent(), hasApprovedFolder })
  }

  /**
   * True only when the user themselves recently asked for a stored file and the
   * requested terms actually relate to that request. Screen content or
   * model-authored text can therefore never silently start a local search.
   */
  supportsFileSearch(query: NormalizedSearchQuery): boolean {
    const latest = this.freshIntent()
    if (!latest || latest.intent !== 'local_file_search') {
      return false
    }

    const requestTokens = new Set(tokenizeText(latest.normalizedRequest))
    const queryTerms = [...query.terms, ...query.synonyms]
    if (queryTerms.some((term) => requestTokens.has(term))) {
      return true
    }

    // The classifier's own file noun counts as a match through its synonyms.
    const anchors = latest.fileQuery ? expandTerm(latest.fileQuery) : []
    return anchors.some((anchor) => queryTerms.includes(anchor))
  }

  private freshIntent(): TrackedIntent | undefined {
    if (!this.latest || this.now() - this.latest.notedAt > this.ttlMs) {
      return undefined
    }
    return this.latest
  }
}
