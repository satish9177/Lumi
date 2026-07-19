import type {
  CompactSearchResult,
  FileSearchOrigin,
  FileSearchOutcome,
  FileSearchRequest,
  FileSearchResults,
  PendingSearchResolution,
  SearchDocumentsInput
} from '../../shared/contracts'
import { messageFrom } from './error-message'
import type { RealtimeServerCall } from './realtime'

/** The one terminal payload a held Realtime search call may ever receive. */
export interface TerminalCallResult {
  ok: boolean
  message: string
  compactResults?: CompactSearchResult[]
  resultIds?: string[]
  /** Full trusted result count remains local; renderer uses it to bound narration. */
  resultCount?: number
}

/** A fail-closed search awaiting the user's explicit go-ahead, held in the UI. */
export interface SearchConfirmationRequest {
  readonly input: SearchDocumentsInput
  readonly serverCall?: RealtimeServerCall
}

export interface FileSearchControllerCallbacks {
  /** IPC into the main-process SearchOrchestrator, the single search choke point. */
  begin: (request: FileSearchRequest) => Promise<FileSearchOutcome>
  /** Opens the folder chooser exactly once; the resume arrives separately. */
  chooseFolder: () => Promise<void>
  /** Delivers the single terminal result to the held Realtime call. */
  completeCall: (serverCall: RealtimeServerCall | undefined, result: TerminalCallResult) => void
  /** Shows trusted local results in the panel. */
  applyResults: (results: FileSearchResults) => void
  /** Presents (or, with undefined, clears) the search-specific confirmation. */
  presentConfirmation: (request: SearchConfirmationRequest | undefined) => void
  setSearching: (searching: boolean) => void
  setError: (message: string | undefined) => void
  /** Returns the companion to its idle listening state. */
  setListening: () => void
}

/**
 * Owns every stored-file search on the renderer side: model tool calls, mock
 * voice, and the panel field. It is the reason a folderless search never lands
 * on the generic create-pending-action path, which assumes an approved root.
 *
 * A search that main cannot yet trust returns needs_confirmation; that is routed
 * to a search-specific confirmation card whose approval re-enters the same
 * orchestrator as an explicit user request, so the fail-closed path still flows
 * through SearchOrchestrator (never the root-assuming pending action) and still
 * opens the folder chooser and resumes when no folder exists.
 */
export class FileSearchController {
  private readonly serverCallsById = new Map<string, RealtimeServerCall>()
  private readonly requestIdsByCorrelation = new Map<string, number>()
  private readonly expiredGenerations = new Set<number>()
  private nextRequestId = 0
  private activeSearchingRequestId: number | undefined

  constructor(private readonly callbacks: FileSearchControllerCallbacks) {}

  /**
   * Main decides whether the search runs now, waits for a folder, or needs a
   * confirmation. Every branch answers or defers the held call exactly once.
   */
  async run(input: SearchDocumentsInput, serverCall?: RealtimeServerCall, origin: FileSearchOrigin = 'model'): Promise<void> {
    if (serverCall && this.expiredGenerations.has(serverCall.generation)) {
      return
    }
    const requestId = ++this.nextRequestId
    if (serverCall) {
      const correlationId = searchCorrelationId(serverCall)
      this.serverCallsById.set(correlationId, serverCall)
      this.requestIdsByCorrelation.set(correlationId, requestId)
    }
    this.activeSearchingRequestId = requestId
    this.callbacks.setError(undefined)
    this.callbacks.setSearching(true)
    try {
      const outcome = await this.callbacks.begin({ ...input, callId: serverCall ? searchCorrelationId(serverCall) : undefined, origin })
      if (serverCall && !this.isTracked(serverCall)) {
        return
      }
      switch (outcome.status) {
        case 'completed':
          this.callbacks.applyResults(outcome)
          this.completeServerCall(serverCall, {
            ok: true,
            message: outcome.message,
            compactResults: outcome.compactResults,
            resultIds: outcome.results.map((result) => result.id),
            resultCount: outcome.results.length
          })
          this.callbacks.setListening()
          return
        case 'awaiting_folder':
          // Main holds the original request; approving a folder resumes it and
          // answers the call, so nothing is completed here.
          await this.callbacks.chooseFolder()
          return
        case 'needs_confirmation':
          // Never the generic pending action, which requires an approved root:
          // a search-specific confirmation re-enters the orchestrator on approval.
          this.callbacks.presentConfirmation({ input: outcome.input, serverCall })
          return
        case 'failed':
          this.completeServerCall(serverCall, { ok: false, message: outcome.message })
          this.callbacks.setError(outcome.message)
          return
      }
    } catch (error) {
      if (serverCall && !this.isTracked(serverCall)) {
        return
      }
      const message = messageFrom(error)
      this.completeServerCall(serverCall, { ok: false, message })
      this.callbacks.setError(message)
    } finally {
      this.finishSearching(requestId)
    }
  }

  /**
   * The user approved a fail-closed search. It continues as an explicit request,
   * so it is trusted and flows through the orchestrator's folder-approval path.
   */
  async confirm(request: SearchConfirmationRequest): Promise<void> {
    if (request.serverCall && this.expiredGenerations.has(request.serverCall.generation)) {
      return
    }
    this.callbacks.presentConfirmation(undefined)
    await this.run(request.input, request.serverCall, 'user')
  }

  /** The user declined; answer the held call exactly once and stay quiet. */
  decline(request: SearchConfirmationRequest): void {
    if (request.serverCall && this.expiredGenerations.has(request.serverCall.generation)) {
      return
    }
    this.callbacks.presentConfirmation(undefined)
    this.completeServerCall(request.serverCall, { ok: false, message: 'The user declined this search.' })
    this.callbacks.setListening()
  }

  /** A held search reached its single terminal state in the main process. */
  resolve(resolution: PendingSearchResolution): void {
    const serverCall = resolution.callId ? this.serverCallsById.get(resolution.callId) : undefined
    if (resolution.callId && !serverCall) {
      return
    }
    const requestId = resolution.callId
      ? this.requestIdsByCorrelation.get(resolution.callId)
      : this.activeSearchingRequestId
    if (requestId !== undefined) {
      this.finishSearching(requestId)
    }
    if (resolution.status === 'completed') {
      this.callbacks.applyResults(resolution)
      this.completeServerCall(serverCall, {
        ok: true,
        message: resolution.message,
        compactResults: resolution.compactResults,
        resultIds: resolution.results.map((result) => result.id),
        resultCount: resolution.results.length
      })
      this.callbacks.setListening()
      return
    }

    this.completeServerCall(serverCall, { ok: false, message: resolution.message })
  }

  /** Prevents an ended Realtime generation from receiving later search results. */
  expireGeneration(generation: number): boolean {
    this.expiredGenerations.add(generation)
    let expired = false
    const expiredRequestIds: number[] = []
    for (const [callId, serverCall] of this.serverCallsById) {
      if (serverCall.generation === generation) {
        const requestId = this.requestIdsByCorrelation.get(callId)
        if (requestId !== undefined) {
          expiredRequestIds.push(requestId)
        }
        this.serverCallsById.delete(callId)
        this.requestIdsByCorrelation.delete(callId)
        expired = true
      }
    }
    for (const requestId of expiredRequestIds) {
      this.finishSearching(requestId)
    }
    return expired
  }

  private completeServerCall(serverCall: RealtimeServerCall | undefined, result: TerminalCallResult): void {
    if (serverCall) {
      const correlationId = searchCorrelationId(serverCall)
      this.serverCallsById.delete(correlationId)
      this.requestIdsByCorrelation.delete(correlationId)
    }
    this.callbacks.completeCall(serverCall, result)
  }

  private isTracked(serverCall: RealtimeServerCall): boolean {
    return this.serverCallsById.get(searchCorrelationId(serverCall)) === serverCall
  }

  private finishSearching(requestId: number): void {
    if (this.activeSearchingRequestId !== requestId) {
      return
    }
    this.activeSearchingRequestId = undefined
    this.callbacks.setSearching(false)
  }
}

function searchCorrelationId(serverCall: RealtimeServerCall): string {
  return `${serverCall.generation}:${serverCall.callId}`
}
