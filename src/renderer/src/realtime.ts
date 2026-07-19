import {
  extractSignals,
  parseToolProposal,
  type ApprovedDocumentRoot,
  type ApprovedImagePayload,
  type CaptureResult,
  type CompactSearchResult,
  type CompanionState,
  type Explanation,
  type RealtimeSessionCredential,
  type SearchDocumentsInput,
  type SourceContext,
  type ToolExecutionResult,
  type ToolName,
  type ToolProposal
} from '../../shared/contracts'
import { isSearchKind, isSearchRecency } from '../../shared/search-query'
import {
  classifyUserIntent,
  evaluateGuardedToolRequest,
  type GuardedTool,
  type ToolPolicyDecision
} from '../../shared/intent'

export interface RealtimeServerCall {
  readonly callId: string
  readonly generation: number
}

export interface TelegramAttachmentCoordinationRequest {
  fileResultId: string
  recipientQuery: string
  caption?: string
  reason: string
}

interface RealtimeCallbacks {
  onState: (state: CompanionState) => void
  onTranscript: (text: string) => void
  onExplanation: (explanation: Explanation) => void
  onCaptureContextRequest: (serverCall?: RealtimeServerCall) => void
  onFileSearchRequest: (request: SearchDocumentsInput, serverCall?: RealtimeServerCall) => void
  /** Completed speech, forwarded so the trusted intent tracker sees it. */
  onUserTranscript?: (text: string) => Promise<void> | void
  onTelegramRecipientSearch?: (query: string, serverCall: RealtimeServerCall) => void
  onTelegramAttachmentRequest?: (request: TelegramAttachmentCoordinationRequest, serverCall: RealtimeServerCall) => void
  onToolProposal: (proposal: ToolProposal, serverCall?: RealtimeServerCall) => void
  onError: (message: string) => void
  onSessionEnded?: (reason: 'idle' | 'collapsed' | 'error', generation: number) => void
  evaluateToolPolicy?: (toolName: GuardedTool) => Promise<ToolPolicyDecision>
}

const CAPTURE_CONTEXT_TOOL = 'capture_screen_context'
const TELEGRAM_RECIPIENT_SEARCH_TOOL = 'telegram_search_recipients'
const TELEGRAM_ATTACHMENT_TOOL = 'telegram_send_attachment'
const SCREEN_CONTEXT_TTL_MS = 10 * 60 * 1_000
export const COLLAPSE_DISCONNECT_MS = 60_000
export const IDLE_DISCONNECT_MS = 4 * 60_000
export const MAX_PENDING_WORK_EXTENSION_MS = 2 * 60_000
const INPUT_TRANSCRIPTION_MODEL = 'gpt-4o-mini-transcribe'
// Tuned conservatively for a laptop microphone. These are operational knobs,
// not universal constants: retain genuine barge-in while rejecting more room
// noise than the Realtime defaults.
const SERVER_TURN_DETECTION = {
  type: 'server_vad',
  threshold: 0.7,
  prefix_padding_ms: 300,
  silence_duration_ms: 650,
  create_response: true,
  interrupt_response: true
} as const
const RESPONSE_BUDGETS = {
  confirmation: 192,
  searchResults: 512,
  normal: 512,
  longForm: 2048
} as const
const MAX_NARRATED_SEARCH_RESULTS = 3
const MAX_NARRATED_FILENAME_LENGTH = 96
export const LAPTOP_MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true
  }
} as const
const LONG_FORM_CUE = /\b(?:explain in detail|in detail|detailed|article|story|summari[sz]e (?:this|the) (?:page|article|screen)|walk me through|step by step)\b/i
let nextRealtimeSessionGeneration = 0

const SYSTEM_INSTRUCTIONS = [
  'You are LifeLens, a concise, supportive floating desktop companion.',
  'Explain captured screen content in simple English; use Telugu-English only if the user does.',
  'State important dates, links, and concrete next actions plainly.',
  'Every function is only a proposal. Never claim an action was performed until the application returns its result.',
  'Never ask for, invent, or repeat a local file path, folder name, or folder identifier. LifeLens chooses the folders.',
  'search_documents finds stored files, such as a resume, CV, PDF, certificate, photo, or screenshot, inside the folders the user has approved. When the user wants to find, locate, search for, or open a stored file, call search_documents immediately as your first action.',
  'Give search_documents one to three useful topic words from the user\'s own request, such as "resume" or "offer letter". Do not include words like my, latest, or file.',
  'Never ask the user for an exact filename or which folder to search before calling search_documents. Call it even when no folder is approved yet: LifeLens asks the user to approve a folder and then runs your search automatically.',
  'LifeLens shows the complete matching-file list in the UI. After a search result, state the total result count, mention at most the first three returned names, say the complete list is visible in the UI, and offer to hear more when there are additional results. Refer to results only by their number and name, and offer to open one with open_file.',
  'For photo requests, search_documents can use local visual concept search over photos already indexed on this device. Put up to three short concepts copied from the user request in concepts. Photo bytes and embeddings never reach you.',
  'Local visual search does not support OCR, reading document text inside photos, counting people, or recognising a person\'s identity or face. Never claim those capabilities.',
  'If indexing is incomplete or a result is described as a filename-only possibility, repeat that limitation plainly. Do not claim a weak result depicts the requested concept.',
  'Selected-photo cloud analysis is separate from local indexing/search and happens only after the user explicitly confirms one photo. Never invoke or imply it happened automatically.',
  'When the user explicitly chooses one photo, LifeLens sends you that single image. Answer their question about it, and answer later follow-ups from that same image without asking for it again.',
  'If the result list is described as recent possibilities rather than matches, say so honestly and offer the numbered options instead of asking for a filename.',
  'capture_screen_context only inspects content already visible on the user\'s screen, such as "this email", "this image", "this page", "this error", or "what is on my screen". It is never a fallback for finding stored files.',
  'If a document request such as "check my resume" does not say whether the document is visible on screen or stored in a folder, ask exactly: Should I inspect the resume currently visible, or find it in your approved folder? Substitute the document the user named.',
  'Telegram contact and dialog metadata are local-only. You may request a local recipient search from the user\'s spoken recipient name, but never receive, repeat, or infer Telegram names, usernames, phone numbers, peer identifiers, or search results.',
  'To send one already-found local photo or document, call telegram_send_attachment. Refer to the file only as selected or by its current result number. Never provide a filename, path, file identifier, recipient identifier, peer, MIME type, or bytes.',
  'For a named file such as my latest resume, call search_documents first with query_terms resume, kind document, and recency latest. Never auto-select a fallback recent possibility; ask for its result number.',
  'Do not capture a screen when the panel opens or during a general greeting.',
  'When a user request needs visible-screen context and there is no current screen context, call capture_screen_context once. The user making that screen-relative request is consent for this one-time capture.',
  'When a current screen context is available, answer follow-up questions from it and do not call capture_screen_context again unless the user asks to refresh, says the screen changed, refers to a new visible item, or you cannot answer reliably.',
  'If it is unclear whether the user means their screen and no stored document is involved, ask exactly: Should I look at your screen?',
  'When analyzing a capture, focus on visible page or document content. Ignore browser tabs, address bars, bookmarks, taskbars, and window chrome.',
  'For simple requests, answer naturally in one or two short sentences.',
  'For article, screen, story, or explicitly detailed requests, give a complete structured explanation without omitting necessary context.',
  'Do not repeat the user\'s question.'
].join(' ')

const TOOL_DEFINITIONS = [
  {
    type: 'function',
    name: CAPTURE_CONTEXT_TOOL,
    description: 'Internally request one user-approved screenshot of the currently selected screen or window. Use only when the user asks about content already visible on screen, such as "this email", "this page", or "what is on my screen", and no usable context is already available. Never use it to find, locate, or open stored files; use search_documents for that. Do not use for greetings or general questions.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {}
    }
  },
  {
    type: 'function',
    name: 'create_reminder',
    description: 'Propose a reminder for a date or next action visible in the current screen context. The user must confirm it before it is saved.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        title: { type: 'string', description: 'Short reminder title.' },
        due_at: { type: 'string', description: 'ISO 8601 date-time for the reminder.' },
        reason: { type: 'string', description: 'Why this reminder is useful.' }
      },
      required: ['title', 'due_at', 'reason']
    }
  },
  {
    type: 'function',
    name: 'search_documents',
    description: 'Find stored files, such as a resume, CV, PDF, certificate, photo, or screenshot, inside the folders the user has approved. Call this immediately whenever the user wants to find, locate, search for, or open a stored file. It is safe to call when no folder is approved yet: LifeLens requests approval once and then runs this search automatically. Never ask for a filename or a folder first.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query_terms: { type: 'string', description: 'One to three topic words from the user\'s request, such as "resume" or "offer letter". No paths, no words like my or latest.' },
        kind: { type: 'string', enum: ['document', 'photo', 'screenshot', 'any'], description: 'The kind of file the user asked for, when they said.' },
        recency: { type: 'string', enum: ['latest', 'any'], description: 'Use latest when the user asked for the latest, newest, or most recent one.' },
        concepts: { type: 'array', minItems: 1, maxItems: 3, items: { type: 'string', maxLength: 64 }, description: 'For visual photo search only: short concepts copied from the user request, such as beach or birthday.' },
        reason: { type: 'string', description: 'Why this approved-folder search helps.' }
      },
      required: ['query_terms', 'reason']
    }
  },
  {
    type: 'function',
    name: 'open_file',
    description: 'Propose opening one numbered result from the most recent search. The user must confirm it before anything opens.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        ordinal: { type: 'integer', minimum: 1, maximum: 5, description: 'The number of the result to open, as listed to you.' },
        reason: { type: 'string', description: 'Why this file should be opened.' }
      },
      required: ['ordinal', 'reason']
    }
  },
  {
    type: 'function',
    name: 'open_url',
    description: 'Propose opening an http or https link visible in the current context. The user must confirm it.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        url: { type: 'string', description: 'The exact http or https link to open.' },
        reason: { type: 'string', description: 'Why this link should be opened.' }
      },
      required: ['url', 'reason']
    }
  },
  {
    type: 'function',
    name: 'save_context',
    description: 'Propose saving the minimal current screen context for later reference. The user must confirm it.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        label: { type: 'string', description: 'A short label for the saved context.' },
        reason: { type: 'string', description: 'Why preserving this context helps.' }
      },
      required: ['label', 'reason']
    }
  },
  {
    type: 'function',
    name: TELEGRAM_ATTACHMENT_TOOL,
    description: 'Coordinate sending exactly one already-found local photo or document through the connected personal Telegram account. This is only a request signal; LifeLens resolves both trusted identifiers locally and shows one final confirmation.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        attachment: { type: 'string', enum: ['selected', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10'], description: 'Use selected for the current trusted selection, or a numbered result string from 1 through 10.' },
        recipient_query: { type: 'string', description: 'Only the spoken recipient name from the user request.' },
        caption: { type: 'string', maxLength: 1024, description: 'Optional complete caption, unchanged.' },
        reason: { type: 'string', description: 'Why this one attachment should be proposed.' }
      },
      required: ['attachment', 'recipient_query', 'reason']
    }
  },
  {
    type: 'function',
    name: TELEGRAM_RECIPIENT_SEARCH_TOOL,
    description: 'Request a local-only recipient lookup using the name in the user\'s own request. Recipient metadata and identifiers stay in LifeLens and are never returned to you. The user selects a recipient locally before any message can be proposed.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string', description: 'The recipient name from the user\'s request.' }
      },
      required: ['query']
    }
  },
  {
    type: 'function',
    name: 'send_telegram_message',
    description: 'Propose sending one plain-text Telegram message to an opaque recipient result identifier selected locally by the user. The user must confirm it before it sends. Never use a username, phone number, chat ID, or raw peer.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        recipient_result_id: { type: 'string', description: 'An opaque recipient result identifier supplied by LifeLens after local selection.' },
        message: { type: 'string', description: 'The complete plain-text message to send.' },
        reason: { type: 'string', description: 'Why this message is being proposed.' }
      },
      required: ['recipient_result_id', 'message', 'reason']
    }
  }
]

export class RealtimeClient {
  private dataChannel: RTCDataChannel | undefined
  private peerConnection: RTCPeerConnection | undefined
  private localAudio: MediaStream | undefined
  private remoteAudio: HTMLAudioElement | undefined
  private currentCapture: CaptureResult | undefined
  private currentExplanation: Explanation | undefined
  private textBuffer = ''
  private responseActive = false
  private connected = false
  private mode: 'live' | 'mock' = 'mock'
  private approvedRoots: ApprovedDocumentRoot[] = []
  private readonly completedCallIds = new Set<string>()
  private readonly answeredCallIds = new Set<string>()
  private readonly pendingCallGenerations = new Map<string, number>()
  private lastUserRequest = ''
  private awaitingInitialSessionUpdate = false
  private greetAfterInitialSessionUpdate = true
  private listening = true
  private idleTimer: number | undefined
  private collapseTimer: number | undefined
  private deferredDisconnectTimer: number | undefined
  private deferredDisconnectReason: 'idle' | 'collapsed' | undefined
  private deferredDisconnectDeadline: number | undefined
  private activeGeneration: number | undefined
  private dataChannelGeneration: number | undefined
  private lastSentInstructions: string | undefined
  /** The one photo the user approved for this session, if any. */
  private selectedPhoto: { resultId: string; name: string } | undefined
  /** Ordinal-to-result mapping stays local; the model only ever sees numbers. */
  private resultOrdinals: string[] = []
  private resultContext: Array<{ resultId: string; kind: 'document' | 'photo' | 'screenshot' | 'other' }> = []
  private latestSearchFallback = false
  private lastOpenedResult: { resultId: string; kind: 'document' | 'photo' | 'screenshot' | 'other' } | undefined
  /** Serializes transcript-driven intent updates ahead of guarded tool calls. */
  private intentUpdate: Promise<void> = Promise.resolve()

  constructor(private readonly callbacks: RealtimeCallbacks) {}

  async connect(credential: RealtimeSessionCredential, options: { greet?: boolean } = {}): Promise<void> {
    this.disconnect()
    this.mode = credential.mode
    this.greetAfterInitialSessionUpdate = options.greet ?? true

    if (credential.mode === 'mock') {
      this.connected = true
      const greeting = 'Hi, I am LifeLens. I am ready to look at a screen with you.'
      this.callbacks.onTranscript(greeting)
      this.callbacks.onState('speaking')
      this.speakMock(greeting)
      return
    }

    if (!credential.token) {
      throw new Error('LifeLens received an incomplete Realtime credential.')
    }

    const generation = ++nextRealtimeSessionGeneration
    this.activeGeneration = generation
    try {
      await this.connectLive(credential.token, generation)
    } catch (error) {
      this.disconnect()
      throw error
    }
  }

  setApprovedRoots(roots: ApprovedDocumentRoot[]): void {
    this.approvedRoots = roots
    this.updateLiveSessionInstructions()
  }

  isConnected(): boolean {
    return this.connected
  }

  isLiveConnected(): boolean {
    return this.mode === 'live' && this.connected
  }

  getActiveSessionGeneration(): number | undefined {
    return this.activeGeneration
  }

  isServerCallActive(serverCall: RealtimeServerCall): boolean {
    return this.isServerCallCurrent(serverCall) &&
      this.pendingCallGenerations.get(serverCall.callId) === serverCall.generation &&
      this.dataChannel?.readyState === 'open'
  }

  /** Starts the one absolute collapse deadline; repeated calls never extend it. */
  startCollapseDisconnect(collapsedAt = Date.now()): void {
    if (!this.isLiveConnected() || this.collapseTimer !== undefined || this.deferredDisconnectReason === 'collapsed') {
      return
    }
    const normalDeadline = collapsedAt + COLLAPSE_DISCONNECT_MS
    this.collapseTimer = window.setTimeout(() => {
      this.collapseTimer = undefined
      this.disconnectOrDefer('collapsed', normalDeadline + MAX_PENDING_WORK_EXTENSION_MS)
    }, Math.max(0, normalDeadline - Date.now()))
  }

  cancelCollapseDisconnect(): void {
    if (this.collapseTimer !== undefined) {
      window.clearTimeout(this.collapseTimer)
      this.collapseTimer = undefined
    }
    if (this.deferredDisconnectReason === 'collapsed') {
      this.clearDeferredDisconnect()
    }
    this.touchActivity()
  }

  /**
   * Stops or restores microphone streaming. Collapsing the companion must never
   * leave an ambient open microphone behind the orb.
   */
  setListening(enabled: boolean): void {
    this.listening = enabled
    this.localAudio?.getAudioTracks().forEach((track) => {
      track.enabled = enabled
    })

    if (this.mode === 'live' && this.dataChannel?.readyState === 'open') {
      this.sendEvent({
        type: 'session.update',
        session: { type: 'realtime', audio: { input: { turn_detection: enabled ? SERVER_TURN_DETECTION : null } } }
      })
    }
  }

  isListening(): boolean {
    return this.listening
  }

  /** Maps the numbers shown to the model onto local result identifiers. */
  setSearchOrdinals(resultIds: readonly string[]): void {
    this.resultOrdinals = [...resultIds]
  }

  setSearchResults(results: ReadonlyArray<{ id: string; kind: 'document' | 'photo' | 'screenshot' | 'other' }>, fallback = false): void {
    this.resultContext = results.map((result) => ({ resultId: result.id, kind: result.kind }))
    this.resultOrdinals = this.resultContext.map((result) => result.resultId)
    this.latestSearchFallback = fallback
    this.lastOpenedResult = undefined
  }

  recordOpenedResult(resultId: string): void {
    const result = this.resultContext.find((candidate) => candidate.resultId === resultId)
    if (result) this.lastOpenedResult = result
  }

  /**
   * Sends the one image the user approved, through the existing image-input
   * path. Choosing another photo replaces this context; follow-up questions
   * reuse the image already in the conversation rather than uploading again.
   */
  async analyzeSelectedPhoto(image: ApprovedImagePayload, question: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before asking Lumi about a photo.')
    }

    const request = question.trim() || 'What is in this photo?'
    this.touchActivity()
    this.selectedPhoto = { resultId: image.resultId, name: image.name }
    this.lastUserRequest = request
    this.callbacks.onState('thinking')

    if (this.mode === 'mock') {
      this.callbacks.onTranscript(`Demo mode: Lumi would look at ${image.name} and answer "${request}".`)
      this.callbacks.onState('listening')
      return
    }

    if (this.responseActive) {
      this.sendEvent({ type: 'response.cancel' })
      this.responseActive = false
    }

    this.updateLiveSessionInstructions()
    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [
          { type: 'input_text', text: `${request} The user selected this one photo, named ${image.name}, for you to look at.` },
          { type: 'input_image', image_url: image.dataUrl, detail: 'low' }
        ]
      }
    })
    this.sendEvent({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: pickResponseBudget('long-form') }
    })
  }

  hasSelectedPhoto(): boolean {
    return this.selectedPhoto !== undefined
  }

  clearSelectedPhoto(): void {
    if (!this.selectedPhoto) {
      return
    }
    this.selectedPhoto = undefined
    this.updateLiveSessionInstructions()
  }

  /**
   * Returns one terminal result for a held search call. The model receives only
   * ordinals, filenames, and coarse ages; identifiers and paths stay local.
   */
  completeFileSearch(
    serverCall: RealtimeServerCall | undefined,
    result: { ok: boolean; message: string; compactResults?: CompactSearchResult[]; resultIds?: string[]; resultCount?: number }
  ): void {
    if (serverCall && !this.isServerCallActive(serverCall)) {
      return
    }

    this.touchActivity()
    if (result.resultIds) {
      this.setSearchOrdinals(result.resultIds)
    }
    if (!serverCall) {
      return
    }

    const key = serverCallKey(serverCall)
    if (this.answeredCallIds.has(key)) {
      return
    }
    this.answeredCallIds.add(key)
    this.sendFunctionCallOutput(serverCall, createSearchNarrationResult(result), true, 'search-results')
  }

  async sendUserRequest(request: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before asking Lumi a question.')
    }

    const trimmedRequest = request.trim()
    if (!trimmedRequest) {
      return
    }

    this.touchActivity()
    this.lastUserRequest = trimmedRequest
    if (this.mode === 'mock') {
      this.handleMockUserRequest(trimmedRequest)
      return
    }

    if (this.responseActive) {
      this.sendEvent({ type: 'response.cancel' })
      this.responseActive = false
    }
    this.callbacks.onState('thinking')
    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: trimmedRequest }]
      }
    })
    this.sendEvent({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: pickResponseBudget('question', trimmedRequest) }
    })
  }

  private handleMockUserRequest(request: string): void {
    if (this.hasActiveScreenContext()) {
      this.callbacks.onTranscript(this.currentExplanation?.summary ?? 'I will use the screen context already captured for this conversation.')
      this.callbacks.onState('listening')
      return
    }

    const classified = classifyUserIntent(request)
    if (classified.intent === 'visible_screen_question') {
      this.callbacks.onCaptureContextRequest()
      return
    }

    if (classified.intent === 'local_file_search') {
      // Mock mode takes the same orchestrated path as live voice, including
      // folder approval and automatic resume.
      this.callbacks.onFileSearchRequest({ queryTerms: classified.fileQuery ?? request })
      this.callbacks.onState('listening')
      return
    }

    this.callbacks.onTranscript(classified.clarification ?? 'Should I look at your screen?')
    this.callbacks.onState('listening')
  }

  async provideScreenContext(capture: CaptureResult, serverCall?: RealtimeServerCall): Promise<void> {
    if (serverCall) {
      await this.sendCaptureForRequest(capture, serverCall)
      return
    }
    await this.sendCapture(capture, this.lastUserRequest)
  }

  declineScreenContext(serverCall?: RealtimeServerCall): void {
    if (serverCall) {
      this.sendFunctionCallOutput(serverCall, { ok: false, message: 'The user did not select a screen or window to share.' })
    }
  }

  invalidateScreenContext(): void {
    this.currentCapture = undefined
    this.currentExplanation = undefined
    this.updateLiveSessionInstructions()
  }

  async sendCapture(capture: CaptureResult, question: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before capturing a screen.')
    }

    this.touchActivity()
    this.currentCapture = capture
    this.currentExplanation = undefined
    this.textBuffer = ''
    this.callbacks.onState('thinking')

    if (this.mode === 'mock') {
      await delay(550)
      const explanation = createMockExplanation(capture, question)
      this.currentExplanation = explanation
      this.callbacks.onExplanation(explanation)
      this.callbacks.onTranscript(explanation.summary)
      this.callbacks.onToolProposal(createMockReminderProposal(explanation, capture))
      this.callbacks.onState('speaking')
      this.speakMock(explanation.summary)
      return
    }

    this.updateLiveSessionInstructions()

    if (this.responseActive) {
      this.sendEvent({ type: 'response.cancel' })
      this.responseActive = false
    }

    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [
          {
            type: 'input_text',
            text: `${question.trim() || 'What is this screen about?'} Review the attached screen capture, taken at ${capture.capturedAt}, and include useful dates, links, and next actions.`
          },
          { type: 'input_image', image_url: capture.dataUrl, detail: 'auto' }
        ]
      }
    })
    this.sendEvent({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: pickResponseBudget('long-form') }
    })
  }

  sendToolResult(proposal: ToolProposal, result: ToolExecutionResult, serverCall?: RealtimeServerCall): void {
    if (!serverCall || proposal.callId !== serverCall.callId) {
      return
    }

    this.sendFunctionCallOutput(serverCall, result)
  }

  declineToolProposal(proposal: ToolProposal, serverCall?: RealtimeServerCall): void {
    this.sendToolResult(proposal, { ok: false, message: 'The user declined this action.' }, serverCall)
  }

  completeTelegramRecipientSearch(serverCall: RealtimeServerCall, foundCount: number): void {
    this.sendFunctionCallOutput(serverCall, {
      ok: foundCount > 0,
      message: foundCount > 0
        ? 'Local recipient choices are displayed to the user. Do not request names or identifiers; wait for their local selection.'
        : 'No local recipient choices matched. Ask the user to try another name.'
    })
  }

  disconnect(): number | undefined {
    const endedGeneration = this.activeGeneration
    this.activeGeneration = undefined
    this.dataChannelGeneration = undefined
    this.clearIdleTimer()
    this.clearCollapseTimer()
    this.clearDeferredDisconnect()
    this.connected = false
    this.responseActive = false
    this.awaitingInitialSessionUpdate = false
    this.pendingCallGenerations.clear()
    this.completedCallIds.clear()
    this.answeredCallIds.clear()
    this.lastSentInstructions = undefined
    this.currentCapture = undefined
    this.currentExplanation = undefined
    this.lastUserRequest = ''
    this.resultOrdinals = []
    this.resultContext = []
    this.latestSearchFallback = false
    this.lastOpenedResult = undefined
    this.selectedPhoto = undefined
    this.listening = true
    this.dataChannel?.close()
    this.peerConnection?.close()
    this.localAudio?.getTracks().forEach((track) => track.stop())
    this.remoteAudio?.remove()
    window.speechSynthesis?.cancel()
    this.dataChannel = undefined
    this.peerConnection = undefined
    this.localAudio = undefined
    this.remoteAudio = undefined
    return endedGeneration
  }

  completeTelegramAttachmentRequest(serverCall: RealtimeServerCall, result: ToolExecutionResult): void {
    this.sendFunctionCallOutput(serverCall, result)
  }

  private async connectLive(token: string, generation: number): Promise<void> {
    this.localAudio = await navigator.mediaDevices.getUserMedia(LAPTOP_MIC_CONSTRAINTS)
    this.peerConnection = new RTCPeerConnection()
    this.remoteAudio = document.createElement('audio')
    this.remoteAudio.autoplay = true
    this.remoteAudio.hidden = true
    document.body.append(this.remoteAudio)
    this.peerConnection.ontrack = (event) => {
      if (this.remoteAudio && event.streams[0]) {
        this.remoteAudio.srcObject = event.streams[0]
      }
    }
    const peerConnection = this.peerConnection
    this.peerConnection.onconnectionstatechange = () => {
      if (this.peerConnection === peerConnection && this.activeGeneration === generation && peerConnection.connectionState === 'failed') {
        this.failLiveConnection(generation)
      }
    }

    const track = this.localAudio.getAudioTracks()[0]
    if (!track) {
      throw new Error('Microphone access did not return an audio track.')
    }
    track.enabled = this.listening
    this.peerConnection.addTrack(track, this.localAudio)

    this.dataChannel = this.peerConnection.createDataChannel('oai-events')
    const dataChannel = this.dataChannel
    this.dataChannelGeneration = generation
    dataChannel.onmessage = (event) => {
      if (this.dataChannel === dataChannel) {
        this.handleServerEvent(event.data, generation)
      }
    }
    const opened = this.waitForDataChannel(dataChannel, generation)
    void opened.catch(() => undefined)

    const offer = await this.peerConnection.createOffer()
    if (!offer.sdp) {
      throw new Error('Could not create a WebRTC session description.')
    }
    await this.peerConnection.setLocalDescription(offer)
    let response: Response
    try {
      response = await fetchWithTimeout('https://api.openai.com/v1/realtime/calls', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/sdp'
        },
        body: offer.sdp
      })
    } catch (error) {
      if (isTimeoutError(error)) {
        throw new Error('Realtime connection timed out while negotiating audio.')
      }
      throw error
    }
    if (!response.ok) {
      throw new Error(`Realtime WebRTC connection failed (status ${response.status}).`)
    }

    await this.peerConnection.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    await opened
  }

  private waitForDataChannel(channel: RTCDataChannel, generation: number): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error('Timed out while opening the Realtime event channel.')), 15_000)
      channel.onopen = () => {
        window.clearTimeout(timeout)
        try {
          if (this.dataChannel !== channel || this.activeGeneration !== generation) {
            reject(new Error('The Realtime session was replaced before its event channel opened.'))
            return
          }
          this.connected = true
          this.configureLiveSession()
          this.touchActivity()
          this.callbacks.onState('listening')
          resolve()
        } catch (error) {
          reject(error)
        }
      }
      channel.onerror = () => {
        window.clearTimeout(timeout)
        reject(new Error('The Realtime event channel could not be opened.'))
      }
      channel.onclose = () => {
        window.clearTimeout(timeout)
        if (!this.connected) {
          reject(new Error('The Realtime event channel closed before it opened.'))
        }
      }
    })
  }

  private configureLiveSession(): void {
    this.awaitingInitialSessionUpdate = true
    this.sendSessionUpdate()
  }

  private requestGreeting(): void {
    this.sendEvent({
      type: 'response.create',
      response: {
        instructions: 'Greet the user briefly, then invite them to capture a screen or ask a question.',
        output_modalities: ['audio'],
        max_output_tokens: pickResponseBudget('confirmation')
      }
    })
  }

  /**
   * Deliberately free of timestamps and identifiers so the instruction prefix
   * stays stable and cacheable across a session. Capture-specific times travel
   * with the capture message instead.
   */
  private sessionInstructions(): string {
    const folderInstructions = this.approvedRoots.length === 0
      ? 'No folder is approved for file search yet. Still call search_documents when the user wants a stored file; LifeLens will ask for approval and run the search.'
      : 'The user has approved at least one folder. LifeLens searches all of them.'
    const contextInstructions = this.hasActiveScreenContext()
      ? 'A recent screen context from this conversation is available; use it for follow-ups.'
      : 'There is no current screen context.'
    // A boolean, never the filename, so the cacheable prefix stays stable.
    const photoInstructions = this.selectedPhoto
      ? 'The user has selected one photo in this conversation; answer follow-up questions about it from that image.'
      : 'No photo has been selected for analysis.'
    return `${SYSTEM_INSTRUCTIONS} ${folderInstructions} ${contextInstructions} ${photoInstructions}`
  }

  private updateLiveSessionInstructions(): void {
    if (this.mode === 'live' && this.dataChannel?.readyState === 'open') {
      const instructions = this.sessionInstructions()
      if (instructions === this.lastSentInstructions) {
        return
      }
      this.sendEvent({
        type: 'session.update',
        session: { type: 'realtime', instructions }
      })
      this.lastSentInstructions = instructions
    }
  }

  private failLiveConnection(generation: number): void {
    if (this.activeGeneration !== generation) {
      return
    }
    this.callbacks.onError('The Realtime voice connection failed.')
    this.callbacks.onState('error')
    const endedGeneration = this.disconnect()
    if (endedGeneration !== undefined) {
      this.callbacks.onSessionEnded?.('error', endedGeneration)
    }
  }

  private sendSessionUpdate(): void {
    const instructions = this.sessionInstructions()
    this.sendEvent({
      type: 'session.update',
      session: {
        type: 'realtime',
        instructions,
        tools: TOOL_DEFINITIONS,
        tool_choice: 'auto',
        // VAD-created spoken responses have no response.create override, so
        // this ceiling is intentionally high enough for legitimate long-form audio.
        max_output_tokens: 1024,
        audio: {
          input: {
            noise_reduction: { type: 'far_field' },
            // Completed transcripts feed the trusted main-process intent
            // tracker, so spoken and typed requests get identical policy.
            transcription: { model: INPUT_TRANSCRIPTION_MODEL },
            turn_detection: this.listening ? SERVER_TURN_DETECTION : null
          }
        }
      }
    })
    this.lastSentInstructions = instructions
  }

  private sendEvent(event: unknown): void {
    if (this.dataChannel?.readyState !== 'open') {
      throw new Error('The Realtime event channel is not ready.')
    }
    this.dataChannel.send(JSON.stringify(event))
  }

  private sendFunctionCallOutput(
    serverCall: RealtimeServerCall,
    result: ToolExecutionResult,
    createResponse = true,
    responseKind: 'confirmation' | 'search-results' = 'confirmation'
  ): void {
    if (!this.isServerCallActive(serverCall)) {
      return
    }

    this.pendingCallGenerations.delete(serverCall.callId)
    this.touchActivity()
    try {
      this.sendEvent({
        type: 'conversation.item.create',
        item: {
          type: 'function_call_output',
          call_id: serverCall.callId,
          // Only the redacted compact view may leave the machine. Trusted
          // results carry identifiers and paths and are never serialized here.
          output: JSON.stringify({
            ok: result.ok,
            message: result.message,
            code: result.code,
            results: result.compactResults
          })
        }
      })
      if (createResponse) {
        const searchNarration = responseKind === 'search-results'
          ? createExactSearchNarration(result)
          : undefined
        this.sendEvent({
          type: 'response.create',
          response: {
            output_modalities: ['audio'],
            max_output_tokens: pickResponseBudget(responseKind),
            ...(searchNarration ? { instructions: `Speak exactly this short search summary and nothing else: ${searchNarration}` } : {})
          }
        })
        this.responseActive = true
      }
    } catch (error) {
      this.callbacks.onError(error instanceof Error ? error.message : 'Could not return the action result to Realtime.')
    }
  }

  private handleServerEvent(serializedEvent: unknown, generation: number): void {
    if (typeof serializedEvent !== 'string' || this.activeGeneration !== generation || this.dataChannelGeneration !== generation) {
      return
    }

    let event: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(serializedEvent)
      if (!isRecord(parsed)) {
        return
      }
      event = parsed
    } catch {
      return
    }

    const type = typeof event.type === 'string' ? event.type : ''
    if (type === 'error') {
      const message = isRecord(event.error) && typeof event.error.message === 'string' ? event.error.message : 'Realtime returned an error.'
      this.callbacks.onError(message)
      this.callbacks.onState('error')
      return
    }

    if (type === 'session.updated') {
      if (this.awaitingInitialSessionUpdate) {
        this.awaitingInitialSessionUpdate = false
        if (this.greetAfterInitialSessionUpdate && !this.responseActive) {
          this.requestGreeting()
        }
      }
      return
    }

    if (type === 'input_audio_buffer.speech_started') {
      this.responseActive = true
      this.touchActivity()
      return
    }

    if (type === 'conversation.item.input_audio_transcription.completed') {
      this.handleUserTranscript(typeof event.transcript === 'string' ? event.transcript : '')
      return
    }

    if (type === 'response.created') {
      this.responseActive = true
      this.textBuffer = ''
      return
    }

    if (type === 'response.output_text.delta' || type === 'response.text.delta' || type === 'response.output_audio_transcript.delta') {
      const delta = typeof event.delta === 'string' ? event.delta : ''
      if (delta) {
        this.textBuffer += delta
      }
      return
    }

    if (type === 'response.function_call_arguments.done') {
      this.handleToolCall(event, generation)
      return
    }

    if (type === 'response.done') {
      this.handleResponseDone(event, generation)
    }
  }

  private handleResponseDone(event: Record<string, unknown>, generation: number): void {
    if (this.activeGeneration !== generation) {
      return
    }
    this.responseActive = false
    this.touchActivity()
    if (!isRecord(event.response)) {
      return
    }

    const response = event.response
    const responseText = extractResponseText(response)
    if (responseText) {
      this.textBuffer = this.textBuffer || responseText
    }
    const output = Array.isArray(response.output) ? response.output : []
    for (const item of output) {
      if (isRecord(item) && item.type === 'function_call') {
        this.handleToolCall(item, generation)
      }
    }

    if (this.currentCapture && this.textBuffer.trim()) {
      this.currentExplanation = {
        summary: this.textBuffer.trim(),
        sourceCaptureId: this.currentCapture.id,
        signals: extractSignals(this.textBuffer)
      }
      this.callbacks.onExplanation(this.currentExplanation)
    }
    if (this.textBuffer.trim()) {
      this.callbacks.onTranscript(this.textBuffer.trim())
    }
    this.callbacks.onState('listening')
    this.finishDeferredDisconnectIfIdle()
  }

  /**
   * A completed spoken request is classified before any guarded tool call runs,
   * so a spoken "find my latest resume" is governed by the same trusted policy
   * as the typed request and can never reach the screen-capture path.
   */
  private handleUserTranscript(transcript: string): void {
    const text = transcript.trim()
    if (!text) {
      return
    }

    this.touchActivity()
    this.lastUserRequest = text
    const notify = this.callbacks.onUserTranscript
    if (!notify) {
      return
    }

    this.intentUpdate = this.intentUpdate
      .then(() => notify(text))
      .catch(() => undefined)
      .then(() => undefined)
  }

  private handleToolCall(event: Record<string, unknown>, generation: number): void {
    if (this.activeGeneration !== generation) {
      return
    }
    const rawName = typeof event.name === 'string' ? event.name : ''
    const callId = typeof event.call_id === 'string' ? event.call_id : typeof event.callId === 'string' ? event.callId : ''
    const serverCall = { callId, generation }
    const callKey = serverCallKey(serverCall)
    if (!rawName || !callId || this.completedCallIds.has(callKey)) {
      return
    }

    this.touchActivity()
    this.completedCallIds.add(callKey)
    this.pendingCallGenerations.set(callId, generation)
    if (rawName === CAPTURE_CONTEXT_TOOL) {
      if (this.hasActiveScreenContext()) {
        this.sendFunctionCallOutput(serverCall, { ok: false, message: 'A current screen context is already available for this conversation.' })
      } else {
        this.withPolicyDecision(serverCall, CAPTURE_CONTEXT_TOOL, (decision) => this.handleCaptureDecision(serverCall, decision))
      }
      return
    }

    if (rawName === TELEGRAM_RECIPIENT_SEARCH_TOOL) {
      try {
        const parsed = JSON.parse(typeof event.arguments === 'string' ? event.arguments : '') as unknown
        if (!isRecord(parsed)) {
          throw new Error('Realtime supplied non-object recipient search details.')
        }
        const query = requiredArgument(parsed, 'query')
        if (!this.callbacks.onTelegramRecipientSearch) {
          throw new Error('Telegram recipient search is unavailable in this companion view.')
        }
        this.callbacks.onTelegramRecipientSearch(query, serverCall)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'LifeLens received malformed Telegram recipient search details.'
        this.callbacks.onError(message)
        this.sendFunctionCallOutput(serverCall, { ok: false, message })
      }
      return
    }

    if (rawName === TELEGRAM_ATTACHMENT_TOOL) {
      try {
        const parsed = JSON.parse(typeof event.arguments === 'string' ? event.arguments : '') as unknown
        if (!isRecord(parsed)) throw new Error('Realtime supplied non-object attachment details.')
        if (!this.callbacks.onTelegramAttachmentRequest) throw new Error('Telegram attachment sending is unavailable in this companion view.')
        const fileResultId = this.resolveAttachmentReference(parsed.attachment)
        const recipientQuery = requiredArgument(parsed, 'recipient_query')
        const reason = requiredArgument(parsed, 'reason')
        const caption = exactOptionalArgument(parsed, 'caption', 1_024)
        this.callbacks.onTelegramAttachmentRequest({ fileResultId, recipientQuery, caption, reason }, serverCall)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'LifeLens received malformed Telegram attachment details.'
        this.callbacks.onError(message)
        this.sendFunctionCallOutput(serverCall, { ok: false, message })
      }
      return
    }

    const name = isToolName(rawName) ? rawName : undefined
    if (!name) {
      this.pendingCallGenerations.delete(callId)
      this.finishDeferredDisconnectIfIdle()
      return
    }
    const argumentsJson = typeof event.arguments === 'string' ? event.arguments : ''
    try {
      const parsed = JSON.parse(argumentsJson) as unknown
      if (!isRecord(parsed)) {
        throw new Error('Realtime supplied non-object function arguments.')
      }

      if (name === 'search_documents') {
        this.requestFileSearch(serverCall, parsed)
        return
      }

      this.callbacks.onToolProposal(this.createToolProposal(name, callId, parsed), serverCall)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'LifeLens received malformed tool details from Realtime.'
      this.callbacks.onError(message)
      this.sendFunctionCallOutput(serverCall, { ok: false, message })
    }
  }

  private withPolicyDecision(serverCall: RealtimeServerCall, toolName: GuardedTool, handler: (decision: ToolPolicyDecision) => void): void {
    // Without a trusted policy channel, fall back to renderer-known state so the
    // decision stays synchronous and deterministic.
    const fallbackDecision = evaluateGuardedToolRequest(toolName, { intent: 'unknown', hasApprovedFolder: this.approvedRoots.length > 0 })
    const evaluate = this.callbacks.evaluateToolPolicy
    const handleIfCurrent = (decision: ToolPolicyDecision): void => {
      if (this.isServerCallActive(serverCall)) {
        handler(decision)
      }
    }
    if (!evaluate) {
      handleIfCurrent(fallbackDecision)
      return
    }

    evaluate(toolName).then(handleIfCurrent, () => handleIfCurrent(fallbackDecision))
  }

  private handleCaptureDecision(serverCall: RealtimeServerCall, decision: ToolPolicyDecision): void {
    if (!decision.allowed) {
      this.sendFunctionCallOutput(serverCall, { ok: false, code: decision.code, message: decision.message })
      return
    }

    this.callbacks.onState('thinking')
    this.callbacks.onCaptureContextRequest(serverCall)
  }

  /**
   * Hands the search to the main process without answering the call. Main may
   * hold it until a folder is approved; the single terminal result arrives
   * later through completeFileSearch.
   */
  private requestFileSearch(serverCall: RealtimeServerCall, argumentsValue: Record<string, unknown>): void {
    void this.intentUpdate.then(() => {
      if (!this.isServerCallActive(serverCall)) {
        return
      }
      try {
        this.callbacks.onFileSearchRequest(parseSearchArguments(argumentsValue), serverCall)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'LifeLens received malformed search details from Realtime.'
        this.callbacks.onError(message)
        this.completeFileSearch(serverCall, { ok: false, message })
      }
    })
  }

  private createToolProposal(name: ToolName, callId: string, argumentsValue: Record<string, unknown>): ToolProposal {
    const reason = requiredArgument(argumentsValue, 'reason', 'The model identified a useful follow-up.')
    const common = { id: crypto.randomUUID(), callId, toolName: name, reason, requiresConfirmation: true as const }

    switch (name) {
      case 'create_reminder':
        return parseToolProposal({
          ...common,
          arguments: {
            title: requiredArgument(argumentsValue, 'title', 'Follow up on this screen'),
            dueAt: normalizeDueAt(argumentsValue.due_at),
            sourceContext: this.currentSourceContext(reason)
          }
        })
      case 'search_documents':
        return parseToolProposal({ ...common, arguments: parseSearchArguments(argumentsValue) })
      case 'open_file': {
        // The model only ever knows result numbers; the identifier is resolved
        // from the local mapping of the most recent search.
        const ordinal = Number(argumentsValue.ordinal)
        const resultId = Number.isInteger(ordinal) ? this.resultOrdinals[ordinal - 1] : undefined
        if (!resultId) {
          throw new Error('Realtime asked to open a result number that does not exist. Search again first.')
        }

        return parseToolProposal({ ...common, arguments: { resultId } })
      }
      case 'open_url':
        return parseToolProposal({
          ...common,
          arguments: { url: requiredArgument(argumentsValue, 'url') }
        })
      case 'save_context':
        return parseToolProposal({
          ...common,
          arguments: { label: requiredArgument(argumentsValue, 'label', 'LifeLens screen context'), sourceContext: this.currentSourceContext(reason) }
        })
      case 'send_telegram_message':
        return parseToolProposal({
          ...common,
          arguments: {
            recipientResultId: requiredArgument(argumentsValue, 'recipient_result_id'),
            message: requiredArgument(argumentsValue, 'message')
          }
        })
      case 'send_telegram_attachment':
        throw new Error('Telegram attachment proposals are assembled only from trusted local selections.')
      case 'analyze_photo':
        // Unreachable through isToolName: sending a photo is a user action and
        // is never offered to the model as a tool.
        throw new Error('Photo analysis is only started by the user, never by a model request.')
    }
  }

  private currentSourceContext(fallbackSummary: string): SourceContext {
    if (!this.currentCapture || !this.hasActiveScreenContext()) {
      throw new Error('Realtime cannot propose this action before a screen capture exists.')
    }

    const explanation = this.currentExplanation
    const summary = explanation?.summary || this.textBuffer.trim() || fallbackSummary
    return {
      captureId: this.currentCapture.id,
      summary,
      capturedAt: this.currentCapture.capturedAt,
      signals: explanation?.signals ?? extractSignals(summary)
    }
  }

  private async sendCaptureForRequest(capture: CaptureResult, serverCall: RealtimeServerCall): Promise<void> {
    if (!this.isServerCallActive(serverCall)) {
      return
    }

    this.touchActivity()
    this.currentCapture = capture
    this.currentExplanation = undefined
    this.textBuffer = ''
    this.updateLiveSessionInstructions()
    if (this.mode === 'mock') {
      await this.sendCapture(capture, this.lastUserRequest)
      return
    }

    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [
          { type: 'input_text', text: `A one-time user-approved screen capture, taken at ${capture.capturedAt}, is attached. Use it to answer the user's current request.` },
          { type: 'input_image', image_url: capture.dataUrl, detail: 'auto' }
        ]
      }
    })
    this.sendFunctionCallOutput(serverCall, { ok: true, message: 'A one-time screen capture is available for the current request.' }, false)
    if (!this.isServerCallCurrent(serverCall)) {
      return
    }
    this.sendEvent({
      type: 'response.create',
      response: { output_modalities: ['audio'], max_output_tokens: pickResponseBudget('long-form') }
    })
    this.responseActive = true
  }

  private resolveAttachmentReference(value: unknown): string {
    if (typeof value === 'string' && /^(?:[1-9]|10)$/.test(value)) {
      const ordinal = Number.parseInt(value, 10)
      const resultId = this.resultOrdinals[ordinal - 1]
      if (!resultId) throw new Error('That result number does not exist. Search again first.')
      return resultId
    }
    if (value !== 'selected') {
      throw new Error('Choose the selected file or a result number from 1 to 10.')
    }

    const asksForDocument = /\b(?:document|resume|cv|pdf|docx?|text file)\b/i.test(this.lastUserRequest)
    const asksForPhoto = /\b(?:photo|picture|image|screenshot|screen shot)\b/i.test(this.lastUserRequest)
    if (asksForPhoto && this.selectedPhoto) return this.selectedPhoto.resultId
    if (!this.latestSearchFallback) {
      const candidates = this.resultContext.filter((result) => asksForDocument
        ? result.kind === 'document'
        : asksForPhoto
          ? result.kind === 'photo' || result.kind === 'screenshot'
          : true)
      if (candidates.length === 1) return candidates[0]!.resultId
      if (candidates.length > 1) throw new Error('Which one — say the number from the list?')
    }
    if (this.lastOpenedResult && (asksForDocument
      ? this.lastOpenedResult.kind === 'document'
      : asksForPhoto
        ? this.lastOpenedResult.kind === 'photo' || this.lastOpenedResult.kind === 'screenshot'
        : true)) return this.lastOpenedResult.resultId
    if (!asksForDocument && this.selectedPhoto) return this.selectedPhoto.resultId
    throw new Error('Which file — say the number from the list?')
  }

  private touchActivity(): void {
    if (!this.isLiveConnected()) {
      return
    }
    if (this.deferredDisconnectReason !== undefined) {
      return
    }
    this.clearIdleTimer()
    this.idleTimer = window.setTimeout(() => this.handleIdleTimeout(), IDLE_DISCONNECT_MS)
  }

  private handleIdleTimeout(): void {
    this.idleTimer = undefined
    if (!this.isLiveConnected()) {
      return
    }
    if (this.hasPendingWork()) {
      this.disconnectOrDefer('idle', Date.now() + MAX_PENDING_WORK_EXTENSION_MS)
      return
    }
    this.endLiveSession('idle')
  }

  private hasPendingWork(): boolean {
    return this.responseActive || this.pendingCallGenerations.size > 0
  }

  private endLiveSession(reason: 'idle' | 'collapsed'): void {
    const endedGeneration = this.disconnect()
    this.callbacks.onState('idle')
    if (endedGeneration !== undefined) {
      this.callbacks.onSessionEnded?.(reason, endedGeneration)
    }
  }

  private disconnectOrDefer(reason: 'idle' | 'collapsed', hardDeadline: number): void {
    if (!this.isLiveConnected()) {
      return
    }
    if (!this.hasPendingWork()) {
      this.endLiveSession(reason)
      return
    }

    if (this.deferredDisconnectDeadline !== undefined && this.deferredDisconnectDeadline <= hardDeadline) {
      return
    }
    this.clearDeferredDisconnect()
    this.deferredDisconnectReason = reason
    this.deferredDisconnectDeadline = hardDeadline
    this.deferredDisconnectTimer = window.setTimeout(() => {
      this.deferredDisconnectTimer = undefined
      const deferredReason = this.deferredDisconnectReason
      this.deferredDisconnectReason = undefined
      this.deferredDisconnectDeadline = undefined
      if (deferredReason && this.isLiveConnected()) {
        this.endLiveSession(deferredReason)
      }
    }, Math.max(0, hardDeadline - Date.now()))
  }

  private finishDeferredDisconnectIfIdle(): void {
    if (!this.deferredDisconnectReason || this.hasPendingWork()) {
      return
    }
    const reason = this.deferredDisconnectReason
    this.clearDeferredDisconnect()
    this.endLiveSession(reason)
  }

  private isServerCallCurrent(serverCall: RealtimeServerCall): boolean {
    return this.mode === 'live' &&
      this.activeGeneration === serverCall.generation &&
      this.dataChannelGeneration === serverCall.generation &&
      this.dataChannel?.readyState === 'open'
  }

  private clearIdleTimer(): void {
    if (this.idleTimer !== undefined) {
      window.clearTimeout(this.idleTimer)
      this.idleTimer = undefined
    }
  }

  private clearCollapseTimer(): void {
    if (this.collapseTimer !== undefined) {
      window.clearTimeout(this.collapseTimer)
      this.collapseTimer = undefined
    }
  }

  private clearDeferredDisconnect(): void {
    if (this.deferredDisconnectTimer !== undefined) {
      window.clearTimeout(this.deferredDisconnectTimer)
      this.deferredDisconnectTimer = undefined
    }
    this.deferredDisconnectReason = undefined
    this.deferredDisconnectDeadline = undefined
  }

  private hasActiveScreenContext(): boolean {
    if (!this.currentCapture) {
      return false
    }
    const capturedAt = Date.parse(this.currentCapture.capturedAt)
    return Number.isFinite(capturedAt) && Date.now() - capturedAt < SCREEN_CONTEXT_TTL_MS
  }

  private speakMock(text: string): void {
    if (!('speechSynthesis' in window)) {
      this.callbacks.onState('listening')
      return
    }

    const utterance = new SpeechSynthesisUtterance(text)
    utterance.rate = 1.03
    utterance.onend = () => this.callbacks.onState('listening')
    utterance.onerror = () => this.callbacks.onState('listening')
    window.speechSynthesis.cancel()
    window.speechSynthesis.speak(utterance)
  }
}

function createSearchNarrationResult(result: {
  ok: boolean
  message: string
  compactResults?: CompactSearchResult[]
  resultCount?: number
}): ToolExecutionResult {
  const compactResults = result.compactResults?.slice(0, MAX_NARRATED_SEARCH_RESULTS).map((entry) => ({
    ...entry,
    name: shortenFilenameForNarration(entry.name)
  }))
  if (!result.ok || !compactResults) {
    return { ok: result.ok, message: result.message, compactResults }
  }

  const resultCount = Math.max(result.resultCount ?? compactResults.length, compactResults.length)
  const moreResults = resultCount > compactResults.length
  const narrationRule = [
    `For the spoken response, say there are ${resultCount} total result${resultCount === 1 ? '' : 's'}.`,
    `Mention no more than these ${compactResults.length} displayed result${compactResults.length === 1 ? '' : 's'}.`,
    'Say the complete list is visible in the UI.',
    moreResults ? 'End by asking exactly: "Would you like to hear more results?"' : undefined
  ].filter((part): part is string => Boolean(part)).join(' ')

  return {
    ok: true,
    message: `${result.message} ${narrationRule}`.trim(),
    compactResults
  }
}

function createExactSearchNarration(result: ToolExecutionResult): string | undefined {
  const results = result.compactResults
  if (!result.ok || !results?.length) {
    return undefined
  }
  const resultCount = Number.parseInt(result.message.match(/\b(\d+)\b/)?.[1] ?? '', 10)
  const total = Number.isFinite(resultCount) && resultCount >= results.length ? resultCount : results.length
  const names = results.map((entry) => `“${entry.name}”`).join(', ')
  const more = total > results.length ? ' Would you like to hear more results?' : ''
  return `I found ${total} matching result${total === 1 ? '' : 's'}: ${names}. The complete list is visible in the UI.${more}`
}

function shortenFilenameForNarration(name: string): string {
  if (name.length <= MAX_NARRATED_FILENAME_LENGTH) {
    return name
  }

  const candidate = name.slice(0, MAX_NARRATED_FILENAME_LENGTH + 1)
  const boundary = Math.max(candidate.lastIndexOf(' '), candidate.lastIndexOf('_'), candidate.lastIndexOf('-'), candidate.lastIndexOf('.'))
  const shortened = boundary > Math.floor(MAX_NARRATED_FILENAME_LENGTH * 0.6)
    ? candidate.slice(0, boundary)
    : candidate.slice(0, MAX_NARRATED_FILENAME_LENGTH)
  return `${shortened}…`
}

function pickResponseBudget(kind: 'confirmation' | 'search-results' | 'question' | 'long-form', request = ''): number {
  if (kind === 'confirmation') {
    return RESPONSE_BUDGETS.confirmation
  }
  if (kind === 'search-results') {
    return RESPONSE_BUDGETS.searchResults
  }
  if (kind === 'long-form' || LONG_FORM_CUE.test(request)) {
    return RESPONSE_BUDGETS.longForm
  }
  return RESPONSE_BUDGETS.normal
}

function serverCallKey(serverCall: RealtimeServerCall): string {
  return `${serverCall.generation}:${serverCall.callId}`
}

function createMockExplanation(capture: CaptureResult, question: string): Explanation {
  const reminderDate = tomorrowAtNine()
  const formattedDate = reminderDate.toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })
  const summary = question.toLowerCase().includes('email')
    ? `This looks like an interview email. A useful follow-up is to prepare your latest resume before ${formattedDate}. The visible preparation link can be reviewed at https://example.com/interview-prep.`
    : `I captured your screen. A useful next step is to review the visible content and prepare any required document before ${formattedDate}. Reference link: https://example.com/interview-prep.`

  return {
    summary,
    sourceCaptureId: capture.id,
    signals: extractSignals(summary)
  }
}

function createMockReminderProposal(explanation: Explanation, capture: CaptureResult): ToolProposal<'create_reminder'> {
  return {
    id: crypto.randomUUID(),
    toolName: 'create_reminder',
    reason: 'The captured screen has a preparation follow-up.',
    requiresConfirmation: true,
    arguments: {
      title: 'Prepare for interview follow-up',
      dueAt: tomorrowAtNine().toISOString(),
      sourceContext: {
        captureId: capture.id,
        summary: explanation.summary,
        capturedAt: capture.capturedAt,
        signals: explanation.signals
      }
    }
  }
}

/** Closed-schema read of the model's search arguments. */
function parseSearchArguments(argumentsValue: Record<string, unknown>): SearchDocumentsInput {
  const queryTerms = optionalArgument(argumentsValue, 'query_terms') ?? optionalArgument(argumentsValue, 'queryTerms')
  if (!queryTerms) {
    throw new Error('Realtime did not provide query_terms for its requested search.')
  }

  const kind = optionalArgument(argumentsValue, 'kind')
  const recency = optionalArgument(argumentsValue, 'recency')
  const rawConcepts = argumentsValue.concepts
  let concepts: string[] | undefined
  if (rawConcepts !== undefined) {
    if (!Array.isArray(rawConcepts)) throw new Error('Realtime supplied invalid visual concepts.')
    concepts = rawConcepts.map((concept) => {
      if (typeof concept !== 'string' || !concept.trim()) throw new Error('Realtime supplied invalid visual concepts.')
      return concept.trim()
    })
  }
  return {
    queryTerms,
    kind: isSearchKind(kind) ? kind : undefined,
    recency: isSearchRecency(recency) ? recency : undefined,
    concepts
  }
}

function requiredArgument(argumentsValue: Record<string, unknown>, name: string, fallback?: string): string {
  const value = optionalArgument(argumentsValue, name)
  if (value) {
    return value
  }
  if (fallback) {
    return fallback
  }
  throw new Error(`Realtime did not provide ${name} for its requested action.`)
}

function optionalArgument(argumentsValue: Record<string, unknown>, name: string): string | undefined {
  const value = argumentsValue[name]
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function exactOptionalArgument(argumentsValue: Record<string, unknown>, name: string, maximum: number): string | undefined {
  const value = argumentsValue[name]
  if (value === undefined) return undefined
  if (typeof value !== 'string') {
    throw new Error(`Realtime did not provide a valid ${name}.`)
  }
  if (value.length > maximum) throw new Error(`That ${name} is ${value.length} characters. Shorten it to ${maximum} characters or fewer.`)
  return value
}

function normalizeDueAt(value: unknown): string {
  if (typeof value === 'string' && Number.isFinite(Date.parse(value))) {
    return new Date(value).toISOString()
  }

  return tomorrowAtNine().toISOString()
}

function tomorrowAtNine(): Date {
  const dueAt = new Date()
  dueAt.setDate(dueAt.getDate() + 1)
  dueAt.setHours(9, 0, 0, 0)
  return dueAt
}

function extractResponseText(response: Record<string, unknown>): string {
  const output = Array.isArray(response.output) ? response.output : []
  const parts: string[] = []
  for (const item of output) {
    if (!isRecord(item)) {
      continue
    }
    if (typeof item.transcript === 'string') {
      parts.push(item.transcript)
    }
    if (!Array.isArray(item.content)) {
      continue
    }
    for (const content of item.content) {
      if (!isRecord(content)) {
        continue
      }
      if (typeof content.text === 'string') {
        parts.push(content.text)
      }
      if (typeof content.transcript === 'string') {
        parts.push(content.transcript)
      }
    }
  }
  return parts.join(' ').trim()
}

/**
 * analyze_photo is deliberately absent: sending a photo is a user action, never
 * a model-initiated one, so a model-authored call is not a recognised tool.
 */
function isToolName(value: string): value is ToolName {
  return value === 'create_reminder' || value === 'search_documents' || value === 'open_file' || value === 'open_url' || value === 'save_context' || value === 'send_telegram_message'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 10_000)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } finally {
    window.clearTimeout(timeout)
  }
}

function isTimeoutError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
}
