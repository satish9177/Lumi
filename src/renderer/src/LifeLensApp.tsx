import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import type {
  ApprovedDocumentRoot,
  CaptureResult,
  CaptureSource,
  CompanionState,
  DocumentSearchResult,
  DroppedFileDescriptor,
  Explanation,
  FileSearchResults,
  PeopleEnrolmentView,
  PeopleSearchStatus,
  PendingActionPreview,
  PhotoSearchStatus,
  RealtimeConfigurationStatus,
  RealtimeMode,
  ResultThumbnail,
  ScreenReasoningSummary,
  SourceContext,
  TelegramRecipient,
  TelegramStatus,
  ToolExecutionResult,
  ToolProposal
} from '../../shared/contracts'
import { fileKindLabel } from '../../shared/search-query'
import {
  BrandMark,
  ConfirmDialog,
  DragGrip,
  DropOverlay,
  DroppedFileCard,
  ExplanationCard,
  PeopleSettings,
  PhotoResultGrid,
  StatusPill,
  ToolConfirmationCard
} from './components'
import type { ConfirmRequest, RequestConfirmation } from './components'
import { COPY } from './copy'
import { countDraggedFiles, decideDrop, preventFileNavigation, TOO_MANY_FILES_MESSAGE } from './drop-intake'
import { focusableWithin, nextTrappedFocus } from './focus-trap'
import { deriveStatus, statusAnnouncement } from './status'

/** Honest copy: Lumi can move a document, but it cannot read one. */
const DOCUMENT_ANALYSIS_NOTICE =
  "Lumi can open this file or send it on Telegram. Reading its contents isn't supported yet."
import { FileSearchController, type SearchConfirmationRequest } from './file-search-controller'
import { messageFrom } from './error-message'
import { approvePendingRendererAction, dismissPendingRendererAction } from './pending-action-coordinator'
import {
  RealtimeClient,
  type RealtimeServerCall,
  type TelegramAttachmentCoordinationRequest
} from './realtime'

const VOICE_PAUSED_NOTICE = 'Voice paused to save cost — ask a question to reconnect.'

interface PendingProposal {
  readonly proposal: ToolProposal
  readonly serverCall?: RealtimeServerCall
}

interface PendingTelegramAttachment {
  readonly request: TelegramAttachmentCoordinationRequest
  readonly serverCall: RealtimeServerCall
}

/** Empty-state chips. These only prefill the composer; they never run on their own. */
const SUGGESTIONS = ['What is this email about?', 'Find my resume', 'Show me photos of the whiteboard']

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
  const [configurationStatus, setConfigurationStatus] = useState<RealtimeConfigurationStatus | undefined>()
  const [question, setQuestion] = useState('')
  const [capture, setCapture] = useState<CaptureResult>()
  const [captureSources, setCaptureSources] = useState<CaptureSource[]>([])
  const [capturePickerOpen, setCapturePickerOpen] = useState(false)
  const [selectedCaptureSourceId, setSelectedCaptureSourceId] = useState<string>()
  const [explanation, setExplanation] = useState<Explanation>()
  const [screenReasoning, setScreenReasoning] = useState<ScreenReasoningSummary>()
  const [screenReasoningConfirmationOpen, setScreenReasoningConfirmationOpen] = useState(false)
  const [isScreenReasoning, setIsScreenReasoning] = useState(false)
  const [pendingAction, setPendingAction] = useState<PendingActionPreview>()
  const [searchConfirmation, setSearchConfirmation] = useState<SearchConfirmationRequest | undefined>()
  const pendingProposalsRef = useRef(new Map<string, PendingProposal>())
  const [toolResult, setToolResult] = useState<ToolExecutionResult>()
  const [windowNotice, setWindowNotice] = useState<string>()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest>()
  const [droppedFile, setDroppedFile] = useState<DroppedFileDescriptor>()
  const [dragFileCount, setDragFileCount] = useState(0)
  const [isDroppedFileBusy, setIsDroppedFileBusy] = useState(false)
  const settingsRef = useRef<HTMLDivElement>(null)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const [online, setOnline] = useState(() => navigator.onLine)
  const conversationRef = useRef<HTMLDivElement>(null)
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
  const [pendingTelegramAttachment, setPendingTelegramAttachment] = useState<PendingTelegramAttachment>()
  const [photoSearchStatus, setPhotoSearchStatus] = useState<PhotoSearchStatus>({
    state: 'off', enabled: false, modelInstalled: false, modelDownloadBytes: 0, downloadedBytes: 0,
    indexed: 0, total: 0, failed: 0, skipped: 0, onlyWhilePluggedIn: true, powerStateKnown: false, onBattery: false,
    textSearchEnabled: false, faceCountEnabled: false, extrasInstalled: false, extrasDownloadBytes: 0,
    textIndexed: 0, faceScanned: 0
  })
  const [isPhotoSearchWorking, setIsPhotoSearchWorking] = useState(false)
  const [peopleStatus, setPeopleStatus] = useState<PeopleSearchStatus>({
    state: 'off', enabled: false, modelInstalled: false, modelDownloadBytes: 0, downloadedBytes: 0,
    paused: false, total: 0, profiles: []
  })
  const [isPeopleWorking, setIsPeopleWorking] = useState(false)
  const [isEnrolmentWorking, setIsEnrolmentWorking] = useState(false)
  const [peopleEnrolment, setPeopleEnrolment] = useState<PeopleEnrolmentView>()

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
    // Settings must never stay open behind the orb.
    if (!expanded) {
      setSettingsOpen(false)
    }
  }, [expanded])

  // A stray drop must never navigate the window to a file:// page.
  useEffect(() => preventFileNavigation(document), [])

  useEffect(() => () => {
    void window.lifeLens.discardCapture()
  }, [])

  useEffect(() => {
    const update = () => setOnline(navigator.onLine)
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    return () => {
      window.removeEventListener('online', update)
      window.removeEventListener('offline', update)
    }
  }, [])

  // Keep the newest message in view as the conversation grows.
  useEffect(() => {
    const region = conversationRef.current
    if (!region) {
      return
    }
    region.scrollTop = region.scrollHeight
  }, [transcript, searchResults, explanation, toolResult, pendingAction, searchConfirmation])

  /**
   * Asks the user a yes-or-no question on Lumi's own surface.
   *
   * Everything destructive routes through here rather than `window.confirm`,
   * which would open a separate operating-system window outside Lumi's frame
   * and block the panel until it was answered.
   */
  const requestConfirmation: RequestConfirmation = useCallback((content, onConfirm) => {
    setConfirmRequest({ content, onConfirm })
  }, [])

  const answerConfirmation = (confirmed: boolean): void => {
    setConfirmRequest(undefined)
    if (confirmed) {
      confirmRequest?.onConfirm()
    }
  }

  /*
   * The settings trap and the confirm dialog both want Tab. A ref lets the
   * settings handler stand down while a dialog is open without re-running the
   * trap effect, whose cleanup would pull focus back to the gear button.
   */
  const confirmRequestRef = useRef<ConfirmRequest | undefined>(undefined)
  useEffect(() => {
    confirmRequestRef.current = confirmRequest
  }, [confirmRequest])

  /**
   * Settings behaves as a modal layer: focus moves into it on open, is trapped
   * while it is open, and returns to the gear that opened it on close.
   */
  useEffect(() => {
    if (!settingsOpen) {
      return
    }
    const opener = settingsButtonRef.current
    focusableWithin(settingsRef.current ?? document.createElement('div'))[0]?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      // A confirm dialog sits above settings and traps Tab itself.
      if (event.key !== 'Tab' || !settingsRef.current || confirmRequestRef.current) {
        return
      }
      const target = nextTrappedFocus(
        focusableWithin(settingsRef.current),
        document.activeElement as HTMLElement | null,
        event.shiftKey
      )
      if (target) {
        event.preventDefault()
        target.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      opener?.focus()
    }
  }, [settingsOpen])

  // Opening the panel puts the caret where the user is going to type.
  useEffect(() => {
    if (expanded) {
      composerRef.current?.focus()
    }
  }, [expanded])

  /*
   * The composer is one row tall and grows with what is typed, up to the
   * max-height in the stylesheet, after which it scrolls. Measuring from `auto`
   * first is what lets it shrink again as text is deleted.
   */
  const fitComposer = useCallback(() => {
    const input = composerRef.current
    if (!input) {
      return
    }
    input.style.height = 'auto'
    // scrollHeight stops at the padding edge, but the box is sized border-box,
    // so the border has to be added back or a single line lands two pixels
    // short of its own height and scrolls.
    const border = input.offsetHeight - input.clientHeight
    input.style.height = `${input.scrollHeight + border}px`
  }, [])

  useEffect(() => {
    fitComposer()
  }, [fitComposer, question, expanded])

  /*
   * Expanding the panel renders it before the window has finished growing, so
   * the first measurement can land while the composer is only a couple of dozen
   * pixels wide — which wraps the text into a tall box and leaves it there. A
   * re-fit on width settles it. Only width is watched: reacting to the height
   * this sets would feed itself.
   */
  useEffect(() => {
    const input = composerRef.current
    if (!input || !expanded) {
      return
    }
    let lastWidth = input.getBoundingClientRect().width
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width === undefined || width === lastWidth) {
        return
      }
      lastWidth = width
      fitComposer()
    })
    observer.observe(input)
    return () => observer.disconnect()
  }, [expanded, fitComposer])

  // Escape closes the topmost layer, one at a time, before collapsing.
  useEffect(() => {
    if (!expanded) {
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') {
        return
      }
      event.preventDefault()
      // Layers close outermost first: the confirm dialog, drop overlay,
      // settings, capture picker, then the panel itself. Escape answers the
      // dialog "no", which is the safe answer and the one that changes nothing
      // — unlike a pending action's confirmation, which must be answered.
      if (confirmRequest) {
        setConfirmRequest(undefined)
      } else if (dragFileCount > 0) {
        setDragFileCount(0)
      } else if (settingsOpen) {
        setSettingsOpen(false)
      } else if (capturePickerOpen) {
        setCapturePickerOpen(false)
      } else {
        setExpanded(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [expanded, settingsOpen, capturePickerOpen, dragFileCount, confirmRequest])

  useEffect(() => {
    void refreshDocumentRoots()
    void refreshTelegramStatus()
    void window.lifeLens.getPhotoSearchStatus().then(setPhotoSearchStatus, () => undefined)
    void window.lifeLens.getPeopleSearchStatus().then(setPeopleStatus, () => undefined)
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
    const removePhotoSearchListener = window.lifeLens.onPhotoSearchStatusChanged(setPhotoSearchStatus)
    const removePeopleSearchListener = window.lifeLens.onPeopleSearchStatusChanged(setPeopleStatus)
    return () => {
      removeSearchListener()
      removeTelegramListener()
      removePhotoSearchListener()
      removePeopleSearchListener()
      void window.lifeLens.cancelFileSearch()
      // No approved-but-unsent photo may outlive the session.
      void window.lifeLens.cancelPhotoAnalysis()
      clientRef.current?.disconnect()
      void window.lifeLens.setRealtimeActive(false)
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
    setPendingTelegramAttachment((current) => current?.serverCall.generation === generation ? undefined : current)
    if (telegramServerCallRef.current?.generation === generation) {
      telegramServerCallRef.current = undefined
      setTelegramRecipients([])
      setSelectedTelegramRecipientId(undefined)
      setIsTelegramWorking(false)
      setPendingTelegramAttachment(undefined)
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
          onTelegramAttachmentRequest: requestTelegramAttachment,
          onToolProposal: (proposal, serverCall) => { void preparePendingAction(proposal, serverCall) },
          onError: setError,
          onSessionEnded: (reason, generation) => {
            void window.lifeLens.setRealtimeActive(false)
            void window.lifeLens.discardCapture()
            expireServerGeneration(generation)
            setCapture(undefined)
            setExplanation(undefined)
            setScreenReasoning(undefined)
            setScreenReasoningConfirmationOpen(false)
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
        setConfigurationStatus(credential.configurationStatus)
        const pendingConnection = client.connect(credential, {
          greet: credential.mode === 'live' ? !hasConnectedOnceRef.current : true
        })
        // If the user collapsed while the credential was being minted, carry
        // that privacy choice into the session before its initial update.
        client.setListening(expandedRef.current)
        await pendingConnection
        void window.lifeLens.setRealtimeActive(true)
        if (!expandedRef.current && collapsedAtRef.current !== undefined) {
          client.startCollapseDisconnect(collapsedAtRef.current)
        }
        if (credential.mode === 'live') {
          hasConnectedOnceRef.current = true
        }
      } catch (connectionError) {
        void window.lifeLens.setRealtimeActive(false)
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
      setScreenReasoning(undefined)
      setScreenReasoningConfirmationOpen(true)
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

  const reviewCaptureWithGpt56 = async (): Promise<void> => {
    if (!capture) {
      return
    }
    setError(undefined)
    setIsScreenReasoning(true)
    setCompanionState('thinking')
    try {
      const result = await window.lifeLens.analyzeCapture(capture.id)
      if (result.sourceCaptureId !== capture.id) {
        throw new Error('Lumi received a screen brief for a different capture.')
      }
      setScreenReasoning(result)
      setScreenReasoningConfirmationOpen(false)
      setExplanation(explanationFromScreenReasoning(result))
      clientRef.current?.provideScreenReview(result)
      setCompanionState('success')
    } catch (reasoningError) {
      setCompanionState('error')
      setError(messageFrom(reasoningError))
    } finally {
      setIsScreenReasoning(false)
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
    clientRef.current?.setSearchResults(payload.results, payload.fallback)
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

    const photoCue = /\b(?:photo|photos|picture|pictures|image|images)\b/i.test(query)
    const concept = query
      .replace(/\b(?:find|show|search|latest|recent|my|photo|photos|picture|pictures|image|images)\b/gi, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 64)
    void controller.run(photoCue && concept
      ? { queryTerms: query, kind: 'photo', concepts: [concept] }
      : { queryTerms: query }, undefined, 'user')
  }

  /**
   * Registers exactly one dropped file and does nothing else with it.
   *
   * The `File` goes to preload, which resolves it to a path and hands that to
   * main. No path comes back — only an opaque id and safe metadata.
   */
  const handleDrop = async (transfer: DataTransfer | null): Promise<void> => {
    setDragFileCount(0)
    const decision = decideDrop(transfer)
    if (decision.kind === 'none') {
      return
    }
    if (decision.kind === 'too-many') {
      setError(TOO_MANY_FILES_MESSAGE)
      return
    }

    setError(undefined)
    try {
      // A second drop replaces the first; the previous id stops resolving.
      const descriptor = await window.lifeLens.registerDroppedFile(decision.file)
      setDroppedFile(descriptor)
    } catch (dropError) {
      setDroppedFile(undefined)
      setError(messageFrom(dropError))
    }
  }

  const removeDroppedFile = async (): Promise<void> => {
    const current = droppedFile
    setDroppedFile(undefined)
    if (!current) {
      return
    }
    try {
      // Clearing main's record is what actually revokes the grant; the source
      // file on disk is untouched.
      await window.lifeLens.removeDroppedFile(current.droppedId)
    } catch {
      // The record is gone from the user's point of view either way.
    }
  }

  const proposeDroppedOpen = (): void => {
    if (!droppedFile) return
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'open_file',
      reason: `Open ${droppedFile.fileName}, the file you dropped on Lumi.`,
      requiresConfirmation: true,
      arguments: { resultId: droppedFile.droppedId }
    })
  }

  const proposeDroppedAnalyse = (): void => {
    if (!droppedFile) return
    if (droppedFile.mediaKind !== 'photo') {
      setError(DOCUMENT_ANALYSIS_NOTICE)
      return
    }
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'analyze_photo',
      reason: `Send only ${droppedFile.fileName} to OpenAI so Lumi can answer your question about it.`,
      requiresConfirmation: true,
      arguments: { resultId: droppedFile.droppedId, question: question.trim() || 'What is in this photo?' }
    })
  }

  const proposeDroppedSend = (): void => {
    if (!droppedFile) return
    proposeTelegramAttachment({ id: droppedFile.droppedId, name: droppedFile.fileName })
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

  const revokeDocumentRoot = (root: ApprovedDocumentRoot): void => {
    requestConfirmation(COPY.files.confirmRevoke(root.label), () => {
      void (async () => {
        try {
          await window.lifeLens.removeDocumentRoot(root.id)
          updateDocumentRoots(await window.lifeLens.listDocumentRoots())
          setSearchResults([])
          setThumbnails(new Map())
        } catch (rootError) {
          setError(messageFrom(rootError))
        }
      })()
    })
  }

  const runPhotoSearchAction = async (action: () => Promise<PhotoSearchStatus>): Promise<void> => {
    setIsPhotoSearchWorking(true)
    setError(undefined)
    try {
      setPhotoSearchStatus(await action())
    } catch (actionError) {
      setError(messageFrom(actionError))
    } finally {
      setIsPhotoSearchWorking(false)
    }
  }

  const runPeopleAction = async (action: () => Promise<PeopleSearchStatus>): Promise<void> => {
    setIsPeopleWorking(true)
    setError(undefined)
    try {
      setPeopleStatus(await action())
    } catch (actionError) {
      setError(messageFrom(actionError))
    } finally {
      setIsPeopleWorking(false)
    }
  }

  /**
   * Runs one enrolment step and refreshes the draft view from whatever main
   * returns. A rejection is not routed to the global error banner: main
   * already turns it into a bounded `lastRejection` on the draft (see
   * addPeopleReference/selectPeopleFace in index.ts), and showing it there
   * keeps the user inside the flow instead of bouncing them out of it.
   */
  const runEnrolmentStep = async (action: () => Promise<PeopleEnrolmentView>): Promise<void> => {
    setIsEnrolmentWorking(true)
    try {
      setPeopleEnrolment(await action())
    } catch (actionError) {
      setError(messageFrom(actionError))
    } finally {
      setIsEnrolmentWorking(false)
    }
  }

  const beginPeopleEnrolment = (label: string): void => {
    void runEnrolmentStep(() => window.lifeLens.beginPeopleEnrolment(label))
  }

  const beginPersonAddition = (profileId: string): void => {
    void runEnrolmentStep(() => window.lifeLens.beginPersonReferenceAddition(profileId))
  }

  const useSearchResultAsReference = (result: DocumentSearchResult): void => {
    if (!peopleEnrolment) {
      return
    }
    void runEnrolmentStep(() => window.lifeLens.addPeopleReference(peopleEnrolment.enrolmentId, result.id))
  }

  const selectEnrolmentFace = (candidateId: string): void => {
    if (!peopleEnrolment) {
      return
    }
    void runEnrolmentStep(() => window.lifeLens.selectPeopleFace(peopleEnrolment.enrolmentId, candidateId))
  }

  const cancelPeopleEnrolment = (): void => {
    if (peopleEnrolment) {
      void window.lifeLens.cancelPeopleEnrolment(peopleEnrolment.enrolmentId)
    }
    setPeopleEnrolment(undefined)
  }

  const confirmPeopleEnrolment = async (): Promise<void> => {
    if (!peopleEnrolment) {
      return
    }
    setIsEnrolmentWorking(true)
    setError(undefined)
    try {
      await window.lifeLens.confirmPeopleEnrolment(peopleEnrolment.enrolmentId)
      setPeopleEnrolment(undefined)
      setPeopleStatus(await window.lifeLens.getPeopleSearchStatus())
    } catch (actionError) {
      setError(messageFrom(actionError))
    } finally {
      setIsEnrolmentWorking(false)
    }
  }

  // Takes only the id and name, so an approved-folder result and a dropped file
  // travel the identical proposal path. Main decides which trust applies.
  const proposeTelegramAttachment = (result: { id: string; name: string }): void => {
    if (!selectedTelegramRecipientId) {
      setError('Choose one Telegram recipient before sending a file.')
      return
    }
    const recipient = telegramRecipients.find((candidate) => candidate.resultId === selectedTelegramRecipientId)
    if (!recipient) {
      setError('That Telegram recipient is no longer available. Search again.')
      return
    }
    if (telegramMessage.length > 1_024) {
      setError(`That caption is ${telegramMessage.length} characters. Shorten it to 1024 characters or fewer.`)
      return
    }
    void preparePendingAction({
      id: crypto.randomUUID(),
      toolName: 'send_telegram_attachment',
      reason: 'Send this one trusted search result from your connected personal Telegram account.',
      requiresConfirmation: true,
      arguments: {
        recipientResultId: recipient.resultId,
        fileResultId: result.id,
        caption: telegramMessage.length > 0 ? telegramMessage : undefined
      }
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

  const requestTelegramAttachment = (request: TelegramAttachmentCoordinationRequest, serverCall: RealtimeServerCall): void => {
    telegramServerCallRef.current = serverCall
    setTelegramQuery(request.recipientQuery)
    setTelegramMessage(request.caption ?? '')
    setPendingTelegramAttachment(undefined)
    setError(undefined)
    setIsTelegramWorking(true)
    void window.lifeLens.searchTelegramRecipients(request.recipientQuery).then((recipients) => {
      if (!clientRef.current?.isServerCallActive(serverCall)) return
      setTelegramRecipients(recipients)
      setSelectedTelegramRecipientId(recipients.length === 1 ? recipients[0]!.resultId : undefined)
      if (recipients.length === 0) {
        clientRef.current.completeTelegramAttachmentRequest(serverCall, { ok: false, message: 'No local recipient matched. Ask the user for another name.' })
        return
      }
      if (recipients.length === 1) {
        proposeTrustedTelegramAttachment(request, recipients[0]!.resultId, serverCall)
        return
      }
      setPendingTelegramAttachment({ request, serverCall })
    }).catch((telegramError) => {
      if (!clientRef.current?.isServerCallActive(serverCall)) return
      const message = messageFrom(telegramError)
      setError(message)
      clientRef.current.completeTelegramAttachmentRequest(serverCall, { ok: false, message })
    }).finally(() => {
      if (telegramServerCallRef.current === serverCall) {
        telegramServerCallRef.current = undefined
        setIsTelegramWorking(false)
      }
    })
  }

  const proposeTrustedTelegramAttachment = (
    request: TelegramAttachmentCoordinationRequest,
    recipientResultId: string,
    serverCall: RealtimeServerCall
  ): void => {
    setPendingTelegramAttachment(undefined)
    void preparePendingAction({
      id: crypto.randomUUID(),
      callId: serverCall.callId,
      toolName: 'send_telegram_attachment',
      reason: request.reason,
      requiresConfirmation: true,
      arguments: {
        recipientResultId,
        fileResultId: request.fileResultId,
        caption: request.caption
      }
    }, serverCall)
  }

  const selectTelegramRecipient = (resultId: string): void => {
    setSelectedTelegramRecipientId(resultId)
    if (pendingTelegramAttachment && clientRef.current?.isServerCallActive(pendingTelegramAttachment.serverCall)) {
      proposeTrustedTelegramAttachment(pendingTelegramAttachment.request, resultId, pendingTelegramAttachment.serverCall)
    }
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
    setError(undefined)
    const outcome = await approvePendingRendererAction(
      approvalId,
      {
        pendingByApprovalId: pendingProposalsRef.current,
        clearCard: (settledApprovalId) => setPendingAction((current) => current?.approvalId === settledApprovalId ? undefined : current)
      },
      (settledApprovalId) => window.lifeLens.approvePendingAction(settledApprovalId),
      setIsConfirming
    )
    if (outcome.status === 'missing') return

    const pending = outcome.pending
    const proposal = pending.proposal
    if (outcome.status === 'failed') {
      const message = messageFrom(outcome.error)
      setToolResult({ ok: false, message })
      if (isPendingProposalCurrent(pending)) {
        clientRef.current?.sendToolResult(proposal, { ok: false, message }, pending.serverCall)
      }
      setCompanionState('error')
      return
    }

    const result = outcome.result
    if (!isPendingProposalCurrent(pending)) return
    try {
      setToolResult(result)
      clientRef.current?.sendToolResult(proposal, result, pending.serverCall)
      if (result.searchResults) {
        applySearchResults({
          results: result.searchResults,
          compactResults: result.compactResults ?? [],
          fallback: result.searchFallback ?? false,
          message: result.message
        })
      }
      if (result.openedResultId) clientRef.current?.recordOpenedResult(result.openedResultId)

      // Main approved exactly one image for this turn; send it through the
      // existing Realtime image path with the user's own question.
      if (result.analysisImage && proposal.toolName === 'analyze_photo') {
        await ensureConnected()
        const client = clientRef.current
        if (!client) throw new Error('Connect voice before asking Lumi about a photo.')
        // A dropped file's identifier is a temporary main-side handle. The
        // confirmed image still goes to OpenAI, but the id is withheld so the
        // model can never later address it as "the selected file".
        const isDropped = result.analysisImage.resultId === droppedFile?.droppedId
        await client.analyzeSelectedPhoto(result.analysisImage, proposal.arguments.question ?? '', !isDropped)
      }

      if (result.ok) {
        setCompanionState('success')
      } else if (/cancelled|declined/i.test(result.message)) {
        setCompanionState('listening')
      } else {
        setCompanionState('error')
      }
    } catch (toolError) {
      const message = messageFrom(toolError)
      setCompanionState('error')
      setError(message)
    }
  }

  const dismissPendingAction = async (approvalId: string): Promise<void> => {
    const outcome = await dismissPendingRendererAction(
      approvalId,
      {
        pendingByApprovalId: pendingProposalsRef.current,
        clearCard: (settledApprovalId) => setPendingAction((current) => current?.approvalId === settledApprovalId ? undefined : current)
      },
      (settledApprovalId) => window.lifeLens.cancelPendingAction(settledApprovalId)
    )
    if (outcome.status === 'dismissed') {
      clientRef.current?.declineToolProposal(outcome.pending.proposal, outcome.pending.serverCall)
      setToolResult({ ok: false, message: 'Cancelled. Nothing was changed, opened, or sent.' })
      setCompanionState('listening')
      return
    }
    if (outcome.status === 'failed') setError(messageFrom(outcome.error))
  }

  const selectCaptureSource = (source: CaptureSource): void => {
    void window.lifeLens.discardCapture()
    setSelectedCaptureSourceId(source.id)
    setCapturePickerOpen(false)
    clientRef.current?.invalidateScreenContext()
    setCapture(undefined)
    setExplanation(undefined)
    setScreenReasoning(undefined)
    setScreenReasoningConfirmationOpen(false)
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
  const showsSemanticResults = searchResults.some((result) => result.reason?.includes('visual match'))
  const applicationWindows = captureSources.filter((source) => source.kind === 'window')
  const entireDisplays = captureSources.filter((source) => source.kind === 'screen')
  const visibleLinks = explanation?.signals.filter((signal) => signal.kind === 'link') ?? []
  const hasConversation = transcript.length > 0 || Boolean(explanation) || searchResults.length > 0 || Boolean(capture)
  const sendDisabledReason = !clientRef.current
    ? COPY.labels.sendDisabledConnecting
    : !question.trim()
      ? COPY.labels.sendDisabledEmpty
      : undefined
  const canSend = sendDisabledReason === undefined
  const status = deriveStatus({
    companionState,
    isConnecting,
    hasConnectedBefore: hasConnectedOnceRef.current,
    online,
    isSending: isTelegramWorking,
    isSearching,
    photoSearchStatus,
    mode
  })

  return (
    <main className={`app-shell ${expanded ? 'is-open' : 'is-closed'}`}>
      <div className="companion-shell drag-region" title="Drag the outer ring to move Lumi">
        <button className={`companion-core state-${companionState}`} type="button" aria-label="Open Lumi" onClick={openCompanion}>
          <span className="companion-eye left" />
          <span className="companion-eye right" />
          <span className="companion-glow" />
        </button>
      </div>

      {expanded && (
        <section
          className="panel"
          aria-label="Lumi"
          onDragOver={(event) => {
            event.preventDefault()
            setDragFileCount(countDraggedFiles(event.dataTransfer))
          }}
          onDragLeave={(event) => {
            // Ignore the transient leave fired when crossing a child element.
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setDragFileCount(0)
            }
          }}
          onDrop={(event) => {
            event.preventDefault()
            void handleDrop(event.dataTransfer)
          }}
        >
          {dragFileCount > 0 && <DropOverlay fileCount={dragFileCount} />}
          <header className="panel-header drag-region">
            <div className="brand-lockup">
              <DragGrip />
              <BrandMark size={20} />
              <h1>Lumi</h1>
            </div>
            <div className="header-controls">
              <StatusPill status={status} />
              <button
                ref={settingsButtonRef}
                className="icon-button"
                type="button"
                aria-label={COPY.labels.settings}
                aria-expanded={settingsOpen}
                onClick={() => setSettingsOpen((open) => !open)}
              >
                ⚙
              </button>
              <button className="icon-button" type="button" aria-label="Collapse to orb" onClick={() => setExpanded(false)}>&times;</button>
            </div>
          </header>

          {/* One polite region for status, so state changes are heard without
              interrupting, and separately from the conversation itself. */}
          <p className="visually-hidden" aria-live="polite">{statusAnnouncement(status, photoSearchStatus)}</p>
          {/* Assertive, because an actionable failure should interrupt. */}
          <p className="visually-hidden" role="alert">{error ?? ''}</p>

          <div className="conversation" role="log" aria-label="Conversation with Lumi" ref={conversationRef}>
          {!hasConversation && (
            <div className="conversation-empty">
              <BrandMark size={48} glow />
              <p>Ask about what is on your screen, or find a file.</p>
              <div className="suggestion-chips">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    className="chip"
                    type="button"
                    key={suggestion}
                    onClick={() => setQuestion(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {transcript.map((line, index) => <p className="message" key={`${line}-${index}`}>{line}</p>)}

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
          {mode === 'mock' && <p className="notice">{configurationStatus === 'openai_api_key_missing'
            ? "Demo mode is active because the OpenAI API key is not configured in Lumi's main process. It exercises the same capture and confirmation path."
            : 'Demo mode is active. It exercises the same capture and confirmation path.'}</p>}

          {capture && (
            <figure className="capture-card">
              <img src={capture.dataUrl} alt={`Preview of ${capture.label}`} />
              <figcaption>Captured {new Date(capture.capturedAt).toLocaleTimeString()} from {capture.sourceKind === 'screen' ? 'screen' : 'window'}</figcaption>
            </figure>
          )}

          {capture && mode === 'live' && screenReasoningConfirmationOpen && !screenReasoning && (
            <section className="screen-reasoning-confirmation" aria-label="GPT-5.6 screen review confirmation">
              <p className="eyebrow">GPT-5.6 REVIEW</p>
              <p>Send only this capture to OpenAI for a read-only brief of visible dates, links, risks, and next actions?</p>
              <div className="actions">
                <button className="secondary-button" type="button" onClick={() => void reviewCaptureWithGpt56()} disabled={isScreenReasoning}>
                  {isScreenReasoning ? 'Reviewing capture…' : 'Review this capture with GPT-5.6'}
                </button>
                <button className="text-button" type="button" onClick={() => setScreenReasoningConfirmationOpen(false)} disabled={isScreenReasoning}>Not now</button>
              </div>
            </section>
          )}

          {screenReasoning && (
            <section className="screen-reasoning-card" aria-label="GPT-5.6 screen review">
              <p className="eyebrow">GPT-5.6 REVIEW</p>
              <p>{screenReasoning.summary}</p>
              {screenReasoning.risks.length > 0 && <><h2>Risks to notice</h2><ul>{screenReasoning.risks.map((risk) => <li key={risk}>{risk}</li>)}</ul></>}
              {screenReasoning.nextActions.length > 0 && <><h2>Suggested next actions</h2><ul>{screenReasoning.nextActions.map((action) => <li key={action}>{action}</li>)}</ul></>}
            </section>
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

          <section className="result-block" aria-label="Search results">
            {searchResults.length > 0 && (
              <>
                <p className="workspace-note">
                  {showsImageResults
                    ? showsSemanticResults
                      ? 'These matches came from the local on-device photo index. OCR and person identity recognition are not supported. Selected-photo analysis remains a separate confirmed action.'
                      : 'These are filename, folder, and date possibilities. Choose one for the separate confirmed photo-analysis action.'
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
                    onSend={proposeTelegramAttachment}
                    onUseAsReference={peopleEnrolment ? useSearchResultAsReference : undefined}
                    referenceForLabel={peopleEnrolment?.label}
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
                        <div className="actions">
                          <button className="text-button" type="button" onClick={() => proposeOpenFile(result)}>Open file</button>
                          <button className="text-button" type="button" onClick={() => proposeTelegramAttachment(result)}>Send via Telegram</button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </section>

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
          </div>

          {droppedFile && (
            <DroppedFileCard
              file={droppedFile}
              busy={isDroppedFileBusy}
              onOpen={proposeDroppedOpen}
              onAnalyse={proposeDroppedAnalyse}
              onSend={proposeDroppedSend}
              onRemove={() => {
                setIsDroppedFileBusy(true)
                void removeDroppedFile().finally(() => setIsDroppedFileBusy(false))
              }}
            />
          )}

          <form className="composer" onSubmit={(event) => { event.preventDefault(); void askQuestion() }}>
            <textarea
              ref={composerRef}
              className="composer-input"
              rows={1}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              // Enter sends; Shift+Enter starts a new line.
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  if (canSend) {
                    void askQuestion()
                  }
                }
              }}
              placeholder={COPY.labels.ask + '…'}
              aria-label={COPY.labels.ask}
            />
            <button
              className="icon-button"
              type="button"
              aria-label={capture ? COPY.labels.captureAgain : COPY.labels.captureScreen}
              title={capture ? COPY.labels.captureAgain : COPY.labels.captureScreen}
              onClick={() => requestScreenContext()}
              disabled={!clientRef.current || isCapturing}
              aria-busy={isCapturing || undefined}
            >
              ◉
            </button>
            <button
              className="primary-button send-button"
              type="submit"
              disabled={!canSend}
              // A disabled control says why, rather than leaving the user guessing.
              aria-describedby={sendDisabledReason ? 'composer-send-reason' : undefined}
              title={sendDisabledReason ?? COPY.labels.send}
            >
              {COPY.labels.send}
            </button>
            {sendDisabledReason && (
              <span id="composer-send-reason" className="visually-hidden">{sendDisabledReason}</span>
            )}
          </form>

          {settingsOpen && (
            <div ref={settingsRef} className="settings-overlay" role="dialog" aria-modal="true" aria-label="Settings">
              <header className="settings-header">
                <h2>Settings</h2>
                <button className="icon-button" type="button" aria-label="Close settings" onClick={() => setSettingsOpen(false)}>&times;</button>
              </header>
              <div className="settings-scroll">
                <section className="settings-group" aria-label="Voice">
                  <h3>Voice</h3>
                  {mode === 'mock'
                    ? <p className="workspace-note">Demo mode — voice and answers are simulated, so Lumi can be tried without an OpenAI key. Capture, search, confirmation, and Telegram all behave exactly as they will live.</p>
                    : <p className="workspace-note">Voice is live. Lumi listens only while the panel is open.</p>}
                  <div className="actions">
                    <button className="secondary-button" type="button" onClick={() => { void connectVoice().catch(() => undefined) }} disabled={isConnecting}>
                      {isConnecting ? (hasConnectedOnceRef.current ? 'Reconnecting…' : 'Connecting…') : 'Connect voice'}
                    </button>
                  </div>
                </section>

                <section className="settings-group" aria-label="Files and approved folders">
                  <h3>Files and approved folders</h3>
                  {documentRoots.length === 0
                    ? <p className="workspace-note">Lumi can only search folders you approve. Ask for a file and Lumi will offer the folder chooser once.</p>
                    : <ul className="approved-root-list">{documentRoots.map((root) => (
                      <li key={root.id}><span>{root.label}</span><button className="text-button" type="button" onClick={() => revokeDocumentRoot(root)}>Revoke</button></li>
                    ))}</ul>}
                  <div className="actions">
                    <button className="secondary-button" type="button" disabled={isChoosingFolder} onClick={() => void chooseDocumentRoot()}>
                      {isChoosingFolder ? 'Choosing…' : 'Approve a folder'}
                    </button>
                  </div>
                  <div className="document-search-row">
                    <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="resume" aria-label="What to look for" />
                    <button className="secondary-button" type="button" disabled={isSearching} onClick={proposeDocumentSearch}>
                      {isSearching ? 'Searching…' : 'Search files'}
                    </button>
                  </div>
                </section>

            <section className="photo-search-settings" aria-label="Intelligent photo search settings">
              <div className="section-heading-row">
                <div><p className="eyebrow">LOCAL PHOTO SEARCH</p><h2>Intelligent photo search</h2></div>
                <span className={`photo-search-state state-${photoSearchStatus.state}`}>{photoSearchStateLabel(photoSearchStatus.state)}</span>
              </div>
              <p className="workspace-note"><strong>Photos are indexed on this device and are not uploaded.</strong> Visual search works only across indexed JPEG, PNG, and WebP photos.</p>
              {!photoSearchStatus.enabled && (
                <button className="secondary-button" type="button" disabled={isPhotoSearchWorking} onClick={() => void runPhotoSearchAction(() => window.lifeLens.enablePhotoSearch())}>Enable intelligent photo search</button>
              )}
              {photoSearchStatus.enabled && !photoSearchStatus.modelInstalled && photoSearchStatus.state !== 'downloading' && photoSearchStatus.state !== 'verifying' && (
                <button className="secondary-button" type="button" disabled={isPhotoSearchWorking} onClick={() => void runPhotoSearchAction(() => window.lifeLens.downloadPhotoSearchModel())}>
                  Download local model ({formatMegabytes(photoSearchStatus.modelDownloadBytes)})
                </button>
              )}
              {(photoSearchStatus.state === 'downloading' || photoSearchStatus.state === 'verifying') && (
                <div className="photo-search-progress">
                  <progress
                    max={Math.max(1, photoSearchStatus.modelDownloadBytes)}
                    value={photoSearchStatus.downloadedBytes}
                    aria-label="Download progress"
                    aria-valuenow={photoSearchStatus.downloadedBytes}
                    aria-valuemin={0}
                    aria-valuemax={Math.max(1, photoSearchStatus.modelDownloadBytes)}
                    aria-valuetext={`${formatMegabytes(photoSearchStatus.downloadedBytes)} of ${formatMegabytes(photoSearchStatus.modelDownloadBytes)}`}
                  />
                  <span>{formatMegabytes(photoSearchStatus.downloadedBytes)} of {formatMegabytes(photoSearchStatus.modelDownloadBytes)}</span>
                  <button className="text-button" type="button" onClick={() => void runPhotoSearchAction(() => window.lifeLens.cancelPhotoSearchDownload())}>Cancel download</button>
                </div>
              )}
              {photoSearchStatus.enabled && photoSearchStatus.modelInstalled && (
                <>
                  <p className="workspace-note">{photoSearchStatus.indexed} of {photoSearchStatus.total} photos indexed · {photoSearchStatus.failed} failed · {photoSearchStatus.skipped} skipped.</p>
                  {photoSearchStatus.lastIndexedAt && <p className="workspace-note">Last indexed {new Date(photoSearchStatus.lastIndexedAt).toLocaleString()}.</p>}
                  <label className="photo-search-toggle">
                    <input type="checkbox" checked={photoSearchStatus.onlyWhilePluggedIn} onChange={(event) => void runPhotoSearchAction(() => window.lifeLens.setPhotoIndexOnlyWhilePluggedIn(event.target.checked))} />
                    <span>Index only while plugged in</span>
                  </label>
                  {!photoSearchStatus.powerStateKnown && <p className="workspace-note">Power state is unavailable, so indexing continues normally.</p>}
                  <div className="actions">
                    {photoSearchStatus.state === 'paused'
                      ? <button className="secondary-button" type="button" onClick={() => void runPhotoSearchAction(() => window.lifeLens.resumePhotoIndex())}>Resume</button>
                      : <button className="secondary-button" type="button" onClick={() => void runPhotoSearchAction(() => window.lifeLens.pausePhotoIndex())}>Pause</button>}
                  </div>

                  {/* Phase 2. Each capability is opt-in and reports its own
                      progress, because they complete at very different rates. */}
                  <div className="photo-search-phase2">
                    <p className="workspace-note">{COPY.photos.phase2LocalOnly}</p>
                    {!photoSearchStatus.extrasInstalled && (
                      <p className="workspace-note">{COPY.photos.extrasDownload}</p>
                    )}

                    <label className="photo-search-toggle">
                      <input
                        type="checkbox"
                        checked={photoSearchStatus.textSearchEnabled}
                        disabled={isPhotoSearchWorking}
                        onChange={(event) => void runPhotoSearchAction(() => window.lifeLens.setPhotoTextSearchEnabled(event.target.checked))}
                      />
                      <span>{COPY.photos.enableTextSearch}</span>
                    </label>
                    <p className="workspace-note">{COPY.photos.enableTextSearchNote}</p>
                    {photoSearchStatus.textSearchEnabled && (
                      <p className="workspace-note">
                        {photoSearchStatus.textIndexed >= photoSearchStatus.total
                          ? COPY.photos.textReady
                          : COPY.photos.textProgress(photoSearchStatus.textIndexed, photoSearchStatus.total)}
                      </p>
                    )}

                    <label className="photo-search-toggle">
                      <input
                        type="checkbox"
                        checked={photoSearchStatus.faceCountEnabled}
                        disabled={isPhotoSearchWorking}
                        onChange={(event) => void runPhotoSearchAction(() => window.lifeLens.setPhotoFaceCountEnabled(event.target.checked))}
                      />
                      <span>{COPY.photos.enableFaceCount}</span>
                    </label>
                    <p className="workspace-note">{COPY.photos.enableFaceCountNote}</p>
                    {photoSearchStatus.faceCountEnabled && (
                      <p className="workspace-note">
                        {photoSearchStatus.faceScanned >= photoSearchStatus.total
                          ? COPY.photos.faceReady
                          : COPY.photos.faceProgress(photoSearchStatus.faceScanned, photoSearchStatus.total)}
                      </p>
                    )}

                    {(photoSearchStatus.textSearchEnabled || photoSearchStatus.faceCountEnabled) && (
                      <div className="actions">
                        {photoSearchStatus.textSearchEnabled && (
                          <button className="text-button" type="button" disabled={isPhotoSearchWorking} onClick={() => {
                            requestConfirmation(COPY.photos.confirmRebuildText, () => void runPhotoSearchAction(() => window.lifeLens.rebuildPhotoTextIndex()))
                          }}>{COPY.photos.rebuildTextIndex}</button>
                        )}
                        {photoSearchStatus.faceCountEnabled && (
                          <button className="text-button" type="button" disabled={isPhotoSearchWorking} onClick={() => {
                            requestConfirmation(COPY.photos.confirmRebuildFaces, () => void runPhotoSearchAction(() => window.lifeLens.rebuildPhotoFaceIndex()))
                          }}>{COPY.photos.rebuildFaceIndex}</button>
                        )}
                      </div>
                    )}
                  </div>
                </>
              )}
              {photoSearchStatus.message && <p className="workspace-note">{photoSearchStatus.message}</p>}
              {photoSearchStatus.enabled && (
                <div className="actions">
                  <button className="text-button" type="button" disabled={isPhotoSearchWorking} onClick={() => {
                    requestConfirmation(COPY.photos.confirmRebuild, () => void runPhotoSearchAction(() => window.lifeLens.rebuildPhotoIndex()))
                  }}>{COPY.photos.confirmRebuild.confirmLabel}</button>
                  <button className="text-button danger-button" type="button" disabled={isPhotoSearchWorking} onClick={() => {
                    requestConfirmation(COPY.photos.confirmDisable, () => void runPhotoSearchAction(() => window.lifeLens.disablePhotoSearch()))
                  }}>{COPY.photos.confirmDisable.confirmLabel}</button>
                </div>
              )}
            </section>

            <PeopleSettings
              status={peopleStatus}
              enrolment={peopleEnrolment}
              enrolmentBusy={isEnrolmentWorking}
              busy={isPeopleWorking}
              requestConfirmation={requestConfirmation}
              onEnable={() => void runPeopleAction(() => window.lifeLens.setPeopleSearchEnabled(true))}
              onPause={() => void runPeopleAction(() => window.lifeLens.pausePeopleScan())}
              onResume={() => void runPeopleAction(() => window.lifeLens.resumePeopleScan())}
              onDeleteAll={() => {
                setPeopleEnrolment(undefined)
                void runPeopleAction(() => window.lifeLens.deleteAllPeopleData())
              }}
              onBeginEnrolment={beginPeopleEnrolment}
              onBeginAddition={beginPersonAddition}
              onSelectFace={selectEnrolmentFace}
              onConfirmEnrolment={() => void confirmPeopleEnrolment()}
              onCancelEnrolment={cancelPeopleEnrolment}
              onRenameProfile={(profileId, label) => void runPeopleAction(async () => {
                await window.lifeLens.renamePeopleProfile(profileId, label)
                return window.lifeLens.getPeopleSearchStatus()
              })}
              onRescanProfile={(profileId) => void runPeopleAction(() => window.lifeLens.rescanPeopleProfile(profileId))}
              onDeleteProfile={(profileId) => void runPeopleAction(() => window.lifeLens.deletePeopleProfile(profileId))}
            />

            <section className="telegram-workspace" aria-label="Telegram integration">
              <div className="section-heading-row">
                <div>
                  <p className="eyebrow">INTEGRATIONS</p>
                  <h2>Telegram</h2>
                  <div className="integration-subtitle">
                    <span>Personal account connection</span>
                    <details className="integration-disclosure">
                      <summary aria-label="About Lumi's Telegram connection" title="About Lumi's Telegram connection">i</summary>
                      <p>Lumi connects through a third-party Telegram client and is not affiliated with or endorsed by Telegram.</p>
                    </details>
                  </div>
                </div>
                {telegramStatus.state === 'connected' ? (
                  <button className="text-button" type="button" disabled={isTelegramWorking} onClick={() => void logoutTelegram()}>Log out</button>
                ) : (
                  <button className="text-button" type="button" disabled={isTelegramWorking || telegramStatus.state === 'connecting'} onClick={() => void connectTelegram()}>
                    {isTelegramWorking || telegramStatus.state === 'connecting' ? 'Connecting...' : 'Connect Telegram'}
                  </button>
                )}
              </div>
              {telegramStatus.state === 'disconnected' && <p className="workspace-note">Connect a personal account to search recipient metadata locally and send one confirmed message, photo, or document.</p>}
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
                            <input type="radio" name="telegram-recipient" checked={selectedTelegramRecipientId === recipient.resultId} onChange={() => selectTelegramRecipient(recipient.resultId)} />
                            <span><strong>{recipient.displayName}</strong>{recipient.username && <small>@{recipient.username}</small>}<small>{recipient.kind}</small></span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  )}
                  {pendingTelegramAttachment && <p className="workspace-note">Choose one local recipient to continue to the single attachment confirmation.</p>}
                  {telegramQuery && telegramRecipients.length === 0 && <p className="workspace-note">Search returns up to ten local dialog/contact matches. Nothing is sent to OpenAI.</p>}
                  <label className="telegram-message-field">
                    <span>Message or attachment caption</span>
                    <textarea value={telegramMessage} maxLength={4096} onChange={(event) => setTelegramMessage(event.target.value)} placeholder="Write the complete message or caption" />
                  </label>
                  <button className="secondary-button" type="button" onClick={proposeTelegramMessage}>Send message</button>
                </>
              )}
            </section>

                <section className="settings-group" aria-label="Appearance">
                  <h3>Appearance</h3>
                  <p className="workspace-note">Drag Lumi by its header to move it. Lumi remembers where you put it.</p>
                  <div className="actions">
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => {
                        void window.lifeLens.resetWindowPosition().then(
                          () => setWindowNotice('Lumi is back at the bottom-right of your main screen.'),
                          () => setWindowNotice('Lumi could not move the window just now. Try again.')
                        )
                      }}
                    >
                      Reset window position
                    </button>
                  </div>
                  {windowNotice && <p className="notice">{windowNotice}</p>}
                </section>

                <section className="settings-group" aria-label="Privacy">
                  <h3>Privacy</h3>
                  <p className="workspace-note">
                    Photos are indexed on this device and are not uploaded. Lumi searches only the folders you approve, looks at your screen
                    only when you ask, and never acts without your confirmation.
                  </p>
                </section>
              </div>
            </div>
          )}

          {/* Last inside the panel, so it covers settings as well as the conversation. */}
          {confirmRequest && (
            <ConfirmDialog
              content={confirmRequest.content}
              onConfirm={() => answerConfirmation(true)}
              onCancel={() => answerConfirmation(false)}
            />
          )}
        </section>
      )}
    </main>
  )
}

function photoSearchStateLabel(state: PhotoSearchStatus['state']): string {
  return ({
    off: 'Off', consent_required: 'Consent required', downloading: 'Downloading', verifying: 'Verifying',
    indexing: 'Indexing', paused: 'Paused', ready: 'Ready', error: 'Error', rebuild_required: 'Rebuild required'
  })[state]
}

function formatMegabytes(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024))} MB`
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

function explanationFromScreenReasoning(reasoning: ScreenReasoningSummary): Explanation {
  return {
    summary: reasoning.summary,
    sourceCaptureId: reasoning.sourceCaptureId,
    signals: [
      ...reasoning.dates.map((value) => ({ kind: 'date' as const, label: 'Important date', value })),
      ...reasoning.links.map((value) => ({ kind: 'link' as const, label: 'Visible link', value })),
      ...reasoning.nextActions.map((value) => ({ kind: 'next_action' as const, label: 'Suggested next action', value }))
    ]
  }
}

/**
 * The approval surface for a search Lumi could not yet trust (a late voice
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
      <p>Search your approved folders for <strong>{request.input.queryTerms}</strong>? If no folder is approved yet, you will choose one next. Lumi searches only if you confirm.</p>
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
        setRenderError('Lumi could not show this sign-in code. Wait for a fresh one and try again.')
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
