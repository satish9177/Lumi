import { useEffect, useRef, useState } from 'react'
import type {
  ApprovedDocumentRoot,
  CaptureResult,
  CaptureSource,
  CompanionState,
  DocumentSearchResult,
  Explanation,
  RealtimeMode,
  SourceContext,
  ToolExecutionResult,
  ToolProposal
} from '../../shared/contracts'
import { ExplanationCard, ToolConfirmationCard } from './components'
import { RealtimeClient } from './realtime'

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
  const [expanded, setExpanded] = useState(false)
  const [companionState, setCompanionState] = useState<CompanionState>('idle')
  const [mode, setMode] = useState<RealtimeMode | undefined>()
  const [question, setQuestion] = useState('')
  const [capture, setCapture] = useState<CaptureResult>()
  const [captureSources, setCaptureSources] = useState<CaptureSource[]>([])
  const [capturePickerOpen, setCapturePickerOpen] = useState(false)
  const [selectedCaptureSourceId, setSelectedCaptureSourceId] = useState<string>()
  const [explanation, setExplanation] = useState<Explanation>()
  const [proposal, setProposal] = useState<ToolProposal>()
  const [toolResult, setToolResult] = useState<ToolExecutionResult>()
  const [transcript, setTranscript] = useState<string[]>([])
  const [documentRoots, setDocumentRoots] = useState<ApprovedDocumentRoot[]>([])
  const [selectedRootId, setSelectedRootId] = useState<string>()
  const [searchQuery, setSearchQuery] = useState('resume')
  const [searchResults, setSearchResults] = useState<DocumentSearchResult[]>([])
  const [error, setError] = useState<string>()
  const [isConnecting, setIsConnecting] = useState(false)
  const [isCapturing, setIsCapturing] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)
  const [isChoosingFolder, setIsChoosingFolder] = useState(false)
  const [pendingScreenCaptureCallId, setPendingScreenCaptureCallId] = useState<string | null | undefined>()

  useEffect(() => {
    window.lifeLens.setPanelOpen(expanded)
  }, [expanded])

  useEffect(() => {
    void refreshDocumentRoots()
    return () => clientRef.current?.disconnect()
  }, [])

  const appendTranscript = (text: string): void => {
    setTranscript((current) => [...current, text].slice(-8))
  }

  const updateDocumentRoots = (roots: ApprovedDocumentRoot[]): void => {
    setDocumentRoots(roots)
    setSelectedRootId((current) => current && roots.some((root) => root.id === current) ? current : roots[0]?.id)
    clientRef.current?.setApprovedRoots(roots)
  }

  async function refreshDocumentRoots(): Promise<void> {
    try {
      updateDocumentRoots(await window.lifeLens.listDocumentRoots())
    } catch (rootError) {
      setError(messageFrom(rootError))
    }
  }

  const connectVoice = async (): Promise<void> => {
    setError(undefined)
    setIsConnecting(true)
    setCompanionState('thinking')
    clientRef.current?.disconnect()
    try {
      const credential = await window.lifeLens.createRealtimeSession()
      const client = new RealtimeClient({
        onState: setCompanionState,
        onTranscript: appendTranscript,
        onExplanation: setExplanation,
        onCaptureContextRequest: requestScreenContext,
        onToolProposal: setProposal,
        onError: setError
      })
      client.setApprovedRoots(documentRoots)
      clientRef.current = client
      setMode(credential.mode)
      await client.connect(credential)
    } catch (connectionError) {
      clientRef.current?.disconnect()
      clientRef.current = undefined
      setCompanionState('error')
      setError(messageFrom(connectionError))
    } finally {
      setIsConnecting(false)
    }
  }

  const openCompanion = (): void => {
    setExpanded(true)
    if (!clientRef.current?.isConnected()) {
      void connectVoice()
    }
  }

  const loadCaptureSources = async (callId?: string): Promise<void> => {
    setError(undefined)
    if (callId !== undefined) {
      setPendingScreenCaptureCallId(callId)
    }
    try {
      const sources = await window.lifeLens.listCaptureSources()
      setCaptureSources(sources)
      setCapturePickerOpen(true)
    } catch (sourceError) {
      setError(messageFrom(sourceError))
    }
  }

  const captureScreen = async (callId?: string, sourceId = selectedCaptureSourceId): Promise<void> => {
    const client = clientRef.current
    if (!client) {
      setCompanionState('error')
      setError('Connect voice first, then capture the visible screen.')
      return
    }

    setError(undefined)
    setIsCapturing(true)
    setCompanionState('thinking')
    try {
      const nextCapture = await window.lifeLens.captureScreen(sourceId)
      setCapture(nextCapture)
      setExplanation(undefined)
      setProposal(undefined)
      setToolResult(undefined)
      setSearchResults([])
      await client.provideScreenContext(nextCapture, callId)
    } catch (captureError) {
      if (sourceId && isSelectedSourceUnavailable(captureError)) {
        setSelectedCaptureSourceId(undefined)
        client.invalidateScreenContext()
        setCapture(undefined)
        setExplanation(undefined)
        setPendingScreenCaptureCallId(callId ?? null)
        void loadCaptureSources()
      } else {
        setCompanionState('error')
        setError(messageFrom(captureError))
      }
    } finally {
      setIsCapturing(false)
    }
  }

  const requestScreenContext = (callId?: string): void => {
    const client = clientRef.current
    if (!client) {
      setError('Connect voice first, then ask Lumi about the visible screen.')
      return
    }

    if (!selectedCaptureSourceId) {
      setPendingScreenCaptureCallId(callId ?? null)
      void loadCaptureSources()
      return
    }

    void captureScreen(callId)
  }

  const askQuestion = async (): Promise<void> => {
    const client = clientRef.current
    if (!client) {
      setError('Connect voice first, then ask Lumi a question.')
      return
    }

    try {
      setError(undefined)
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
        const roots = await window.lifeLens.listDocumentRoots()
        updateDocumentRoots(roots)
        setSelectedRootId(root.id)
      }
    } catch (folderError) {
      setError(messageFrom(folderError))
    } finally {
      setIsChoosingFolder(false)
    }
  }

  const proposeDocumentSearch = (): void => {
    const query = searchQuery.trim()
    if (!selectedRootId || !query) {
      setError('Choose an approved folder and enter a filename query first.')
      return
    }

    setToolResult(undefined)
    setProposal({
      id: crypto.randomUUID(),
      toolName: 'search_documents',
      reason: `Search only the selected approved folder for files matching "${query}".`,
      requiresConfirmation: true,
      arguments: { rootId: selectedRootId, query }
    })
  }

  const proposeOpenFile = (result: DocumentSearchResult): void => {
    setToolResult(undefined)
    setProposal({
      id: crypto.randomUUID(),
      toolName: 'open_file',
      reason: `Open ${result.name}, which was returned by your approved-folder search.`,
      requiresConfirmation: true,
      arguments: { resultId: result.id }
    })
  }

  const proposeOpenUrl = (url: string): void => {
    setToolResult(undefined)
    setProposal({
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

    setToolResult(undefined)
    setProposal({
      id: crypto.randomUUID(),
      toolName: 'save_context',
      reason: 'Keep the interview summary and extracted signals for a later reminder or follow-up.',
      requiresConfirmation: true,
      arguments: { label: 'Interview screen context', sourceContext }
    })
  }

  const confirmProposal = async (proposalToConfirm: ToolProposal): Promise<void> => {
    setError(undefined)
    setIsConfirming(true)
    try {
      const result = await window.lifeLens.executeConfirmedTool(proposalToConfirm)
      setToolResult(result)
      clientRef.current?.sendToolResult(proposalToConfirm, result)
      setProposal((current) => current?.id === proposalToConfirm.id ? undefined : current)
      if (result.searchResults) {
        setSearchResults(result.searchResults)
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
      clientRef.current?.sendToolResult(proposalToConfirm, { ok: false, message })
      setCompanionState('error')
      setError(message)
    } finally {
      setIsConfirming(false)
    }
  }

  const dismissProposal = (): void => {
    if (!proposal) {
      return
    }

    clientRef.current?.declineToolProposal(proposal)
    setToolResult({ ok: false, message: 'Action declined. Nothing was changed or opened.' })
    setProposal(undefined)
    setCompanionState('listening')
  }

  const selectCaptureSource = (source: CaptureSource): void => {
    setSelectedCaptureSourceId(source.id)
    setCapturePickerOpen(false)
    clientRef.current?.invalidateScreenContext()
    setCapture(undefined)
    setExplanation(undefined)
    const callId = pendingScreenCaptureCallId ?? undefined
    setPendingScreenCaptureCallId(undefined)
    if (pendingScreenCaptureCallId !== undefined) {
      void captureScreen(callId, source.id)
    }
  }

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
            <span>{STATUS_LABELS[companionState]}</span>
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
                  clientRef.current?.declineScreenContext(pendingScreenCaptureCallId ?? undefined)
                  setPendingScreenCaptureCallId(undefined)
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
            {documentRoots.length === 0 ? <p className="workspace-note">LifeLens cannot search until you explicitly approve one folder.</p> : (
              <>
                <label className="root-select-field">
                  <span>Approved folder</span>
                  <select value={selectedRootId ?? ''} onChange={(event) => setSelectedRootId(event.target.value)}>
                    {documentRoots.map((root) => <option key={root.id} value={root.id}>{root.label}</option>)}
                  </select>
                </label>
                <div className="document-search-row">
                  <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="resume" aria-label="Filename query" />
                  <button className="secondary-button" type="button" onClick={proposeDocumentSearch}>Search (confirm)</button>
                </div>
              </>
            )}
            {searchResults.length > 0 && (
              <ul className="search-results">
                {searchResults.map((result) => (
                  <li key={result.id}>
                    <div><strong>{result.name}</strong><span>{result.relativePath}</span></div>
                    <button className="text-button" type="button" onClick={() => proposeOpenFile(result)}>Open (confirm)</button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <details className="troubleshooting">
            <summary>More / Troubleshooting</summary>
            <div className="actions">
              <button className="secondary-button" type="button" onClick={connectVoice} disabled={isConnecting}>
                {isConnecting ? 'Connecting...' : 'Connect voice'}
              </button>
              <button className="secondary-button" type="button" onClick={() => requestScreenContext()} disabled={!clientRef.current || isCapturing}>
                {isCapturing ? 'Looking...' : capture ? 'Refresh screen' : 'Capture screen'}
              </button>
              <button className="text-button" type="button" onClick={() => void loadCaptureSources()}>Change screen</button>
            </div>
          </details>

          {proposal && (
            <ToolConfirmationCard
              proposal={proposal}
              approvedRoots={documentRoots}
              searchResults={searchResults}
              isConfirming={isConfirming}
              onConfirm={confirmProposal}
              onDismiss={dismissProposal}
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
