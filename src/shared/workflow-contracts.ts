import type { AgentResult, AgentTaskStatus, AgentWorkflowProvenance } from './agent-contracts'

/**
 * Milestone 10 S4 renderer-safe shapes: one cross-app preparation workflow.
 *
 * A workflow is a deterministic controller over existing steps (a download, documents, an account form). It
 * owns lineage only. Nothing here can submit a form, upload a file, press a key or click: the last step
 * prepares a form locally and STOPS BEFORE SUBMIT. A candidate's kind, value and provenance are derived by
 * the runtime from the document; the renderer only picks ids.
 *
 * `value` on a candidate is the person's own document text, shown so they can decide what to adopt. An
 * adopted value is shown only as its masked preview.
 */

export const WORKFLOW_ROLES = ['download', 'documents', 'form'] as const
export type AgentWorkflowRole = typeof WORKFLOW_ROLES[number]

export const WORKFLOW_CANDIDATE_STATUSES = ['PROPOSED', 'ADOPTED', 'DISMISSED'] as const
export type AgentWorkflowCandidateStatus = typeof WORKFLOW_CANDIDATE_STATUSES[number]

export interface AgentWorkflowStepView {
  role: AgentWorkflowRole
  taskId: string
  taskStatus: AgentTaskStatus
}

export interface AgentWorkflowCandidateView {
  candidateId: string
  kind: string
  provenance: AgentWorkflowProvenance
  /** Absent once the workflow stopped or expired (the text is purged). */
  value?: string
  preview: string
  status: AgentWorkflowCandidateStatus
  documentLabel: string
  /** Provider-derived only: the grounded quote the value was lifted from. */
  quote?: string
}

export interface AgentWorkflowValueView {
  kind: string
  provenance: AgentWorkflowProvenance
  preview: string
  purged: boolean
}

export interface AgentWorkflowAdoptionView {
  actionId: string
  revision: number
  actionStatus: string
  approvalStatus?: string
  candidateId: string
  kind: string
  provenance: AgentWorkflowProvenance
  preview: string
  /** Shown while the approval is pending, so the person approves exactly what they read. */
  value?: string
  documentLabel: string
}

export interface AgentWorkflowView {
  workflowId: string
  status: 'ACTIVE' | 'STOPPED'
  live: boolean
  revision: number
  objective: string
  expiresAt: string
  stopReason?: string
  steps: AgentWorkflowStepView[]
  transferStatus?: string
  placedName?: string
  documentCount: number
  disclosureStatus?: string
  candidates: AgentWorkflowCandidateView[]
  values: AgentWorkflowValueView[]
  adoptions: AgentWorkflowAdoptionView[]
}

/** The M10 S4 bridge methods. Ids, revisions, a typed objective, and the S2 download card's own fields. */
export interface AgentWorkflowApi {
  createWorkflow: (objective: string) => Promise<AgentResult<AgentWorkflowView>>
  getWorkflow: (workflowId: string) => Promise<AgentResult<AgentWorkflowView>>
  getLatestWorkflow: () => Promise<AgentResult<AgentWorkflowView | null>>
  /** Role download: opens the S2 download card (approved separately, with main's native confirmation). */
  startWorkflowDownload: (workflowId: string, url: string, rootId: string, fileName: string, intent: string) => Promise<AgentResult<AgentWorkflowView>>
  /** Role documents: brings in exactly the placed file (same identity, same bytes). */
  startWorkflowDocuments: (workflowId: string) => Promise<AgentResult<AgentWorkflowView>>
  extractWorkflowCandidates: (workflowId: string, documentId: string) => Promise<AgentResult<AgentWorkflowView>>
  deriveWorkflowCandidates: (workflowId: string) => Promise<AgentResult<AgentWorkflowView>>
  /** Opens the adoption card. Nothing is adopted. */
  proposeWorkflowAdoption: (workflowId: string, candidateId: string) => Promise<AgentResult<AgentWorkflowView>>
  /** The trusted approval. Main shows its own native confirmation naming the detail, its value and its source. */
  approveWorkflowAdoption: (workflowId: string, actionId: string, expectedRevision: number) => Promise<AgentResult<AgentWorkflowView | null>>
  rejectWorkflowAdoption: (workflowId: string, actionId: string, expectedRevision: number) => Promise<AgentResult<AgentWorkflowView>>
  /**
   * Role form: an account task that may place ONLY this workflow's adopted values, and never submits. It
   * becomes the active task with its account-reading card open (PENDING), so the existing account and form
   * cards drive it; `recipientId` is one of the providers `getAuthenticatedOptions` offered.
   */
  startWorkflowForm: (workflowId: string, profileId: string, objective: string, recipientId: string) => Promise<AgentResult<AgentWorkflowView>>
  stopWorkflow: (workflowId: string) => Promise<AgentResult<AgentWorkflowView>>
}
