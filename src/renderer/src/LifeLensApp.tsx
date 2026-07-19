import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import type {
  ApprovedDocumentRoot,
  CaptureResult,
  CaptureSource,
  CompanionState,
  DocumentSearchResult,
  Explanation,
  FileSearchResults,
  PendingActionPreview,
  RealtimeMode,
  ResultThumbnail,
  SourceContext,
  TelegramRecipient,
  TelegramStatus,
  ToolExecutionResult,
  ToolProposal
} from '../../shared/contracts'
import { fileKindLabel } from '../../shared/search-query'
import { ExplanationCard, PhotoResultGrid, ToolConfirmationCard } from './components'
import { FileSearchController, type SearchConfirmationRequest } from './file-search-controller'
import { RealtimeClient, type RealtimeServerCall } from './realtime'

const VOICE_PAUSED_NOTICE = 'Voice paused to save cost — ask a question to reconnect.'

interface PendingProposal {
  readonly proposal: ToolProposal
  readonly serverCall?: RealtimeServerCall
}

const STATUS_LABELS: Record<CompanionState, string> = {
  idle: 'Ready',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  success: 'Done',
  error: 'Needs attention'
}

export default function LifeLensApp() {
  const clientRef = useRef<RealtimeClient | undefined>(undefined)
  const connectPromiseRef = useRef<Promise<void> | undefined>(undefined)
  const hasConnectedOnceRef = useRef(false)
  const expandedRef = useRef(false)
  const collapsedAtRef = useRef<number | undefined>(Date.now())
  const controllerRef = useRef<FileSearchController>(undefined)
  const [expanded, setExpanded] = useState(false)
  const [companionState, setCompanionState] = useState<CompanionState>('idle')
  const [mode, setMode] = useState<RealtimeMode | undefined>()
  const [question, setQuestion] = useState('')
  const [capture, setCapture] = useState<CaptureResult>()
  const [captureSources, setCaptureSources] = useState<CaptureSource[]>([])
  const [capturePickerOpen, setCapturePickerOpen] = useState(false)
  const [selectedCaptureSourceId, setSelectedCaptureSourceId] = useState<string>()
  const [explanation, setExplanation] = useState<Explanation>()
  const [pendingAction, setPendingAction] = useState<PendingActionPreview>()
  const [searchConfirmation, setSearchConfirmation] = useState<SearchConfirmationRequest | undefined>()
  const pendingProposalsRef = useRef(new Map<string, PendingProposal>())
  const [toolResult, setToolResult] = useState<ToolExecutionResult>()
  const [transcript, setTranscript] = useState<string[]>([])
  const [documentRoots, setDocumentRoots] = useState<ApprovedDocumentRoot[]>([])
  const [searchQuery, setSearchQuery] = useState('resume')
  const [searchResults, setSearchResults] = useState<DocumentSearchResult[]>([])
  const [searchFallback, setSearchFallback] = useState(false)
  const [thumbnails, setThumbnails] = useState<Map<string, ResultThumbnail>>(new Map())
  const [isSearching, setIsSearching] = useState(false)
  const [error, setError] = useState<string>()
  const [pausedNotice, setPausedNotice] = useState<string>()
  const [isConnecting, setIsConnecting] = useState(false)
  const [isCapturing, setIsCapturing] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)
  const [isChoosingFolder, setIsChoosingFolder] = useState(false)
  const [pendingScreenCaptureCall, setPendingScreenCaptureCall] = useState<RealtimeServerCall | null | undefined>()
  const telegramServerCallRef = useRef<RealtimeServerCall | undefined>(undefined)
  const [telegramStatus, setTelegramStatus] = useState<TelegramStatus>({ state: 'disconnected' })
  const [telegramQuery, setTelegramQuery] = useState('')
  const [telegramRecipients, setTelegramRecipients] = useState<TelegramRecipient[]>([])
  const [selectedTelegramRecipientId, setSelectedTelegramRecipientId] = useState<string>()
  const [telegramMessage, setTelegramMessage] = useState('')
  const [telegramPassword, setTelegramPassword] = useState('')
  const [isTelegramWorking, setIsTelegramWorking] = useState(false)

  const clearExpiredTelegramQr = useCallback((qrUrl: string): void => {
    setTelegramStatus((current) => current.qrUrl === qrUrl
      ? { state: 'connecting', message: 'This Telegram QR code expired. Waiting for a refreshed code.' }
      : current)
  }, [])

  useEffect(() => {
    expandedRef.current = expanded
    window.lifeLens.setPanelOpen(expanded)
    // Collapsing the companion must not leave an open microphone behind the orb.
    const client = clientRef.current
    client?.setListening(expanded)
    if (!expanded) {
      collapsedAtRef.current = Date.now()
      client?.startCollapseDisconnect(collapsedAtRef.current)
    } else {
      collapsedAtRef.current = undefined
      client?.cancelCollapseDisconnect()
    }
  }, [expanded])

  useEffect(() => {
    void refreshDocumentRoots()
    void refreshTelegramStatus()
    const removeSearchListener = window.lifeLens.onFileSearchResolved((resolution) => controllerRef.current?.resolve(resolution))
    const removeTelegramListener = window.lifeLens.onTelegramAuthUpdate((status) => {
      setTelegramStatus(status)
      if (status.state !== 'connected') {
        setTelegramRecipients([])
        setSelectedTelegramRecipientId(undefined)
      }
      if (status.state === 'connected') {
        setTelegramPassword('')
      }
    })
    return () => {
      removeSearchListener()
      removeTelegramListener()
      void window.lifeLens.cancelFileSearch()
      // No approved-but-unsent photo may outlive the session.
      void window.lifeLens.cancelPhotoAnalysis()
      clientRef.current?.disconnect()
    }
  }, [])

  const appendTranscript = (text: string): void => {
    setTranscript((current) => [...current, text].slice(-8))
  }

  const updateDocumentRoots = (roots: ApprovedDocumentRoot[]): void => {
    setDocumentRoots(roots)
    clientRef.current?.setApprovedRoots(roots)
  }

  async function refreshDocumentRoots(): Promise<void> {
    try {
      updateDocumentRoots(await window.lifeLens.listDocumentRoots())
    } catch (rootError) {
      setError(messageFrom(rootError))
    }
  }

  async function refreshTelegramStatus(): Promise<void> {
    try {
      setTelegramStatus(await window.lifeLens.getTelegramStatus())
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    }
  }

  function expireServerGeneration(generation: number): void {
    if (controllerRef.current?.expireGeneration(generation)) {
      void window.lifeLens.cancelFileSearch()
    }
    setSearchConfirmation((current) => current?.serverCall?.generation === generation ? undefined : current)
    setPendingScreenCaptureCall((current) => {
      if (current && current.generation === generation) {
        setCapturePickerOpen(false)
        return undefined
      }
      return current
    })
    if (telegramServerCallRef.current?.generation === generation) {
      telegramServerCallRef.current = undefined
      setTelegramRecipients([])
      setSelectedTelegramRecipientId(undefined)
      setIsTelegramWorking(false)
    }
    for (const [approvalId, pending] of pendingProposalsRef.current) {
      if (pending.serverCall?.generation !== generation) {
        continue
      }
      pendingProposalsRef.current.delete(approvalId)
      void window.lifeLens.cancelPendingAction(approvalId)
      setPendingAction((current) => current?.approvalId === approvalId ? undefined : current)
    }
  }

  function isPendingProposalCurrent(pending: PendingProposal | undefined): boolean {
    return pending?.serverCall === undefined || clientRef.current?.isServerCallActive(pending.serverCall) === true
  }

  const connectVoice = (): Promise<void> => {
    if (connectPromiseRef.current) {
      return connectPromiseRef.current
    }

    const connection = (async (): Promise<void> => {
      setError(undefined)
      setPausedNotice(undefined)
      setIsConnecting(true)
      setCompanionState('thinking')
      const previousGeneration = clientRef.current?.disconnect()
      if (previousGeneration !== undefined) {
        expireServerGeneration(previousGeneration)
      }
      void window.lifeLens.cancelPhotoAnalysis()
      try {
        const credential = await window.lifeLens.createRealtimeSession()
        const client = new RealtimeClient({
          onState: setCompanionState,
          onTranscript: appendTranscript,
          onExplanation: setExplanation,
          onCaptureContextRequest: requestScreenContext,
          onFileSearchRequest: (request, serverCall) => { void controllerRef.current?.run(request, serverCall) },
          onUserTranscript: (text) => window.lifeLens.noteUserRequest(text).then(() => undefined, () => undefined),
          onTelegramRecipientSearch: requestTelegramRecipientSearch,
          onToolProposal: (proposal, serverCall) => { void preparePendingAction(proposal, serverCall) },
          onError: setError,
          onSessionEnded: (reason, generation) => {
            expireServerGeneration(generation)
            if (reason === 'error') {
              setCompanionState('error')
              return
            }
            setCompanionState('idle')
            setPausedNotice(VOICE_PAUSED_NOTICE)
          },
          evaluateToolPolicy: (toolName) => window.lifeLens.evaluateToolRequest(toolName)
        })
        client.setApprovedRoots(documentRoots)
        clientRef.current = client
        setMode(credential.mode)
        const pendingConnection = client.connect(credential, {
          greet: credential.mode === 'live' ? !hasConnectedOnceRef.current : true
        })
        // If the user collapsed while the credential was being minted, carry
        // that privacy choice into the session before its initial update.
        client.setListening(expandedRef.current)
        await pendingConnection
        if (!expandedRef.current && collapsedAtRef.current !== undefined) {
          client.startCollapseDisconnect(collapsedAtRef.current)
        }
        if (credential.mode === 'live') {
          hasConnectedOnceRef.current = true
        }
      } catch (connectionError) {
        clientRef.current?.disconnect()
        clientRef.current = undefined
        setCompanionState('error')
        setError(messageFrom(connectionError))
        throw connectionError
      } finally {
        setIsConnecting(false)
      }
    })()
    connectPromiseRef.current = connection
    const clearConnection = (): void => {
      if (connectPromiseRef.current === connection) {
        connectPromiseRef.current = undefined
      }
    }
    void connection.then(clearConnection, clearConnection)
    return connection
  }

  const ensureConnected = async (): Promise<void> => {
    if (clientRef.current?.isConnected()) {
      return
    }
    await connectVoice()
  }

  const openCompanion = (): void => {
    setExpanded(true)
    if (!clientRef.current?.isConnected()) {
      void connectVoice().catch(() => undefined)
    }
  }

  const loadCaptureSources = async (serverCall?: RealtimeServerCall): Promise<void> => {
    setError(undefined)
    if (serverCall !== undefined) {
      setPendingScreenCaptureCall(serverCall)
    }
    try {
      const sources = await window.lifeLens.listCaptureSources()
      if (serverCall && !clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      setCaptureSources(sources)
      setCapturePickerOpen(true)
    } catch (sourceError) {
      if (serverCall && !clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      setError(messageFrom(sourceError))
    }
  }

  const captureScreen = async (serverCall?: RealtimeServerCall, sourceId = selectedCaptureSourceId): Promise<void> => {
    setError(undefined)
    setIsCapturing(true)
    setCompanionState('thinking')
    try {
      await ensureConnected()
      const client = clientRef.current
      if (!client) {
        throw new Error('Connect voice first, then capture the visible screen.')
      }
      const nextCapture = await window.lifeLens.captureScreen(sourceId)
      if (serverCall && !client.isServerCallActive(serverCall)) {
        return
      }
      setCapture(nextCapture)
      setExplanation(undefined)
      setPendingAction(undefined)
      setToolResult(undefined)
      setSearchResults([])
      await client.provideScreenContext(nextCapture, serverCall)
    } catch (captureError) {
      if (serverCall && !clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      if (sourceId && isSelectedSourceUnavailable(captureError)) {
        setSelectedCaptureSourceId(undefined)
        clientRef.current?.invalidateScreenContext()
        setCapture(undefined)
        setExplanation(undefined)
        setPendingScreenCaptureCall(serverCall ?? null)
        void loadCaptureSources()
      } else {
        setCompanionState('error')
        setError(messageFrom(captureError))
      }
    } finally {
      setIsCapturing(false)
    }
  }

  const requestScreenContext = (serverCall?: RealtimeServerCall): void => {
    const client = clientRef.current
    if (!client) {
      setError('Connect voice first, then ask Lumi about the visible screen.')
      return
    }

    if (!selectedCaptureSourceId) {
      setPendingScreenCaptureCall(serverCall ?? null)
      void loadCaptureSources()
      return
    }

    void captureScreen(serverCall)
  }

  const askQuestion = async (): Promise<void> => {
    try {
      setError(undefined)
      await ensureConnected()
      const client = clientRef.current
      if (!client) {
        throw new Error('Connect voice first, then ask Lumi a question.')
      }
      try {
        await window.lifeLens.noteUserRequest(question)
      } catch {
        // Intent tracking is advisory; the request itself still proceeds.
      }
      await client.sendUserRequest(question)
      setQuestion('')
    } catch (requestError) {
      setCompanionState('error')
      setError(messageFrom(requestError))
    }
  }

  const chooseDocumentRoot = async (): Promise<void> => {
    setError(undefined)
    setIsChoosingFolder(true)
    try {
      const root = await window.lifeLens.chooseDocumentRoot()
      if (root) {
        updateDocumentRoots(await window.lifeLens.listDocumentRoots())
      }
    } catch (folderError) {
      setError(messageFrom(folderError))
    } finally {
      setIsChoosingFolder(false)
    }
  }

  const applySearchResults = (payload: FileSearchResults): void => {
    setSearchResults(payload.results)
    setSearchFallback(payload.fallback)
    setThumbnails(new Map())
    clientRef.current?.setSearchOrdinals(payload.results.map((result) => result.id))
    void loadThumbnails(payload.results)
  }

  /** Previews are built in main and stay local; nothing here reaches the model. */
  async function loadThumbnails(results: DocumentSearchResult[]): Promise<void> {
    const imageResults = results.filter((result) => result.kind === 'photo' || result.kind === 'screenshot')
    if (imageResults.length === 0) {
      return
    }

    try {
      const loaded = await window.lifeLens.getResultThumbnails(imageResults.map((result) => result.id))
      setThumbnails(new Map(loaded.map((thumbnail) => [thumbnail.resultId, thumbnail])))
    } catch {
      // A preview failure must never break the result list itself.
      setThumbnails(new Map(imageResults.map((result) => [result.id, { resultId: result.id, status: 'unavailable' as const }])))
    }
  }

  /** One explicit in-app confirmation before a single photo is ever sent. */
  const proposeAnalyzePhoto = (result: DocumentSearchResult): void => {
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'analyze_photo',
      reason: `Send only ${result.name} to OpenAI so Lumi can answer your question about it.`,
      requiresConfirmation: true,
      arguments: { resultId: result.id, question: question.trim() || 'What is in this photo?' }
    })
  }

  // The single entry point for every stored-file search: model tool calls, mock
  // voice, and the panel field. It keeps a folderless search off the generic
  // create-pending-action path and routes a fail-closed search to its own
  // confirmation, which re-enters the orchestrator rather than assuming a root.
  if (!controllerRef.current) {
    controllerRef.current = new FileSearchController({
      begin: (request) => window.lifeLens.beginFileSearch(request),
      chooseFolder: () => chooseDocumentRoot(),
      completeCall: (serverCall, result) => clientRef.current?.completeFileSearch(serverCall, result),
      applyResults: (results) => applySearchResults(results),
      presentConfirmation: (request) => setSearchConfirmation(request),
      setSearching: (searching) => setIsSearching(searching),
      setError: (message) => setError(message),
      setListening: () => setCompanionState('listening')
    })
  }
  const controller = controllerRef.current

  const proposeDocumentSearch = (): void => {
    const query = searchQuery.trim()
    if (!query) {
      setError('Enter what you are looking for first.')
      return
    }

    void controller.run({ queryTerms: query }, undefined, 'user')
  }

  const proposeOpenFile = (result: DocumentSearchResult): void => {
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'open_file',
      reason: `Open ${result.name}, which was returned by your approved-folder search.`,
      requiresConfirmation: true,
      arguments: { resultId: result.id }
    })
  }

  const proposeOpenUrl = (url: string): void => {
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'open_url',
      reason: 'Open the link extracted from the captured screen in your default browser.',
      requiresConfirmation: true,
      arguments: { url }
    })
  }

  const proposeSaveContext = (): void => {
    const sourceContext = currentSourceContext(capture, explanation)
    if (!sourceContext) {
      setError('Capture a screen and wait for its explanation before saving context.')
      return
    }

    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'save_context',
      reason: 'Keep the interview summary and extracted signals for a later reminder or follow-up.',
      requiresConfirmation: true,
      arguments: { label: 'Interview screen context', sourceContext }
    })
  }

  const connectTelegram = async (): Promise<void> => {
    setError(undefined)
    setIsTelegramWorking(true)
    try {
      setTelegramStatus(await window.lifeLens.connectTelegram())
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    } finally {
      setIsTelegramWorking(false)
    }
  }

  const cancelTelegramConnect = async (): Promise<void> => {
    setIsTelegramWorking(true)
    try {
      setTelegramStatus(await window.lifeLens.cancelTelegramConnect())
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    } finally {
      setIsTelegramWorking(false)
    }
  }

  const submitTelegramPassword = async (): Promise<void> => {
    if (!telegramPassword) {
      return
    }
    setIsTelegramWorking(true)
    try {
      setTelegramStatus(await window.lifeLens.submitTelegramPassword(telegramPassword))
      setTelegramPassword('')
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    } finally {
      setIsTelegramWorking(false)
    }
  }

  const logoutTelegram = async (): Promise<void> => {
    setIsTelegramWorking(true)
    try {
      setTelegramStatus(await window.lifeLens.logoutTelegram())
      setTelegramRecipients([])
      setSelectedTelegramRecipientId(undefined)
      setTelegramMessage('')
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    } finally {
      setIsTelegramWorking(false)
    }
  }

  const searchTelegramRecipients = async (): Promise<void> => {
    if (!telegramQuery.trim()) {
      setError('Enter a recipient name to search Telegram.')
      return
    }
    setError(undefined)
    setIsTelegramWorking(true)
    try {
      const recipients = await window.lifeLens.searchTelegramRecipients(telegramQuery)
      setTelegramRecipients(recipients)
      setSelectedTelegramRecipientId(undefined)
    } catch (telegramError) {
      setError(messageFrom(telegramError))
    } finally {
      setIsTelegramWorking(false)
    }
  }

  const requestTelegramRecipientSearch = (query: string, serverCall: RealtimeServerCall): void => {
    telegramServerCallRef.current = serverCall
    setTelegramQuery(query)
    setError(undefined)
    setIsTelegramWorking(true)
    void window.lifeLens.searchTelegramRecipients(query).then((recipients) => {
      if (!clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      setTelegramRecipients(recipients)
      setSelectedTelegramRecipientId(undefined)
      clientRef.current.completeTelegramRecipientSearch(serverCall, recipients.length)
    }).catch((telegramError) => {
      if (!clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      const message = messageFrom(telegramError)
      setError(message)
      clientRef.current.completeTelegramRecipientSearch(serverCall, 0)
    }).finally(() => {
      if (telegramServerCallRef.current === serverCall) {
        telegramServerCallRef.current = undefined
        setIsTelegramWorking(false)
      }
    })
  }

  const proposeTelegramMessage = (): void => {
    if (!selectedTelegramRecipientId || !telegramMessage.trim()) {
      setError('Choose one Telegram recipient and enter the full message first.')
      return
    }
    const recipient = telegramRecipients.find((candidate) => candidate.resultId === selectedTelegramRecipientId)
    if (!recipient) {
      setError('That Telegram recipient is no longer available. Search again.')
      return
    }
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'send_telegram_message',
      reason: 'Send this one plain-text message from your connected personal Telegram account.',
      requiresConfirmation: true,
      arguments: { recipientResultId: recipient.resultId, message: telegramMessage.trim() }
    })
  }

  const preparePendingAction = async (proposal: ToolProposal, serverCall?: RealtimeServerCall): Promise<void> => {
    setError(undefined)
    setToolResult(undefined)
    try {
      const action = await window.lifeLens.createPendingAction(proposal)
      if (serverCall && !clientRef.current?.isServerCallActive(serverCall)) {
        await window.lifeLens.cancelPendingAction(action.approvalId)
        return
      }
      pendingProposalsRef.current.set(action.approvalId, { proposal, serverCall })
      setPendingAction(action)
    } catch (pendingError) {
      if (serverCall && !clientRef.current?.isServerCallActive(serverCall)) {
        return
      }
      const message = messageFrom(pendingError)
      clientRef.current?.sendToolResult(proposal, { ok: false, message }, serverCall)
      setCompanionState('error')
      setError(message)
    }
  }

  const confirmPendingAction = async (approvalId: string): Promise<void> => {
    const pending = pendingProposalsRef.current.get(approvalId)
    const proposal = pending?.proposal
    setError(undefined)
    setIsConfirming(true)
    try {
      const result = await window.lifeLens.approvePendingAction(approvalId)
      if (!isPendingProposalCurrent(pending)) {
        pendingProposalsRef.current.delete(approvalId)
        setPendingAction((current) => current?.approvalId === approvalId ? undefined : current)
        return
      }
      setToolResult(result)
      if (proposal) clientRef.current?.sendToolResult(proposal, result, pending?.serverCall)
      pendingProposalsRef.current.delete(approvalId)
      setPendingAction((current) => current?.approvalId === approvalId ? undefined : current)
      if (result.searchResults) {
        applySearchResults({
          results: result.searchResults,
          compactResults: result.compactResults ?? [],
          fallback: result.searchFallback ?? false,
          message: result.message
        })
      }

      // Main approved exactly one image for this turn; send it through the
      // existing Realtime image path with the user's own question.
      if (result.analysisImage && proposal?.toolName === 'analyze_photo') {
        await ensureConnected()
        const client = clientRef.current
        if (!client) {
          throw new Error('Connect voice before asking Lumi about a photo.')
        }
        await client.analyzeSelectedPhoto(result.analysisImage, proposal.arguments.question ?? '')
      }

      if (result.ok) {
        setCompanionState('success')
      } else if (/cancelled|declined/i.test(result.message)) {
        setCompanionState('listening')
      } else {
        setCompanionState('error')
      }
    } catch (toolError) {
      if (!isPendingProposalCurrent(pending)) {
        return
      }
      const message = messageFrom(toolError)
      if (proposal) clientRef.current?.sendToolResult(proposal, { ok: false, message }, pending?.serverCall)
      setCompanionState('error')
      setError(message)
    } finally {
      setIsConfirming(false)
    }
  }

  const dismissPendingAction = async (approvalId: string): Promise<void> => {
    const pending = pendingProposalsRef.current.get(approvalId)
    const proposal = pending?.proposal
    try {
      await window.lifeLens.cancelPendingAction(approvalId)
      if (proposal) clientRef.current?.declineToolProposal(proposal, pending?.serverCall)
      pendingProposalsRef.current.delete(approvalId)
      setPendingAction((current) => current?.approvalId === approvalId ? undefined : current)
      setToolResult({ ok: false, message: 'Cancelled. Nothing was changed, opened, or sent.' })
      setCompanionState('listening')
    } catch (cancelError) {
      setError(messageFrom(cancelError))
    }
  }

  const selectCaptureSource = (source: CaptureSource): void => {
    setSelectedCaptureSourceId(source.id)
    setCapturePickerOpen(false)
    clientRef.current?.invalidateScreenContext()
    setCapture(undefined)
    setExplanation(undefined)
    const serverCall = pendingScreenCaptureCall ?? undefined
    setPendingScreenCaptureCall(undefined)
    if (pendingScreenCaptureCall !== undefined) {
      void captureScreen(serverCall, source.id)
    }
  }

  // A result set is a photo set when every result is an image, so a mixed
  // document search keeps its list view.
  const showsImageResults = searchResults.length > 0 &&
    searchResults.every((result) => result.kind === 'photo' || result.kind === 'screenshot')
  const applicationWindows = captureSources.filter((source) => source.kind === 'window')
  const entireDisplays = captureSources.filter((source) => source.kind === 'screen')
  const visibleLinks = explanation?.signals.filter((signal) => signal.kind === 'link') ?? []

  return (
    <main className={`app-shell ${expanded ? 'is-open' : 'is-closed'}`}>
      <div className="companion-shell" title="Drag the outer ring to move LifeLens">
        <button className={`companion-core state-${companionState}`} type="button" aria-label="Open LifeLens" onClick={openCompanion}>
          <span className="companion-eye left" />
          <span className="companion-eye right" />
          <span className="companion-glow" />
        </button>
      </div>

      {expanded && (
        <section className="panel" aria-label="LifeLens interaction panel">
          <header className="panel-header drag-handle">
            <div>
              <p className="eyebrow">LIFELENS</p>
              <h1>See it. Understand it.</h1>
            </div>
            <button className="icon-button" type="button" aria-label="Close LifeLens panel" onClick={() => setExpanded(false)}>&times;</button>
          </header>

          <div className="status-row" aria-live="polite">
            <span className={`status-dot state-${companionState}`} />
            <span>{isConnecting ? (hasConnectedOnceRef.current ? 'Reconnecting…' : 'Connecting…') : STATUS_LABELS[companionState]}</span>
            {mode && <span className="mode-badge">{mode === 'live' ? 'Realtime voice' : 'Mock voice'}</span>}
          </div>

          <form className="question-field" onSubmit={(event) => { event.preventDefault(); void askQuestion() }}>
            <span>Ask Lumi</span>
            <input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What is this email about?" />
            <button className="primary-button" type="submit" disabled={!clientRef.current || !question.trim()}>Ask Lumi</button>
          </form>

          {capturePickerOpen && (
            <section className="source-picker" aria-label="Choose a screen or window to capture">
              <div className="section-heading-row">
                <div><p className="eyebrow">CAPTURE SOURCE</p><h2>Choose once, then capture</h2></div>
                <button className="text-button" type="button" onClick={() => {
                  clientRef.current?.declineScreenContext(pendingScreenCaptureCall ?? undefined)
                  setPendingScreenCaptureCall(undefined)
                  setCapturePickerOpen(false)
                }}>Close</button>
              </div>
              {captureSources.length === 0 ? <p className="notice">No capturable sources are available right now.</p> : (
                <>
                  <CaptureSourceSection
                    title="Application windows"
                    sources={applicationWindows}
                    selectedSourceId={selectedCaptureSourceId}
                    recommended={!selectedCaptureSourceId}
                    onSelect={selectCaptureSource}
                  />
                  <CaptureSourceSection
                    title="Entire displays"
                    sources={entireDisplays}
                    selectedSourceId={selectedCaptureSourceId}
                    onSelect={selectCaptureSource}
                  />
                </>
              )}
            </section>
          )}

          {error && <p className="notice error-notice">{error}</p>}
          {pausedNotice && <p className="notice">{pausedNotice}</p>}
          {isCapturing && <p className="notice privacy-notice"><strong>Looking at your screen</strong> once to answer this request. Lumi does not save or continuously monitor it.</p>}
          {mode === 'mock' && <p className="notice">Demo mode is active because no API key is configured. It exercises the same capture and confirmation path.</p>}

          {capture && (
            <figure className="capture-card">
              <img src={capture.dataUrl} alt={`Preview of ${capture.label}`} />
              <figcaption>Captured {new Date(capture.capturedAt).toLocaleTimeString()} from {capture.sourceKind === 'screen' ? 'screen' : 'window'}</figcaption>
            </figure>
          )}

          {explanation && <ExplanationCard explanation={explanation} />}
          {explanation && (
            <section className="context-actions" aria-label="Screen follow-up actions">
              <p className="eyebrow">FOLLOW-UP</p>
              <div className="actions">
                <button className="secondary-button" type="button" onClick={proposeSaveContext}>Save context</button>
                {visibleLinks.slice(0, 2).map((signal, index) => (
                  <button className="secondary-button" type="button" key={`${signal.value}-${index}`} onClick={() => proposeOpenUrl(signal.value)}>Open extracted link</button>
                ))}
              </div>
            </section>
          )}

          <section className="document-workspace" aria-label="Approved document search">
            <div className="section-heading-row">
              <div><p className="eyebrow">APPROVED DOCUMENTS</p><h2>Find the latest resume</h2></div>
              <button className="text-button" type="button" disabled={isChoosingFolder} onClick={() => void chooseDocumentRoot()}>
                {isChoosingFolder ? 'Choosing...' : 'Approve folder'}
              </button>
            </div>
            {documentRoots.length === 0
              ? <p className="workspace-note">LifeLens cannot search until you approve a folder. Ask for a file and it will offer the folder chooser once.</p>
              : <p className="workspace-note">Searching {documentRoots.map((root) => root.label).join(', ')}.</p>}
            <div className="document-search-row">
              <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="resume" aria-label="What to look for" />
              <button className="secondary-button" type="button" disabled={isSearching} onClick={proposeDocumentSearch}>
                {isSearching ? 'Searching...' : 'Search files'}
              </button>
            </div>
            {searchResults.length > 0 && (
              <>
                <p className="workspace-note">
                  {showsImageResults
                    ? 'I can search photo names, folders, and dates, but I cannot recognise the contents of every photo automatically. Here are the closest local matches. Choose one and I can look at that selected photo.'
                    : searchFallback
                      ? 'No filename matched, so these are possible recent matches, newest first.'
                      : 'Best matches, newest first.'}
                </p>
                {showsImageResults ? (
                  <PhotoResultGrid
                    results={searchResults}
                    thumbnails={thumbnails}
                    fallback={searchFallback}
                    onOpen={proposeOpenFile}
                    onAnalyze={proposeAnalyzePhoto}
                  />
                ) : (
                  <ul className="search-results">
                    {searchResults.map((result, index) => (
                      <li key={result.id}>
                        <div>
                          <strong>{index + 1}. {result.name}</strong>
                          <span>{result.relativePath}</span>
                          <span>{fileKindLabel(result.kind)} · {new Date(result.modifiedAt).toLocaleDateString()}</span>
                        </div>
                        <button className="text-button" type="button" onClick={() => proposeOpenFile(result)}>Open file</button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </section>

          <section className="telegram-workspace" aria-label="Telegram integration">
            <div className="section-heading-row">
              <div><p className="eyebrow">INTEGRATIONS</p><h2>Telegram <span className="unofficial-label">Unofficial</span></h2></div>
              {telegramStatus.state === 'connected' ? (
                <button className="text-button" type="button" disabled={isTelegramWorking} onClick={() => void logoutTelegram()}>Log out</button>
              ) : (
                <button className="text-button" type="button" disabled={isTelegramWorking || telegramStatus.state === 'connecting'} onClick={() => void connectTelegram()}>
                  {isTelegramWorking || telegramStatus.state === 'connecting' ? 'Connecting...' : 'Connect Telegram'}
                </button>
              )}
            </div>
            {telegramStatus.state === 'disconnected' && <p className="workspace-note">Connect a personal account to search recipient metadata locally and send one confirmed plain-text message.</p>}
            {(telegramStatus.state === 'connecting' || telegramStatus.state === 'awaiting_2fa') && (
              <div className="telegram-auth-card">
                {telegramStatus.qrUrl ? (
                  <>
                    <p>Open Telegram on your phone: <strong>Settings → Devices → Link Desktop Device</strong>, then scan the current login QR token.</p>
                    <TelegramLoginQr qrUrl={telegramStatus.qrUrl} expiresAt={telegramStatus.expiresAt} onExpire={clearExpiredTelegramQr} />
                  </>
                ) : <p>{telegramStatus.message ?? 'Preparing a Telegram QR token…'}</p>}
                {telegramStatus.state === 'awaiting_2fa' && (
                  <form className="telegram-password-row" onSubmit={(event) => { event.preventDefault(); void submitTelegramPassword() }}>
                    <input type="password" autoComplete="current-password" value={telegramPassword} onChange={(event) => setTelegramPassword(event.target.value)} placeholder="Telegram two-step password" aria-label="Telegram two-step verification password" />
                    <button className="secondary-button" type="submit" disabled={!telegramPassword || isTelegramWorking}>Continue</button>
                  </form>
                )}
                <button className="text-button" type="button" disabled={isTelegramWorking} onClick={() => void cancelTelegramConnect()}>Cancel</button>
              </div>
            )}
            {telegramStatus.state === 'error' && <p className="notice error-notice">{telegramStatus.message}</p>}
            {telegramStatus.state === 'connected' && (
              <>
                <p className="workspace-note">Connected as <strong>{formatTelegramAccount(telegramStatus)}</strong>. Recipient names and chat metadata stay local to Lumi.</p>
                <div className="document-search-row">
                  <input value={telegramQuery} onChange={(event) => setTelegramQuery(event.target.value)} placeholder="Recipient name" aria-label="Telegram recipient name" />
                  <button className="secondary-button" type="button" disabled={isTelegramWorking} onClick={() => void searchTelegramRecipients()}>Find</button>
                </div>
                {telegramRecipients.length > 0 && (
                  <ul className="telegram-recipients">
                    {telegramRecipients.map((recipient) => (
                      <li key={recipient.resultId}>
                        <label>
                          <input type="radio" name="telegram-recipient" checked={selectedTelegramRecipientId === recipient.resultId} onChange={() => setSelectedTelegramRecipientId(recipient.resultId)} />
                          <span><strong>{recipient.displayName}</strong>{recipient.username && <small>@{recipient.username}</small>}<small>{recipient.kind}</small></span>
                        </label>
                      </li>
                    ))}
                  </ul>
                )}
                {telegramQuery && telegramRecipients.length === 0 && <p className="workspace-note">Search returns up to ten local dialog/contact matches. Nothing is sent to OpenAI.</p>}
                <label className="telegram-message-field">
                  <span>Message to send</span>
                  <textarea value={telegramMessage} maxLength={4096} onChange={(event) => setTelegramMessage(event.target.value)} placeholder="Write the complete message" />
                </label>
                <button className="secondary-button" type="button" onClick={proposeTelegramMessage}>Send message</button>
              </>
            )}
          </section>

          <details className="troubleshooting">
            <summary>More / Troubleshooting</summary>
            <div className="actions">
              <button className="secondary-button" type="button" onClick={() => { void connectVoice().catch(() => undefined) }} disabled={isConnecting}>
                {isConnecting ? (hasConnectedOnceRef.current ? 'Reconnecting…' : 'Connecting…') : 'Connect voice'}
              </button>
              <button className="secondary-button" type="button" onClick={() => requestScreenContext()} disabled={!clientRef.current || isCapturing}>
                {isCapturing ? 'Looking...' : capture ? 'Refresh screen' : 'Capture screen'}
              </button>
              <button className="text-button" type="button" onClick={() => void loadCaptureSources()}>Change screen</button>
            </div>
          </details>

          {searchConfirmation && (
            <SearchConfirmationCard
              request={searchConfirmation}
              onConfirm={() => void controller.confirm(searchConfirmation)}
              onDismiss={() => controller.decline(searchConfirmation)}
            />
          )}
          {pendingAction && (
            <ToolConfirmationCard
              action={pendingAction}
              isConfirming={isConfirming}
              onConfirm={confirmPendingAction}
              onDismiss={dismissPendingAction}
            />
          )}
          {toolResult && <p className={`notice ${toolResult.ok ? 'success-notice' : 'error-notice'}`}>{toolResult.message}</p>}
          {transcript.length > 0 && (
            <details className="transcript">
              <summary>Conversation</summary>
              {transcript.map((line, index) => <p key={`${line}-${index}`}>{line}</p>)}
            </details>
          )}
        </section>
      )}
    </main>
  )
}

function currentSourceContext(capture: CaptureResult | undefined, explanation: Explanation | undefined): SourceContext | undefined {
  if (!capture || !explanation) {
    return undefined
  }
  return {
    captureId: capture.id,
    summary: explanation.summary,
    capturedAt: capture.capturedAt,
    signals: explanation.signals
  }
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : 'LifeLens encountered an unexpected error.'
}

/**
 * The approval surface for a search LifeLens could not yet trust (a late voice
 * transcript, or a model-initiated request). Approving it continues through the
 * SearchOrchestrator as an explicit request, including opening the folder
 * chooser when none is approved; it never touches the create-pending-action path.
 */
function SearchConfirmationCard({
  request,
  onConfirm,
  onDismiss
}: {
  request: SearchConfirmationRequest
  onConfirm: () => void
  onDismiss: () => void
}) {
  return (
    <article className="notice" aria-label="Confirm a stored-file search">
      <p className="eyebrow">READY TO SEARCH</p>
      <p>Search your approved folders for <strong>{request.input.queryTerms}</strong>? If no folder is approved yet, you will choose one next. LifeLens searches only if you confirm.</p>
      <div className="actions">
        <button className="secondary-button" type="button" onClick={onConfirm}>Search files</button>
        <button className="text-button" type="button" onClick={onDismiss}>Cancel</button>
      </div>
    </article>
  )
}

function formatTelegramAccount(status: TelegramStatus): string {
  const account = status.account
  if (!account) {
    return 'your personal account'
  }
  return account.username ? `${account.displayName} (@${account.username})` : account.displayName
}

function TelegramLoginQr({
  qrUrl,
  expiresAt,
  onExpire
}: {
  qrUrl: string
  expiresAt?: string
  onExpire: (qrUrl: string) => void
}) {
  const [imageUrl, setImageUrl] = useState<string>()
  const [renderError, setRenderError] = useState<string>()

  useEffect(() => {
    let active = true
    setImageUrl(undefined)
    setRenderError(undefined)
    void QRCode.toDataURL(qrUrl, {
      width: 208,
      margin: 1,
      errorCorrectionLevel: 'M',
      color: { dark: '#09142e', light: '#f7fbff' }
    }).then((nextImageUrl) => {
      if (active) {
        setImageUrl(nextImageUrl)
      }
    }).catch(() => {
      if (active) {
        setRenderError('LifeLens could not render this Telegram QR code. Wait for a refreshed code and try again.')
      }
    })

    const expiration = expiresAt ? Date.parse(expiresAt) : Number.NaN
    const timeout = Number.isFinite(expiration)
      ? window.setTimeout(() => onExpire(qrUrl), Math.max(0, expiration - Date.now()))
      : undefined
    return () => {
      active = false
      setImageUrl(undefined)
      if (timeout !== undefined) {
        window.clearTimeout(timeout)
      }
    }
  }, [expiresAt, onExpire, qrUrl])

  if (renderError) {
    return <p className="notice error-notice">{renderError}</p>
  }

  return (
    <div className="telegram-qr-code" aria-live="polite">
      {imageUrl ? <img src={imageUrl} alt="Scan this temporary Telegram login QR code with Telegram on your phone." /> : <p>Rendering a local QR code…</p>}
      {expiresAt && <p className="workspace-note">This code refreshes automatically. Current code expires {new Date(expiresAt).toLocaleTimeString()}.</p>}
    </div>
  )
}

function isSelectedSourceUnavailable(error: unknown): boolean {
  return error instanceof Error && /selected capture source is no longer available/i.test(error.message)
}

function CaptureSourceSection({
  title,
  sources,
  selectedSourceId,
  recommended = false,
  onSelect
}: {
  title: string
  sources: CaptureSource[]
  selectedSourceId: string | undefined
  recommended?: boolean
  onSelect: (source: CaptureSource) => void
}) {
  if (sources.length === 0) {
    return null
  }

  return (
    <section className="capture-source-section" aria-label={title}>
      <h3>{title}{recommended && <span>Recommended</span>}</h3>
      <div className="source-grid">
        {sources.map((source, index) => (
          <button
            className={`source-option ${source.id === selectedSourceId ? 'is-selected' : ''} ${recommended && index === 0 ? 'is-recommended' : ''}`}
            type="button"
            key={source.id}
            onClick={() => onSelect(source)}
          >
            <img src={source.thumbnailDataUrl} alt="" />
            <span>{source.label}{recommended && index === 0 ? ' (recommended)' : ''}</span>
          </button>
        ))}
      </div>
    </section>
  )
}
