import { extractSignals, type CaptureResult, type CompanionState, type Explanation, type RealtimeSessionCredential, type ToolExecutionResult, type ToolProposal } from '../../shared/contracts'

interface RealtimeCallbacks {
  onState: (state: CompanionState) => void
  onTranscript: (text: string) => void
  onExplanation: (explanation: Explanation) => void
  onToolProposal: (proposal: ToolProposal) => void
  onError: (message: string) => void
}

const SYSTEM_INSTRUCTIONS = [
  'You are LifeLens, a concise, supportive floating desktop companion.',
  'Explain captured screen content in simple English; use Telugu-English only if the user does.',
  'State important dates, links, and concrete next actions plainly.',
  'Never claim you performed an external or state-changing action.',
  'If a reminder would help, call create_reminder with a precise title and ISO 8601 due_at.',
  'Keep a visible text version of your answer under 120 words.'
].join(' ')

const REMINDER_TOOL = {
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
}

export class RealtimeClient {
  private dataChannel: RTCDataChannel | undefined
  private peerConnection: RTCPeerConnection | undefined
  private localAudio: MediaStream | undefined
  private remoteAudio: HTMLAudioElement | undefined
  private currentCapture: CaptureResult | undefined
  private textBuffer = ''
  private connected = false
  private model = 'gpt-realtime-2.1'
  private mode: 'live' | 'mock' = 'mock'
  private readonly completedCallIds = new Set<string>()

  constructor(private readonly callbacks: RealtimeCallbacks) {}

  async connect(credential: RealtimeSessionCredential): Promise<void> {
    this.mode = credential.mode
    this.model = credential.model

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

    await this.connectLive(credential.token)
  }

  async sendCapture(capture: CaptureResult, question: string): Promise<void> {
    if (!this.connected) {
      throw new Error('Connect voice before capturing a screen.')
    }

    this.currentCapture = capture
    this.textBuffer = ''
    this.callbacks.onState('thinking')

    if (this.mode === 'mock') {
      await delay(550)
      const explanation = createMockExplanation(capture, question)
      this.callbacks.onExplanation(explanation)
      this.callbacks.onTranscript(explanation.summary)
      this.callbacks.onToolProposal(createMockReminderProposal(explanation, capture))
      this.callbacks.onState('speaking')
      this.speakMock(explanation.summary)
      return
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
      response: { output_modalities: ['audio', 'text'] }
    })
  }

  sendToolResult(proposal: ToolProposal, result: ToolExecutionResult): void {
    if (this.mode !== 'live' || !proposal.callId) {
      return
    }

    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'function_call_output',
        call_id: proposal.callId,
        output: JSON.stringify({ ok: result.ok, message: result.message })
      }
    })
    this.sendEvent({ type: 'response.create' })
  }

  disconnect(): void {
    this.connected = false
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
      }
    }

    const track = this.localAudio.getAudioTracks()[0]
    if (!track) {
      throw new Error('Microphone access did not return an audio track.')
    }
    this.peerConnection.addTrack(track, this.localAudio)

    this.dataChannel = this.peerConnection.createDataChannel('oai-events')
    this.dataChannel.onmessage = (event) => this.handleServerEvent(event.data)
    const opened = new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error('Timed out while opening the Realtime event channel.')), 15_000)
      this.dataChannel!.onopen = () => {
        window.clearTimeout(timeout)
        this.connected = true
        this.configureLiveSession()
        this.callbacks.onState('listening')
        resolve()
      }
      this.dataChannel!.onerror = () => {
        window.clearTimeout(timeout)
        reject(new Error('The Realtime event channel could not be opened.'))
      }
    })

    const offer = await this.peerConnection.createOffer()
    if (!offer.sdp) {
      throw new Error('Could not create a WebRTC session description.')
    }
    await this.peerConnection.setLocalDescription(offer)
    const response = await fetch('https://api.openai.com/v1/realtime/calls', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/sdp'
      },
      body: offer.sdp
    })
    if (!response.ok) {
      throw new Error(`Realtime WebRTC connection failed (status ${response.status}).`)
    }

    await this.peerConnection.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    await opened
  }

  private configureLiveSession(): void {
    this.sendEvent({
      type: 'session.update',
      session: {
        type: 'realtime',
        model: this.model,
        instructions: SYSTEM_INSTRUCTIONS,
        audio: { output: { voice: 'marin' } },
        tools: [REMINDER_TOOL],
        tool_choice: 'auto'
      }
    })
    this.sendEvent({
      type: 'response.create',
      response: {
        instructions: 'Greet the user briefly, then invite them to capture a screen or ask a question.',
        output_modalities: ['audio', 'text']
      }
    })
  }

  private sendEvent(event: unknown): void {
    if (this.dataChannel?.readyState !== 'open') {
      throw new Error('The Realtime event channel is not ready.')
    }
    this.dataChannel.send(JSON.stringify(event))
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

    if (type === 'response.output_text.delta' || type === 'response.text.delta' || type === 'response.audio_transcript.delta') {
      const delta = typeof event.delta === 'string' ? event.delta : ''
      if (delta) {
        this.textBuffer += delta
        this.callbacks.onTranscript(delta)
      }
      return
    }

    if (type === 'response.function_call_arguments.done') {
      this.handleReminderCall(event)
      return
    }

    if (type === 'response.done') {
      this.handleResponseDone(event)
    }
  }

  private handleResponseDone(event: Record<string, unknown>): void {
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
        this.handleReminderCall(item)
      }
    }

    if (this.currentCapture && this.textBuffer.trim()) {
      this.callbacks.onExplanation({
        summary: this.textBuffer.trim(),
        sourceCaptureId: this.currentCapture.id,
        signals: extractSignals(this.textBuffer)
      })
    }
    this.callbacks.onState('listening')
  }

  private handleReminderCall(event: Record<string, unknown>): void {
    const name = typeof event.name === 'string' ? event.name : ''
    const callId = typeof event.call_id === 'string' ? event.call_id : typeof event.callId === 'string' ? event.callId : ''
    if (name !== 'create_reminder' || !callId || this.completedCallIds.has(callId) || !this.currentCapture) {
      return
    }

    const argumentsJson = typeof event.arguments === 'string' ? event.arguments : ''
    let parsedArguments: Record<string, unknown> = {}
    try {
      const parsed: unknown = JSON.parse(argumentsJson)
      if (isRecord(parsed)) {
        parsedArguments = parsed
      }
    } catch {
      this.callbacks.onError('LifeLens received malformed reminder details from Realtime.')
      return
    }

    const title = typeof parsedArguments.title === 'string' && parsedArguments.title.trim() ? parsedArguments.title.trim() : 'Follow up on this screen'
    const dueAt = normalizeDueAt(parsedArguments.due_at)
    const reason = typeof parsedArguments.reason === 'string' && parsedArguments.reason.trim() ? parsedArguments.reason.trim() : 'A follow-up was identified in the captured screen.'
    const summary = this.textBuffer.trim() || reason
    this.completedCallIds.add(callId)
    this.callbacks.onToolProposal({
      id: crypto.randomUUID(),
      callId,
      toolName: 'create_reminder',
      reason,
      requiresConfirmation: true,
      arguments: {
        title,
        dueAt,
        sourceContext: {
          captureId: this.currentCapture.id,
          summary,
          capturedAt: this.currentCapture.capturedAt,
          signals: extractSignals(summary)
        }
      }
    })
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
    if (!isRecord(item) || !Array.isArray(item.content)) {
      continue
    }
    for (const content of item.content) {
      if (isRecord(content) && typeof content.text === 'string') {
        parts.push(content.text)
      }
    }
  }
  return parts.join(' ').trim()
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}
