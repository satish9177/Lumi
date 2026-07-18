import type {
  CompactSearchResult,
  FileSearchOrigin,
  FileSearchOutcome,
  FileSearchRequest,
  FileSearchResults,
  PendingSearchResolution,
  SearchDocumentsInput
} from '../../shared/contracts'

/** The one terminal payload a held Realtime search call may ever receive. */
export interface TerminalCallResult {
  ok: boolean
  message: string
  compactResults?: CompactSearchResult[]
  resultIds?: string[]
}

/** A fail-closed search awaiting the user's explicit go-ahead, held in the UI. */
export interface SearchConfirmationRequest {
  readonly input: SearchDocumentsInput
  readonly callId?: string
}

export interface FileSearchControllerCallbacks {
  /** IPC into the main-process SearchOrchestrator, the single search choke point. */
  begin: (request: FileSearchRequest) => Promise<FileSearchOutcome>
  /** Opens the folder chooser exactly once; the resume arrives separately. */
  chooseFolder: () => Promise<void>
  /** Delivers the single terminal result to the held Realtime call. */
  completeCall: (callId: string | undefined, result: TerminalCallResult) => void
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
  constructor(private readonly callbacks: FileSearchControllerCallbacks) {}

  /**
   * Main decides whether the search runs now, waits for a folder, or needs a
   * confirmation. Every branch answers or defers the held call exactly once.
   */
  async run(input: SearchDocumentsInput, callId?: string, origin: FileSearchOrigin = 'model'): Promise<void> {
    this.callbacks.setError(undefined)
    this.callbacks.setSearching(true)
    try {
      const outcome = await this.callbacks.begin({ ...input, callId, origin })
      switch (outcome.status) {
        case 'completed':
          this.callbacks.applyResults(outcome)
          this.callbacks.completeCall(callId, {
            ok: true,
            message: outcome.message,
            compactResults: outcome.compactResults,
            resultIds: outcome.results.map((result) => result.id)
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
          this.callbacks.presentConfirmation({ input: outcome.input, callId })
          return
        case 'failed':
          this.callbacks.completeCall(callId, { ok: false, message: outcome.message })
          this.callbacks.setError(outcome.message)
          return
      }
    } catch (error) {
      const message = messageFrom(error)
      this.callbacks.completeCall(callId, { ok: false, message })
      this.callbacks.setError(message)
    } finally {
      this.callbacks.setSearching(false)
    }
  }

  /**
   * The user approved a fail-closed search. It continues as an explicit request,
   * so it is trusted and flows through the orchestrator's folder-approval path.
   */
  async confirm(request: SearchConfirmationRequest): Promise<void> {
    this.callbacks.presentConfirmation(undefined)
    await this.run(request.input, request.callId, 'user')
  }

  /** The user declined; answer the held call exactly once and stay quiet. */
  decline(request: SearchConfirmationRequest): void {
    this.callbacks.presentConfirmation(undefined)
    this.callbacks.completeCall(request.callId, { ok: false, message: 'The user declined this search.' })
    this.callbacks.setListening()
  }

  /** A held search reached its single terminal state in the main process. */
  resolve(resolution: PendingSearchResolution): void {
    this.callbacks.setSearching(false)
    if (resolution.status === 'completed') {
      this.callbacks.applyResults(resolution)
      this.callbacks.completeCall(resolution.callId, {
        ok: true,
        message: resolution.message,
        compactResults: resolution.compactResults,
        resultIds: resolution.results.map((result) => result.id)
      })
      this.callbacks.setListening()
      return
    }

    this.callbacks.completeCall(resolution.callId, { ok: false, message: resolution.message })
  }
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : 'LifeLens encountered an unexpected error.'
}
