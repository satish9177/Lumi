import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentApi } from '../../../shared/agent-contracts'
import type { AgentFileRootView } from '../../../shared/document-contracts'
import type { AgentTransferCardView, AgentTransferView } from '../../../shared/transfer-contracts'
import './components.css'

export interface TransferPanelProps {
  agent: AgentApi
  /** Folders the person approved; only those allowed to receive new files are offered. */
  roots: AgentFileRootView[]
}

const PHASE_TEXT: Record<AgentTransferView['phase'], string> = {
  awaiting_approval: 'Waiting for your approval. Nothing has been downloaded.',
  approved: 'Approved. Nothing has been downloaded yet.',
  declined: 'Cancelled. Nothing was downloaded or saved.',
  downloading: 'Downloading into Lumi’s holding area…',
  quarantined: 'Downloaded and checked. It is in Lumi’s holding area, not yet in your folder.',
  placing: 'Saving into your folder…',
  placed: 'Saved into your folder. Lumi did not open it.',
  failed: 'Stopped. Nothing was saved into your folder.',
  download_unknown: 'Lumi cannot tell whether the download finished. It will not download again; check what happened instead.',
  placement_unknown: 'Lumi cannot tell whether the file was saved. It will not try again; check what happened instead.'
}

/**
 * Milestone 10 S2: one controlled download into one approved folder.
 *
 * The card shows exactly what will happen: the address, the folder and the name, the file type, the size
 * limit, and that nothing is ever replaced. "Allow" is confirmed again by a dialog Lumi itself shows.
 * Download and Save are separate clicks. When a step's result is unknown the only button is "Check what
 * happened", which never downloads again. The panel never sees a path.
 */
export function TransferPanel({ agent, roots }: TransferPanelProps) {
  const writable = roots.filter((root) => root.canCreate)
  const [url, setUrl] = useState('')
  const [rootId, setRootId] = useState('')
  const [name, setName] = useState('')
  const [intent, setIntent] = useState('')
  const [transfer, setTransfer] = useState<AgentTransferView>()
  const [message, setMessage] = useState<string>()
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const run = useCallback(async <T,>(work: () => Promise<{ ok: true; value: T } | { ok: false; error: { message: string } }>, done: (value: T) => void) => {
    setBusy(true)
    setMessage(undefined)
    try {
      const result = await work()
      if (!mounted.current) return
      if (result.ok) done(result.value)
      else setMessage(result.error.message)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }, [])

  useEffect(() => { void run(() => agent.getLatestTransfer(), (value) => { if (value) setTransfer(value) }) }, [agent, run])

  const chosenRoot = rootId || writable[0]?.rootId || ''
  return (
    <section data-testid="transfer-panel">
      <h3 className="lifelens-card-heading">Download a file</h3>
      {writable.length === 0 ? (
        <p className="workspace-note">Approve a folder with “Save new downloads here” first.</p>
      ) : (
        <>
          <label>Address <input value={url} maxLength={2048} onChange={(event) => setUrl(event.target.value)} data-testid="transfer-url" /></label>
          <label>Folder
            <select value={chosenRoot} onChange={(event) => setRootId(event.target.value)} data-testid="transfer-root">
              {writable.map((root) => <option key={root.rootId} value={root.rootId}>{root.label}</option>)}
            </select>
          </label>
          <label>Save as <input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} data-testid="transfer-name" /></label>
          <label>What is it? <input value={intent} maxLength={300} onChange={(event) => setIntent(event.target.value)} data-testid="transfer-intent" /></label>
          <button className="primary-button" type="button" disabled={busy || !url.trim() || !name.trim() || !intent.trim() || !chosenRoot}
            onClick={() => void run(() => agent.createTransfer(url.trim(), chosenRoot, name.trim(), intent.trim()), setTransfer)}
            data-testid="transfer-create">
            Review download
          </button>
        </>
      )}

      {transfer && (
        <TransferCard transfer={transfer} busy={busy}
          onAllow={(card) => void run(() => agent.grantTransfer(transfer.taskId, card.grantId, card.grantRevision), (value) => { if (value) setTransfer(value) })}
          onDecline={(card) => void run(() => agent.declineTransfer(transfer.taskId, card.grantId, card.grantRevision), setTransfer)}
          onDownload={() => void run(() => agent.downloadTransfer(transfer.taskId), setTransfer)}
          onPlace={() => void run(() => agent.placeTransfer(transfer.taskId), setTransfer)}
          onReconcile={() => void run(() => agent.reconcileTransfer(transfer.taskId), setTransfer)} />
      )}
      {message && <p role="alert" className="workspace-note" data-testid="transfer-message">{message}</p>}
    </section>
  )
}

export interface TransferCardProps {
  transfer: AgentTransferView
  busy: boolean
  onAllow: (card: AgentTransferCardView) => void
  onDecline: (card: AgentTransferCardView) => void
  onDownload: () => void
  onPlace: () => void
  onReconcile: () => void
}

/** The trusted card. Everything shown comes from the runtime's record, rendered as inert text. */
export function TransferCard({ transfer, busy, onAllow, onDecline, onDownload, onPlace, onReconcile }: TransferCardProps) {
  const card = transfer.card
  const phase = transfer.phase
  return (
    <div className="lifelens-card" data-testid="transfer-card" data-phase={phase}>
      {card && (
        <dl>
          <dt>From</dt><dd><bdi data-testid="transfer-card-url">{card.sourceUrl}</bdi></dd>
          <dt>Save into</dt><dd><bdi>{card.destRootLabel}</bdi> as <bdi data-testid="transfer-card-name">{card.destName}</bdi></dd>
          <dt>Type</dt><dd>{card.expectedKind.toUpperCase()} only, at most {Math.ceil(card.maxBytes / 1024)} KB</dd>
          <dt>Replace existing files</dt><dd data-testid="transfer-card-overwrite">Never</dd>
          <dt>Purpose</dt><dd><bdi>{card.intent}</bdi></dd>
        </dl>
      )}
      <p className="workspace-note" data-testid="transfer-phase">{PHASE_TEXT[phase]}</p>
      {transfer.errorCode === 'type_mismatch' && (
        <p className="workspace-note" data-testid="transfer-type-mismatch">The file was not the type you approved, so Lumi will not save it.</p>
      )}
      {phase === 'awaiting_approval' && card && (
        <>
          <button className="primary-button" type="button" disabled={busy} onClick={() => onAllow(card)} data-testid="transfer-allow">
            Allow this download
          </button>
          <button className="text-button" type="button" disabled={busy} onClick={() => onDecline(card)} data-testid="transfer-decline">
            Cancel
          </button>
        </>
      )}
      {phase === 'approved' && (
        <button className="primary-button" type="button" disabled={busy} onClick={onDownload} data-testid="transfer-download">Download</button>
      )}
      {phase === 'quarantined' && (
        <button className="primary-button" type="button" disabled={busy} onClick={onPlace} data-testid="transfer-place">Save into folder</button>
      )}
      {(phase === 'download_unknown' || phase === 'placement_unknown') && (
        <button className="primary-button" type="button" disabled={busy} onClick={onReconcile} data-testid="transfer-reconcile">
          Check what happened
        </button>
      )}
    </div>
  )
}
