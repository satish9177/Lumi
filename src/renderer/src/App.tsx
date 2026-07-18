import { useEffect, useRef, useState } from 'react'
import type { CaptureResult, CompanionState, Explanation, RealtimeMode, ToolExecutionResult, ToolProposal } from '../../shared/contracts'
import { RealtimeClient } from './realtime'

const STATUS_LABELS: Record<CompanionState, string> = {
  idle: 'Ready',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  success: 'Done',
  error: 'Needs attention'
}

export default function App() {
  const clientRef = useRef<RealtimeClient | undefined>(undefined)
  const [expanded, setExpanded] = useState(false)
  const [companionState, setCompanionState] = useState<CompanionState>('idle')
  const [mode, setMode] = useState<RealtimeMode | undefined>()
  const [question, setQuestion] = useState('What is this email about?')
  const [capture, setCapture] = useState<CaptureResult>()
  const [explanation, setExplanation] = useState<Explanation>()
  const [proposal, setProposal] = useState<ToolProposal>()
  const [toolResult, setToolResult] = useState<ToolExecutionResult>()
  const [transcript, setTranscript] = useState<string[]>([])
  const [error, setError] = useState<string>()
  const [isConnecting, setIsConnecting] = useState(false)
  const [isCapturing, setIsCapturing] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)

  useEffect(() => {
    window.lifeLens.setPanelOpen(expanded)
  }, [expanded])

  useEffect(() => {
    return () => clientRef.current?.disconnect()
  }, [])

  const appendTranscript = (text: string): void => {
    setTranscript((current) => [...current, text].slice(-6))
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
        onCaptureContextRequest: () => void captureScreen(),
        onToolProposal: setProposal,
        onError: setError
      })
      clientRef.current = client
      setMode(credential.mode)
      await client.connect(credential)
    } catch (connectionError) {
      clientRef.current = undefined
      setCompanionState('error')
      setError(messageFrom(connectionError))
    } finally {
      setIsConnecting(false)
    }
  }

  const captureScreen = async (): Promise<void> => {
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
      const nextCapture = await window.lifeLens.captureScreen()
      setCapture(nextCapture)
      setExplanation(undefined)
      setProposal(undefined)
      setToolResult(undefined)
      await client.sendCapture(nextCapture, question)
    } catch (captureError) {
      setCompanionState('error')
      setError(messageFrom(captureError))
    } finally {
      setIsCapturing(false)
    }
  }

  const confirmProposal = async (): Promise<void> => {
    if (!proposal) {
      return
    }

    setError(undefined)
    setIsConfirming(true)
    try {
      const result = await window.lifeLens.executeConfirmedTool(proposal)
      setToolResult(result)
      if (result.ok) {
        setCompanionState('success')
        clientRef.current?.sendToolResult(proposal, result)
        setProposal(undefined)
      } else {
        setCompanionState('error')
      }
    } catch (toolError) {
      setCompanionState('error')
      setError(messageFrom(toolError))
    } finally {
      setIsConfirming(false)
    }
  }

  return (
    <main className={`app-shell ${expanded ? 'is-open' : 'is-closed'}`}>
      <div className="companion-shell" title="Drag the outer ring to move LifeLens">
        <button
          className={`companion-core state-${companionState}`}
          type="button"
          aria-label="Open LifeLens"
          onClick={() => setExpanded(true)}
        >
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
            <button className="icon-button" type="button" aria-label="Close LifeLens panel" onClick={() => setExpanded(false)}>
              ×
            </button>
          </header>

          <div className="status-row" aria-live="polite">
            <span className={`status-dot state-${companionState}`} />
            <span>{STATUS_LABELS[companionState]}</span>
            {mode && <span className="mode-badge">{mode === 'live' ? 'Realtime voice' : 'Mock voice'}</span>}
          </div>

          <label className="question-field">
            <span>Ask about what is on screen</span>
            <input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What is this email about?" />
          </label>

          <div className="actions">
            <button className="primary-button" type="button" onClick={connectVoice} disabled={isConnecting}>
              {isConnecting ? 'Connecting…' : mode ? 'Reconnect voice' : 'Connect voice'}
            </button>
            <button className="secondary-button" type="button" onClick={captureScreen} disabled={!clientRef.current || isCapturing}>
              {isCapturing ? 'Capturing…' : 'Capture screen'}
            </button>
          </div>

          {error && <p className="notice error-notice">{error}</p>}
          {mode === 'mock' && <p className="notice">Demo mode is active because no API key is configured. It exercises the same capture and confirmation path.</p>}

          {capture && (
            <figure className="capture-card">
              <img src={capture.dataUrl} alt={`Preview of ${capture.label}`} />
              <figcaption>Captured {new Date(capture.capturedAt).toLocaleTimeString()}</figcaption>
            </figure>
          )}

          {explanation && (
            <article className="explanation-card">
              <p className="eyebrow">SCREEN EXPLANATION</p>
              <p>{explanation.summary}</p>
              {explanation.signals.length > 0 && (
                <ul className="signal-list">
                  {explanation.signals.map((signal, index) => (
                    <li key={`${signal.kind}-${signal.value}-${index}`}>
                      <strong>{signal.label}:</strong> {signal.value}
                    </li>
                  ))}
                </ul>
              )}
            </article>
          )}

          {proposal?.toolName === 'create_reminder' && (
            <article className="proposal-card">
              <p className="eyebrow">CONFIRM REMINDER</p>
              <h2>{proposal.arguments.title}</h2>
              <p>{proposal.reason}</p>
              <p className="due-date">{new Date(proposal.arguments.dueAt).toLocaleString()}</p>
              <p className="context-note">The reminder will retain this screen explanation as its source context.</p>
              <div className="actions">
                <button className="primary-button" type="button" disabled={isConfirming} onClick={confirmProposal}>
                  {isConfirming ? 'Saving…' : 'Create reminder'}
                </button>
                <button className="text-button" type="button" onClick={() => setProposal(undefined)} disabled={isConfirming}>
                  Not now
                </button>
              </div>
            </article>
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

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : 'LifeLens encountered an unexpected error.'
}
