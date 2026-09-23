import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentApi } from '../../../shared/agent-contracts'
import type {
  AgentDocumentTaskView,
  AgentFileRootView,
  AgentLocalComparisonView,
  AgentRootListingView
} from '../../../shared/document-contracts'
import { TransferPanel } from './TransferPanel'
import { ProjectPanel } from './ProjectPanel'
import { WorkflowPanel } from './WorkflowPanel'
import './components.css'

export interface DocumentPanelProps {
  agent: AgentApi
  /** The file the person dropped on Lumi, by its opaque id, if any. Never a path. */
  droppedFile?: { droppedId: string; fileName: string }
  onClose: () => void
}

const MAX_PURPOSE = 400

/**
 * Milestone 10 S1: approved documents.
 *
 *   1. File access: approve a folder in a NATIVE dialog, with explicit permissions (read, and/or save new
 *      files into). Approving a folder for search elsewhere in Lumi does not give it any of these.
 *   2. Documents: add a file from a readable folder (or the one file you dropped), read its text on this
 *      device, and compare two documents on this device. Nothing is sent anywhere.
 *   3. Optionally, a trusted card shows EXACTLY which excerpt of each document ONE named AI provider would
 *      see, for the purpose you typed. Only "Allow once" sends it, once.
 *
 * Document text is shown only inside labelled, inert text nodes. The panel never sees or sends a path.
 */
export function DocumentPanel({ agent, droppedFile, onClose }: DocumentPanelProps) {
  const [roots, setRoots] = useState<AgentFileRootView[]>([])
  const [label, setLabel] = useState('')
  const [canRead, setCanRead] = useState(true)
  const [canCreate, setCanCreate] = useState(false)
  const [listing, setListing] = useState<AgentRootListingView>()
  const [task, setTask] = useState<AgentDocumentTaskView>()
  const [comparison, setComparison] = useState<AgentLocalComparisonView>()
  const [purpose, setPurpose] = useState('')
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

  const loadRoots = useCallback(() => run(() => agent.listFileRoots(), setRoots), [agent, run])
  useEffect(() => { void loadRoots() }, [loadRoots])

  const ensureTask = async (): Promise<AgentDocumentTaskView | undefined> => {
    if (task) return task
    const created = await agent.createDocumentTask('')
    if (!created.ok) {
      setMessage(created.error.message)
      return undefined
    }
    setTask(created.value)
    return created.value
  }

  const documents = task?.documents ?? []

  return (
    <section className="settings-scroll" data-testid="document-panel">
      <header className="settings-header">
        <h2>Documents</h2>
        <button className="icon-button" type="button" aria-label="Close documents" onClick={onClose}>&times;</button>
      </header>

      <h3 className="lifelens-card-heading">File access</h3>
      <p className="workspace-note">
        Approve a folder for Lumi’s document features. Reading lets Lumi read documents you choose from it; saving lets Lumi put
        downloads you approve into it. Lumi can never change or delete files there.
      </p>
      <ul data-testid="document-roots">
        {roots.map((root) => (
          <li key={root.rootId}>
            <bdi>{root.label}</bdi> — {[root.canRead ? 'read' : undefined, root.canCreate ? 'save new files' : undefined].filter(Boolean).join(', ')}
            <button className="text-button" type="button" disabled={busy} onClick={() => void run(() => agent.listFileRootFiles(root.rootId), setListing)}
              data-testid="document-root-browse" hidden={!root.canRead}>
              Browse
            </button>
            <button className="text-button" type="button" disabled={busy}
              onClick={() => void run(() => agent.revokeFileRoot(root.rootId, root.revision), () => void loadRoots())} data-testid="document-root-revoke">
              Remove
            </button>
          </li>
        ))}
      </ul>
      <label>Name <input value={label} maxLength={64} onChange={(event) => setLabel(event.target.value)} data-testid="document-root-label" /></label>
      <label><input type="checkbox" checked={canRead} onChange={(event) => setCanRead(event.target.checked)} data-testid="document-root-read" /> Read documents</label>
      <label><input type="checkbox" checked={canCreate} onChange={(event) => setCanCreate(event.target.checked)} data-testid="document-root-create" /> Save new downloads here</label>
      <button className="primary-button" type="button" disabled={busy || !label.trim() || (!canRead && !canCreate)}
        onClick={() => void run(() => agent.addFileRoot(label.trim(), canRead, canCreate), () => { setLabel(''); void loadRoots() })}
        data-testid="document-root-add">
        Choose folder…
      </button>

      {listing && (
        <ul data-testid="document-listing">
          {listing.files.map((file) => (
            <li key={file.relativePath}>
              <bdi>{file.relativePath}</bdi>
              <button className="text-button" type="button" disabled={busy}
                onClick={() => void run(async () => {
                  const current = await ensureTask()
                  if (!current) return { ok: false as const, error: { message: 'Lumi could not start a document task.' } }
                  return agent.addDocumentFromRoot(current.taskId, listing.rootId, file.relativePath)
                }, setTask)} data-testid="document-add-file">
                Add
              </button>
            </li>
          ))}
          {listing.truncated && <li className="workspace-note">Only the first files are listed.</li>}
        </ul>
      )}
      {droppedFile && (
        <button className="text-button" type="button" disabled={busy}
          onClick={() => void run(async () => {
            const current = await ensureTask()
            if (!current) return { ok: false as const, error: { message: 'Lumi could not start a document task.' } }
            return agent.addDroppedDocument(current.taskId, droppedFile.droppedId)
          }, setTask)} data-testid="document-add-dropped">
          Use the dropped file <bdi>{droppedFile.fileName}</bdi> (only that file)
        </button>
      )}

      {task && (
        <>
          <h3 className="lifelens-card-heading">This task’s files</h3>
          <ul data-testid="document-files">
            {task.files.map((file) => {
              const document = documents.find((item) => item.fileId === file.fileId)
              return (
                <li key={file.fileId}>
                  <bdi>{file.displayName}</bdi> ({file.format.toUpperCase()})
                  {document ? (
                    <details>
                      <summary>{document.purged ? 'Text expired' : `Read on this device (${document.textChars} characters${document.truncated ? ', truncated' : ''})`}</summary>
                      {document.preview !== undefined && (
                        <pre className="workspace-note" data-testid="document-preview">
                          <span className="visually-hidden">Text from the document, not from Lumi: </span>
                          <bdi>{document.preview}</bdi>
                        </pre>
                      )}
                    </details>
                  ) : (
                    <button className="text-button" type="button" disabled={busy}
                      onClick={() => void run(() => agent.extractDocument(task.taskId, file.fileId), setTask)} data-testid="document-extract">
                      Read text on this device
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
          {documents.length === 2 && (
            <button className="text-button" type="button" disabled={busy}
              onClick={() => void run(() => agent.compareDocumentsLocally(task.taskId, documents[0].documentId, documents[1].documentId), setComparison)}
              data-testid="document-compare-local">
              Compare on this device
            </button>
          )}
          {comparison && (
            <div data-testid="document-local-comparison">
              <p>Shared terms: <bdi>{comparison.sharedTerms.slice(0, 15).join(', ') || 'none'}</bdi></p>
              <p>Only in the first: <bdi>{comparison.onlyFirst.slice(0, 10).join(', ') || 'none'}</bdi></p>
              <p>Only in the second: <bdi>{comparison.onlySecond.slice(0, 10).join(', ') || 'none'}</bdi></p>
            </div>
          )}
          {documents.length > 0 && !task.card && (
            <>
              <textarea aria-label="Why do you want an AI to compare these" value={purpose} maxLength={MAX_PURPOSE} rows={2}
                placeholder="How well does my resume match this job?" onChange={(event) => setPurpose(event.target.value)}
                data-testid="document-purpose" />
              <button className="primary-button" type="button" disabled={busy || !purpose.trim()}
                onClick={() => void run(() => agent.createDocumentDisclosure(task.taskId, documents.slice(0, 2).map((item) => item.documentId), purpose.trim()), setTask)}
                data-testid="document-request-disclosure">
                Prepare AI comparison
              </button>
            </>
          )}
          <DocumentDisclosureCard view={task} busy={busy}
            onAllow={(view) => view.card && void run(async () => {
              const granted = await agent.grantDocumentDisclosure(view.taskId, view.card!.grantId, view.card!.grantRevision)
              if (!granted.ok) return granted
              return agent.runDocumentDisclosure(view.taskId)
            }, setTask)}
            onDecline={(view) => view.card && void run(() => agent.declineDocumentDisclosure(view.taskId, view.card!.grantId, view.card!.grantRevision), setTask)}
          />
        </>
      )}
      {message && <p role="alert" data-testid="document-message">{message}</p>}

      <TransferPanel agent={agent} roots={roots} />

      <WorkflowPanel agent={agent} roots={roots} />

      <ProjectPanel agent={agent} />
    </section>
  )
}

export interface DocumentDisclosureCardProps {
  view: AgentDocumentTaskView
  busy: boolean
  onAllow: (view: AgentDocumentTaskView) => void
  onDecline: (view: AgentDocumentTaskView) => void
}

/** The trusted card and its outcomes. Every word outside the labelled document text is app-authored. */
export function DocumentDisclosureCard({ view, busy, onAllow, onDecline }: DocumentDisclosureCardProps) {
  const card = view.card
  if (!card) return null
  if (view.phase === 'awaiting_approval') {
    return (
      <article className="agent-booking-card tone-uncertain" role="group" aria-label="Allow sending document excerpts to an AI"
        data-testid="document-disclosure-card" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">SEND THESE EXCERPTS TO ONE AI, ONCE?</p>
        <p data-testid="document-recipient"><strong>{recipientName(card.provider)}</strong> ({card.model}) will receive exactly the text below — nothing else, and no file names or folders.</p>
        <p>For this purpose: <q data-testid="document-purpose-shown"><bdi>{card.purpose}</bdi></q></p>
        {card.documents.map((item) => (
          <div key={item.docRef} data-testid="document-card-excerpt">
            <p><strong>{item.docRef === 'd1' ? 'First document' : 'Second document'}</strong> (<bdi>{item.label}</bdi>) — shown to the AI as “{item.docRef}”</p>
            <pre className="workspace-note">
              <span className="visually-hidden">Document text, not from Lumi: </span>
              <bdi>{item.excerpt ?? '(no longer available)'}</bdi>
            </pre>
          </div>
        ))}
        <p className="workspace-note">
          At most {card.maxExcerptBytes} bytes per document; {card.redactionCount ?? 0} email address or phone number(s) replaced. If this provider
          fails, Lumi stops: it never sends these excerpts to another provider or model.
        </p>
        <button className="text-button" type="button" disabled={busy} onClick={() => onDecline(view)} data-testid="document-decline">Cancel</button>
        <button className="primary-button" type="button" disabled={busy} onClick={() => onAllow(view)} data-testid="document-allow">Allow once</button>
      </article>
    )
  }
  if (view.phase === 'compared' && view.answer) {
    const answer = view.answer
    return (
      <article className="agent-booking-card tone-success" role="group" aria-label="Document comparison" data-testid="document-answer" data-phase={view.phase}>
        <p className="lifelens-card-eyebrow">COMPARISON BY {recipientName(answer.provider).toUpperCase()}</p>
        {answer.summary && <p><bdi>{answer.summary}</bdi></p>}
        <ul className="agent-evidence">
          {answer.findings.map((finding, index) => (
            <li key={index}>
              {finding.kind}: <bdi>{finding.text}</bdi>
              {finding.evidence.map((evidence, position) => (
                <span key={position}> — {evidence.docRef}: <q><bdi>{evidence.quote}</bdi></q></span>
              ))}
            </li>
          ))}
        </ul>
        <p className="workspace-note">An AI’s reading of your documents, grounded in the quoted excerpts. It is not saved to memory.</p>
      </article>
    )
  }
  const text = view.phase === 'comparing' ? 'Asking the approved provider…'
    : view.phase === 'failed' ? 'The provider could not produce a grounded comparison. Nothing was retried.'
      : view.phase === 'outcome_unknown' ? 'Lumi cannot tell whether the excerpts reached the provider, so it did not try again.'
        : view.phase === 'expired' ? 'That approval expired. Nothing was sent.'
          : undefined
  if (!text) return null
  return (
    <article className="agent-booking-card tone-uncertain" role="status" data-testid="document-ended" data-phase={view.phase}>
      <p>{text}</p>
    </article>
  )
}

function recipientName(recipient: string): string {
  switch (recipient) {
    case 'openai': return 'OpenAI'
    case 'gemini': return 'Google Gemini'
    case 'deepseek': return 'DeepSeek'
    case 'scripted': return 'the built-in test model'
    default: return 'the approved provider'
  }
}
