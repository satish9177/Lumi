import {
  extractSignals,
  parseToolProposal,
  type ApprovedDocumentRoot,
  type CaptureResult,
  type CompanionState,
  type Explanation,
  type RealtimeSessionCredential,
  type SourceContext,
  type ToolExecutionResult,
  type ToolName,
  type ToolProposal
} from '../../shared/contracts'

interface RealtimeCallbacks {
  onState: (state: CompanionState) => void
  onTranscript: (text: string) => void
  onExplanation: (explanation: Explanation) => void
  onCaptureContextRequest: (callId?: string) => void
  onTelegramRecipientSearch?: (query: string, callId: string) => void
  onToolProposal: (proposal: ToolProposal) => void
  onError: (message: string) => void
}

const CAPTURE_CONTEXT_TOOL = 'capture_screen_context'
const TELEGRAM_RECIPIENT_SEARCH_TOOL = 'telegram_search_recipients'
const SCREEN_CONTEXT_TTL_MS = 10 * 60 * 1_000

const SYSTEM_INSTRUCTIONS = [
  'You are LifeLens, a concise, supportive floating desktop companion.',
  'Explain captured screen content in simple English; use Telugu-English only if the user does.',
  'State important dates, links, and concrete next actions plainly.',
  'Every function is only a proposal. Never claim an action was performed until the application returns its result.',
  'Use only the supplied approved-folder identifiers; never ask for or invent a local file path.',
  'Telegram contact and dialog metadata are local-only. You may request a local recipient search from the user\'s spoken recipient name, but never receive, repeat, or infer Telegram names, usernames, phone numbers, peer identifiers, or search results.',
  'Do not capture a screen when the panel opens or during a general greeting.',
  'When a user request needs visible-screen context and there is no current screen context, call capture_screen_context once. The user making that screen-relative request is consent for this one-time capture.',
  'When a current screen context is available, answer follow-up questions from it and do not call capture_screen_context again unless the user asks to refresh, says the screen changed, refers to a new visible item, or you cannot answer reliably.',
  'If it is unclear whether the user means their screen, ask exactly: Should I look at your screen?',
  'When analyzing a capture, focus on visible page or document content. Ignore browser tabs, address bars, bookmarks, taskbars, and window chrome.',
  'Keep a visible text version of each answer under 120 words.'
].join(' ')

const TOOL_DEFINITIONS = [
  {
    type: 'function',
    name: CAPTURE_CONTEXT_TOOL,
    description: 'Internally request one user-approved screenshot of the currently selected screen or window. Use only when the current user request needs visible-screen context and no usable context is already available. Do not use for greetings or general questions.',
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
    description: 'Propose a filename search within one currently approved folder. The user must confirm it before the folder is searched.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        root_id: { type: 'string', description: 'An approved folder identifier supplied in the session context.' },
        query: { type: 'string', description: 'A short filename query such as resume.' },
        reason: { type: 'string', description: 'Why this approved-folder search helps.' }
      },
      required: ['root_id', 'query', 'reason']
    }
  },
  {
    type: 'function',
    name: 'open_file',
    description: 'Propose opening one result identifier returned by an approved document search. The user must confirm it.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        result_id: { type: 'string', description: 'A result identifier returned by LifeLens.' },
        reason: { type: 'string', description: 'Why this file should be opened.' }
      },
      required: ['result_id', 'reason']
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
  private lastUserRequest = ''
  private awaitingInitialSessionUpdate = false

  constructor(private readonly callbacks: RealtimeCallbacks) {}

  async connect(credential: RealtimeSessionCredential): Promise<void> {
    this.disconnect()
    this.mode = credential.mode

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

    try {
      await this.connectLive(credential.token)
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

  async sendUserRequest(request: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before asking Lumi a question.')
    }

    const trimmedRequest = request.trim()
    if (!trimmedRequest) {
      return
    }

    this.lastUserRequest = trimmedRequest
    if (this.mode === 'mock') {
      if (this.hasActiveScreenContext()) {
        this.callbacks.onTranscript(this.currentExplanation?.summary ?? 'I will use the screen context already captured for this conversation.')
        this.callbacks.onState('listening')
      } else if (likelyNeedsScreenContext(trimmedRequest)) {
        this.callbacks.onCaptureContextRequest()
      } else {
        this.callbacks.onTranscript('Should I look at your screen?')
        this.callbacks.onState('listening')
      }
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
    this.sendEvent({ type: 'response.create', response: { output_modalities: ['audio'] } })
  }

  async provideScreenContext(capture: CaptureResult, callId?: string): Promise<void> {
    if (callId) {
      await this.sendCaptureForRequest(capture, callId)
      return
    }
    await this.sendCapture(capture, this.lastUserRequest)
  }

  declineScreenContext(callId?: string): void {
    if (this.mode === 'live' && callId) {
      this.sendFunctionCallOutput(callId, { ok: false, message: 'The user did not select a screen or window to share.' })
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
            text: `${question.trim() || 'What is this screen about?'} Review the attached screen capture and give a short answer with dates, links, and next actions.`
          },
          { type: 'input_image', image_url: capture.dataUrl }
        ]
      }
    })
    this.sendEvent({
      type: 'response.create',
      response: { output_modalities: ['audio'] }
    })
  }

  sendToolResult(proposal: ToolProposal, result: ToolExecutionResult): void {
    if (this.mode !== 'live' || !proposal.callId) {
      return
    }

    this.sendFunctionCallOutput(proposal.callId, result)
  }

  declineToolProposal(proposal: ToolProposal): void {
    this.sendToolResult(proposal, { ok: false, message: 'The user declined this action.' })
  }

  completeTelegramRecipientSearch(callId: string, foundCount: number): void {
    this.sendFunctionCallOutput(callId, {
      ok: foundCount > 0,
      message: foundCount > 0
        ? 'Local recipient choices are displayed to the user. Do not request names or identifiers; wait for their local selection.'
        : 'No local recipient choices matched. Ask the user to try another name.'
    })
  }

  disconnect(): void {
    this.connected = false
    this.responseActive = false
    this.awaitingInitialSessionUpdate = false
    this.currentCapture = undefined
    this.currentExplanation = undefined
    this.lastUserRequest = ''
    this.dataChannel?.close()
    this.peerConnection?.close()
    this.localAudio?.getTracks().forEach((track) => track.stop())
    this.remoteAudio?.remove()
    window.speechSynthesis?.cancel()
    this.dataChannel = undefined
    this.peerConnection = undefined
    this.localAudio = undefined
    this.remoteAudio = undefined
  }

  private async connectLive(token: string): Promise<void> {
    this.localAudio = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    })
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
    this.peerConnection.onconnectionstatechange = () => {
      if (this.peerConnection?.connectionState === 'failed') {
        this.callbacks.onError('The Realtime voice connection failed.')
        this.callbacks.onState('error')
        this.disconnect()
      }
    }

    const track = this.localAudio.getAudioTracks()[0]
    if (!track) {
      throw new Error('Microphone access did not return an audio track.')
    }
    this.peerConnection.addTrack(track, this.localAudio)

    this.dataChannel = this.peerConnection.createDataChannel('oai-events')
    this.dataChannel.onmessage = (event) => this.handleServerEvent(event.data)
    const opened = this.waitForDataChannel(this.dataChannel)
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

  private waitForDataChannel(channel: RTCDataChannel): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error('Timed out while opening the Realtime event channel.')), 15_000)
      channel.onopen = () => {
        window.clearTimeout(timeout)
        try {
          this.connected = true
          this.configureLiveSession()
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
        output_modalities: ['audio']
      }
    })
  }

  private sessionInstructions(): string {
    const folderInstructions = this.approvedRoots.length === 0
      ? 'No folder is approved for document search right now.'
      : `Approved folders available for search: ${this.approvedRoots.map((root) => `${root.label} (${root.id})`).join(', ')}.`
    const contextInstructions = this.hasActiveScreenContext()
      ? `A current screen context was captured at ${this.currentCapture?.capturedAt} and expires at ${new Date(Date.parse(this.currentCapture!.capturedAt) + SCREEN_CONTEXT_TTL_MS).toISOString()}; use it for follow-ups until then.`
      : 'There is no current screen context.'
    return `${SYSTEM_INSTRUCTIONS} ${folderInstructions} ${contextInstructions}`
  }

  private updateLiveSessionInstructions(): void {
    if (this.mode === 'live' && this.dataChannel?.readyState === 'open') {
      this.sendSessionUpdate()
    }
  }

  private sendSessionUpdate(): void {
    this.sendEvent({
      type: 'session.update',
      session: {
        type: 'realtime',
        instructions: this.sessionInstructions(),
        tools: TOOL_DEFINITIONS,
        tool_choice: 'auto'
      }
    })
  }

  private sendEvent(event: unknown): void {
    if (this.dataChannel?.readyState !== 'open') {
      throw new Error('The Realtime event channel is not ready.')
    }
    this.dataChannel.send(JSON.stringify(event))
  }

  private sendFunctionCallOutput(callId: string, result: ToolExecutionResult, createResponse = true): void {
    try {
      this.sendEvent({
        type: 'conversation.item.create',
        item: {
          type: 'function_call_output',
          call_id: callId,
          output: JSON.stringify({ ok: result.ok, message: result.message, searchResults: result.searchResults })
        }
      })
      if (createResponse) {
        this.sendEvent({ type: 'response.create' })
      }
    } catch (error) {
      this.callbacks.onError(error instanceof Error ? error.message : 'Could not return the action result to Realtime.')
    }
  }

  private handleServerEvent(serializedEvent: unknown): void {
    if (typeof serializedEvent !== 'string') {
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
        this.requestGreeting()
      }
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
      this.handleToolCall(event)
      return
    }

    if (type === 'response.done') {
      this.handleResponseDone(event)
    }
  }

  private handleResponseDone(event: Record<string, unknown>): void {
    this.responseActive = false
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
        this.handleToolCall(item)
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
  }

  private handleToolCall(event: Record<string, unknown>): void {
    const rawName = typeof event.name === 'string' ? event.name : ''
    const callId = typeof event.call_id === 'string' ? event.call_id : typeof event.callId === 'string' ? event.callId : ''
    if (!rawName || !callId || this.completedCallIds.has(callId)) {
      return
    }

    this.completedCallIds.add(callId)
    if (rawName === CAPTURE_CONTEXT_TOOL) {
      if (this.hasActiveScreenContext()) {
        this.sendFunctionCallOutput(callId, { ok: false, message: 'A current screen context is already available for this conversation.' })
      } else {
        this.callbacks.onState('thinking')
        this.callbacks.onCaptureContextRequest(callId)
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
        this.callbacks.onTelegramRecipientSearch(query, callId)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'LifeLens received malformed Telegram recipient search details.'
        this.callbacks.onError(message)
        this.sendFunctionCallOutput(callId, { ok: false, message })
      }
      return
    }

    const name = isToolName(rawName) ? rawName : undefined
    if (!name) {
      return
    }
    const argumentsJson = typeof event.arguments === 'string' ? event.arguments : ''
    try {
      const parsed = JSON.parse(argumentsJson) as unknown
      if (!isRecord(parsed)) {
        throw new Error('Realtime supplied non-object function arguments.')
      }

      this.callbacks.onToolProposal(this.createToolProposal(name, callId, parsed))
    } catch (error) {
      const message = error instanceof Error ? error.message : 'LifeLens received malformed tool details from Realtime.'
      this.callbacks.onError(message)
      this.sendFunctionCallOutput(callId, { ok: false, message })
    }
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
      case 'search_documents': {
        const requestedRootId = optionalArgument(argumentsValue, 'root_id') ?? optionalArgument(argumentsValue, 'rootId')
        const rootId = requestedRootId || (this.approvedRoots.length === 1 ? this.approvedRoots[0].id : '')
        if (!rootId || !this.approvedRoots.some((root) => root.id === rootId)) {
          throw new Error('Realtime requested a document search without an approved folder.')
        }

        return parseToolProposal({
          ...common,
          arguments: { rootId, query: requiredArgument(argumentsValue, 'query') }
        })
      }
      case 'open_file':
        return parseToolProposal({
          ...common,
          arguments: { resultId: requiredArgument(argumentsValue, 'result_id') }
        })
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

  private async sendCaptureForRequest(capture: CaptureResult, callId: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before capturing a screen.')
    }

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
          { type: 'input_text', text: 'A one-time user-approved screen capture is attached. Use it to answer the user\'s current request.' },
          { type: 'input_image', image_url: capture.dataUrl }
        ]
      }
    })
    this.sendFunctionCallOutput(callId, { ok: true, message: 'A one-time screen capture is available for the current request.' }, false)
    this.sendEvent({ type: 'response.create', response: { output_modalities: ['audio'] } })
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

function isToolName(value: string): value is ToolName {
  return value === 'create_reminder' || value === 'search_documents' || value === 'open_file' || value === 'open_url' || value === 'save_context' || value === 'send_telegram_message'
}

function likelyNeedsScreenContext(request: string): boolean {
  const normalized = request.toLocaleLowerCase()
  return /\b(screen|page|email|document|message|deadline|visible|looking at|this|that|here)\b/.test(normalized) &&
    /\b(what|when|where|who|why|how|explain|read|deadline|prepare|should|looking|is)\b/.test(normalized)
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
