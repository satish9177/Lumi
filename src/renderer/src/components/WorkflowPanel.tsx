import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentBrowserProfileView,
  AgentDisclosureRecipient,
  AgentWorkflowProvenance
} from '../../../shared/agent-contracts'
import type { AgentDocumentTaskView, AgentFileRootView } from '../../../shared/document-contracts'
import type { AgentTransferView } from '../../../shared/transfer-contracts'
import type { AgentWorkflowAdoptionView, AgentWorkflowCandidateView, AgentWorkflowView } from '../../../shared/workflow-contracts'
import { TransferCard } from './TransferPanel'
import './components.css'

export interface WorkflowPanelProps {
  agent: AgentApi
  /** Folders the person approved; the download step offers only those allowed to receive new files. */
  roots: AgentFileRootView[]
}

const KIND_LABELS: Record<string, string> = {
  legal_name: 'Legal name',
  preferred_name: 'Preferred name',
  email: 'Email',
  phone: 'Phone',
  city: 'City',
  country: 'Country',
  linkedin_url: 'LinkedIn link',
  portfolio_url: 'Portfolio link'
}

const PROVENANCE_TEXT: Record<AgentWorkflowProvenance, string> = {
  document_extracted: 'Found in your document',
  provider_derived: 'Suggested by the AI from the approved excerpt'
}

type Result<T> = { ok: true; value: T } | { ok: false; error: { message: string } }

/**
 * Milestone 10 S4: one cross-app preparation workflow — download, read, pick details, prepare a form.
 *
 * Each step keeps its own approval: the download card (and Lumi's own dialog), the document disclosure card
 * (in the documents section above), an explicit adoption of each detail (confirmed again by Lumi's own
 * dialog), and the account and form cards of the task panel. Nothing here submits, uploads or clicks on a
 * website: the form is prepared locally and Lumi stops before submitting.
 */
export function WorkflowPanel({ agent, roots }: WorkflowPanelProps) {
  const writable = roots.filter((root) => root.canCreate)
  const [workflow, setWorkflow] = useState<AgentWorkflowView>()
  const [transfer, setTransfer] = useState<AgentTransferView>()
  const [documents, setDocuments] = useState<AgentDocumentTaskView>()
  const [profiles, setProfiles] = useState<AgentBrowserProfileView[]>([])
  const [recipients, setRecipients] = useState<AgentDisclosureRecipient[]>([])
  const [objective, setObjective] = useState('')
  const [url, setUrl] = useState('')
  const [rootId, setRootId] = useState('')
  const [name, setName] = useState('')
  const [intent, setIntent] = useState('')
  const [profileId, setProfileId] = useState('')
  const [recipient, setRecipient] = useState('')
  const [formGoal, setFormGoal] = useState('')
  const [message, setMessage] = useState<string>()
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const run = useCallback(async <T,>(work: () => Promise<Result<T>>, done: (value: T) => void) => {
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

  const stepTask = (role: string): string | undefined => workflow?.steps.find((step) => step.role === role)?.taskId
  const downloadTask = stepTask('download')
  const documentsTask = stepTask('documents')
  const formTask = stepTask('form')

  useEffect(() => { void run(() => agent.getLatestWorkflow(), (value) => { if (value) setWorkflow(value) }) }, [agent, run])
  useEffect(() => {
    void run(() => agent.listBrowserProfiles(), (value) => setProfiles(value.filter((profile) => profile.status === 'AUTHENTICATED')))
    void run(() => agent.getAuthenticatedOptions(), (value) => setRecipients(value.recipients))
  }, [agent, run])
  useEffect(() => {
    if (downloadTask) void run(() => agent.getTransfer(downloadTask), setTransfer)
  }, [agent, run, downloadTask, workflow?.revision])
  useEffect(() => {
    if (documentsTask) void run(() => agent.getDocumentTask(documentsTask), setDocuments)
  }, [agent, run, documentsTask, workflow?.revision])

  const refresh = (id: string): void => { void run(() => agent.getWorkflow(id), setWorkflow) }

  if (!workflow || workflow.status === 'STOPPED') {
    return (
      <section data-testid="workflow-panel">
        <h3 className="lifelens-card-heading">Prepare a form from a downloaded document</h3>
        {workflow && <p className="workspace-note" data-testid="workflow-stopped">The last workflow was stopped. Nothing further was prepared.</p>}
        <label>What are you preparing?
          <input value={objective} maxLength={300} onChange={(event) => setObjective(event.target.value)} data-testid="workflow-objective" />
        </label>
        <button className="primary-button" type="button" disabled={busy || !objective.trim()}
          onClick={() => void run(() => agent.createWorkflow(objective.trim()), setWorkflow)} data-testid="workflow-create">
          Start
        </button>
        {message && <p role="alert" className="workspace-note" data-testid="workflow-message">{message}</p>}
      </section>
    )
  }

  const id = workflow.workflowId
  const chosenRoot = rootId || writable[0]?.rootId || ''
  const chosenProfile = profileId || profiles[0]?.profileId || ''
  const chosenRecipient = recipient || recipients[0] || ''
  const pending = workflow.adoptions.filter((item) => item.actionStatus === 'WAITING_APPROVAL')
  return (
    <section data-testid="workflow-panel" data-workflow={id}>
      <h3 className="lifelens-card-heading">Preparing: <bdi>{workflow.objective}</bdi></h3>
      <p className="workspace-note">Lumi prepares the form for you to check. It never submits it, uploads a file or clicks on the website.</p>
      <button className="text-button" type="button" disabled={busy} onClick={() => refresh(id)} data-testid="workflow-refresh">Refresh</button>
      <button className="text-button" type="button" onClick={() => void run(() => agent.stopWorkflow(id), setWorkflow)} data-testid="workflow-stop">
        Stop this workflow
      </button>

      <h4>1. Download the document</h4>
      {!downloadTask ? (
        writable.length === 0 ? <p className="workspace-note">Approve a folder with “Save new downloads here” first.</p> : (
          <>
            <label>Address <input value={url} maxLength={2048} onChange={(event) => setUrl(event.target.value)} data-testid="workflow-url" /></label>
            <label>Folder
              <select value={chosenRoot} onChange={(event) => setRootId(event.target.value)} data-testid="workflow-root">
                {writable.map((root) => <option key={root.rootId} value={root.rootId}>{root.label}</option>)}
              </select>
            </label>
            <label>Save as <input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} data-testid="workflow-name" /></label>
            <label>What is it? <input value={intent} maxLength={300} onChange={(event) => setIntent(event.target.value)} data-testid="workflow-intent" /></label>
            <button className="primary-button" type="button" disabled={busy || !url.trim() || !name.trim() || !intent.trim() || !chosenRoot}
              onClick={() => void run(() => agent.startWorkflowDownload(id, url.trim(), chosenRoot, name.trim(), intent.trim()), setWorkflow)}
              data-testid="workflow-download">
              Review download
            </button>
          </>
        )
      ) : transfer && (
        <TransferCard transfer={transfer} busy={busy}
          onAllow={(card) => void run(() => agent.grantTransfer(transfer.taskId, card.grantId, card.grantRevision), (value) => { if (value) setTransfer(value) })}
          onDecline={(card) => void run(() => agent.declineTransfer(transfer.taskId, card.grantId, card.grantRevision), setTransfer)}
          onDownload={() => void run(() => agent.downloadTransfer(transfer.taskId), setTransfer)}
          onPlace={() => void run(() => agent.placeTransfer(transfer.taskId), (value) => { setTransfer(value); refresh(id) })}
          onReconcile={() => void run(() => agent.reconcileTransfer(transfer.taskId), setTransfer)} />
      )}

      <h4>2. Read it</h4>
      {!documentsTask ? (
        <button className="primary-button" type="button" disabled={busy || workflow.transferStatus !== 'PLACED'}
          onClick={() => void run(() => agent.startWorkflowDocuments(id), setWorkflow)} data-testid="workflow-documents">
          Read the saved file
        </button>
      ) : documents && (
        <ul data-testid="workflow-documents-list">
          {documents.files.map((file) => {
            const document = documents.documents.find((item) => item.fileId === file.fileId)
            return (
              <li key={file.fileId}>
                <bdi>{file.displayName}</bdi>{' '}
                {!document ? (
                  <button className="text-button" type="button" disabled={busy}
                    onClick={() => void run(() => agent.extractDocument(documents.taskId, file.fileId), setDocuments)}
                    data-testid="workflow-extract">Extract text</button>
                ) : (
                  <button className="text-button" type="button" disabled={busy || document.purged}
                    onClick={() => void run(() => agent.extractWorkflowCandidates(id, document.documentId), setWorkflow)}
                    data-testid="workflow-find">Find details</button>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {workflow.disclosureStatus === 'SUCCEEDED' && (
        <button className="text-button" type="button" disabled={busy}
          onClick={() => void run(() => agent.deriveWorkflowCandidates(id), setWorkflow)} data-testid="workflow-derive">
          Use the AI comparison’s quotes
        </button>
      )}

      <h4>3. Choose the details to use</h4>
      <WorkflowCandidates candidates={workflow.candidates} busy={busy}
        onAdopt={(candidate) => void run(() => agent.proposeWorkflowAdoption(id, candidate.candidateId), setWorkflow)} />
      {pending.map((card) => (
        <WorkflowAdoptionCard key={card.actionId} card={card} busy={busy}
          onAdopt={() => void run(() => agent.approveWorkflowAdoption(id, card.actionId, card.revision), (value) => { if (value) setWorkflow(value) })}
          onDecline={() => void run(() => agent.rejectWorkflowAdoption(id, card.actionId, card.revision), setWorkflow)} />
      ))}
      {workflow.values.length > 0 && (
        <ul data-testid="workflow-values">
          {workflow.values.map((value) => (
            <li key={value.kind}>✓ {KIND_LABELS[value.kind] ?? value.kind}: <bdi>{value.preview}</bdi> — {PROVENANCE_TEXT[value.provenance]}</li>
          ))}
        </ul>
      )}

      <h4>4. Prepare the form</h4>
      {formTask ? (
        <p className="workspace-note" data-testid="workflow-form-started">
          The account card is open in the task panel. The form can use only the details adopted above, each with its own approval, and Lumi stops before submitting.
        </p>
      ) : profiles.length === 0 || recipients.length === 0 ? (
        <p className="workspace-note">Sign in to the website in a Lumi browser profile first.</p>
      ) : (
        <>
          <label>Account
            <select value={chosenProfile} onChange={(event) => setProfileId(event.target.value)} data-testid="workflow-profile">
              {profiles.map((profile) => <option key={profile.profileId} value={profile.profileId}>{profile.label}</option>)}
            </select>
          </label>
          <label>AI to read the form
            <select value={chosenRecipient} onChange={(event) => setRecipient(event.target.value)} data-testid="workflow-recipient">
              {recipients.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label>What should Lumi prepare? <input value={formGoal} maxLength={300} onChange={(event) => setFormGoal(event.target.value)} data-testid="workflow-form-goal" /></label>
          <button className="primary-button" type="button" disabled={busy || workflow.values.length === 0 || !formGoal.trim() || !chosenProfile || !chosenRecipient}
            onClick={() => void run(() => agent.startWorkflowForm(id, chosenProfile, formGoal.trim(), chosenRecipient), setWorkflow)}
            data-testid="workflow-form">
            Open the account form
          </button>
        </>
      )}
      {message && <p role="alert" className="workspace-note" data-testid="workflow-message">{message}</p>}
    </section>
  )
}

export interface WorkflowCandidatesProps {
  candidates: AgentWorkflowCandidateView[]
  busy: boolean
  onAdopt: (candidate: AgentWorkflowCandidateView) => void
}

/** Candidate details, rendered as inert text. None is used until the person adopts it. */
export function WorkflowCandidates({ candidates, busy, onAdopt }: WorkflowCandidatesProps) {
  if (candidates.length === 0) return <p className="workspace-note">No details found yet.</p>
  return (
    <ul data-testid="workflow-candidates">
      {candidates.map((candidate) => (
        <li key={candidate.candidateId} data-provenance={candidate.provenance} data-status={candidate.status}>
          <strong>{KIND_LABELS[candidate.kind] ?? candidate.kind}</strong>: <bdi>{candidate.value ?? candidate.preview}</bdi>
          {' — '}{PROVENANCE_TEXT[candidate.provenance]} (<bdi>{candidate.documentLabel}</bdi>)
          {candidate.quote && <> quoting <q><bdi>{candidate.quote}</bdi></q></>}
          {candidate.status === 'PROPOSED' && candidate.value !== undefined && (
            <button className="text-button" type="button" disabled={busy} onClick={() => onAdopt(candidate)} data-testid="workflow-candidate-adopt">
              Use this…
            </button>
          )}
          {candidate.status === 'ADOPTED' && <> — adopted</>}
        </li>
      ))}
    </ul>
  )
}

export interface WorkflowAdoptionCardProps {
  card: AgentWorkflowAdoptionView
  busy: boolean
  onAdopt: () => void
  onDecline: () => void
}

/** The trusted adoption card: exactly the detail, its value and its source. Lumi confirms again natively. */
export function WorkflowAdoptionCard({ card, busy, onAdopt, onDecline }: WorkflowAdoptionCardProps) {
  return (
    <div className="lifelens-card" data-testid="workflow-adoption-card" data-provenance={card.provenance}>
      <dl>
        <dt>Detail</dt><dd>{KIND_LABELS[card.kind] ?? card.kind}</dd>
        <dt>Value</dt><dd><bdi data-testid="workflow-adoption-value">{card.value ?? card.preview}</bdi></dd>
        <dt>Source</dt><dd data-testid="workflow-adoption-source">{PROVENANCE_TEXT[card.provenance]} (<bdi>{card.documentLabel}</bdi>)</dd>
        <dt>Used for</dt><dd>This workflow only. It is not saved as one of your own details, and every form it goes into needs its own approval.</dd>
      </dl>
      <button className="primary-button" type="button" disabled={busy} onClick={onAdopt} data-testid="workflow-adoption-approve">Adopt</button>
      <button className="text-button" type="button" disabled={busy} onClick={onDecline} data-testid="workflow-adoption-decline">Decline</button>
    </div>
  )
}
