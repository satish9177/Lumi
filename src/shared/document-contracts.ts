import type { AgentDisclosureRecipient, AgentResult, AgentTaskStatus } from './agent-contracts'

/**
 * Milestone 10 S1 renderer-safe shapes: M10 file roots and approved documents.
 *
 * Nothing here carries an absolute path, a volume serial, a file index, a canonical root path or a
 * provider credential. A root is an id and a label; a file is an id, a display name and at most a
 * root-relative name. Extracted text appears only as the bounded `preview` for the trusted view and as
 * the exact redacted `excerpt` a disclosure card will send -- never anything the renderer can send on.
 */

export const DOCUMENT_FORMATS = ['pdf', 'docx', 'txt'] as const
export type AgentDocumentFormat = typeof DOCUMENT_FORMATS[number]

export const DOCUMENT_PHASES = ['local', 'awaiting_approval', 'approved', 'expired', 'comparing', 'compared', 'failed', 'outcome_unknown'] as const
export type AgentDocumentPhase = typeof DOCUMENT_PHASES[number]

export interface AgentFileRootView {
  rootId: string
  label: string
  canRead: boolean
  canCreate: boolean
  canModify: boolean
  revision: number
  createdAt: string
}

export interface AgentListedFileView {
  relativePath: string
  name: string
  sizeBytes: number
  modifiedAt: string
  format: AgentDocumentFormat
}

export interface AgentRootListingView {
  rootId: string
  files: AgentListedFileView[]
  truncated: boolean
}

export interface AgentDocumentFileView {
  fileId: string
  source: 'ROOT_FILE' | 'DROPPED_FILE'
  displayName: string
  relativePath?: string
  rootId?: string
  format: AgentDocumentFormat
  sizeBytes: number
  addedAt: string
}

export interface AgentDocumentView {
  documentId: string
  fileId: string
  format: AgentDocumentFormat
  pageCount?: number
  textChars: number
  truncated: boolean
  flags: Record<string, number>
  preview?: string
  expiresAt: string
  purged: boolean
}

export interface AgentDocumentCardView {
  grantId: string
  grantRevision: number
  grantStatus: 'PENDING' | 'ACTIVE' | 'REVOKED' | 'EXPIRED' | 'COMPLETED'
  expiresAt?: string
  provider: AgentDisclosureRecipient
  model: string
  purpose: string
  documents: Array<{ docRef: 'd1' | 'd2'; documentId: string; label: string; excerpt?: string }>
  maxExcerptBytes: number
  textBytes?: number
  redactionCount?: number
  truncated?: boolean
  redactionPolicy: string
}

export interface AgentDocumentDisclosureStateView {
  disclosureId: string
  status: 'STARTED' | 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN'
  errorCode?: string
  startedAt: string
  finishedAt?: string
  textBytes: number
  redactionCount: number
  truncated: boolean
}

export interface AgentDocumentComparisonView {
  kind: 'comparison' | 'cannot_compare'
  summary?: string
  reason?: string
  findings: Array<{ kind: 'match' | 'gap' | 'difference'; text: string; evidence: Array<{ docRef: 'd1' | 'd2'; quote: string }> }>
  provider: AgentDisclosureRecipient
  model: string
  createdAt: string
}

export interface AgentDocumentTaskView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  objective: string
  phase: AgentDocumentPhase
  files: AgentDocumentFileView[]
  documents: AgentDocumentView[]
  card?: AgentDocumentCardView
  disclosure?: AgentDocumentDisclosureStateView
  answer?: AgentDocumentComparisonView
}

export interface AgentDocumentShapeView {
  characters: number
  words: number
  lines: number
  headings: string[]
}

export interface AgentLocalComparisonView {
  first: AgentDocumentShapeView
  second: AgentDocumentShapeView
  sharedTerms: string[]
  onlyFirst: string[]
  onlySecond: string[]
  overlap: number
}

/** The M10 S1 bridge methods. Ids, revisions, labels, booleans and typed purposes only -- never a path. */
export interface AgentDocumentApi {
  listFileRoots: () => Promise<AgentResult<AgentFileRootView[]>>
  /** Opens a NATIVE folder dialog in main. The renderer never supplies or sees the folder's path. */
  addFileRoot: (label: string, canRead: boolean, canCreate: boolean) => Promise<AgentResult<AgentFileRootView | null>>
  revokeFileRoot: (rootId: string, expectedRevision: number) => Promise<AgentResult<AgentFileRootView>>
  listFileRootFiles: (rootId: string) => Promise<AgentResult<AgentRootListingView>>
  createDocumentTask: (objective: string) => Promise<AgentResult<AgentDocumentTaskView>>
  getDocumentTask: (taskId: string) => Promise<AgentResult<AgentDocumentTaskView>>
  addDocumentFromRoot: (taskId: string, rootId: string, relativePath: string) => Promise<AgentResult<AgentDocumentTaskView>>
  /** The dropped file main already holds, by its opaque id. Never its folder. */
  addDroppedDocument: (taskId: string, droppedId: string) => Promise<AgentResult<AgentDocumentTaskView>>
  extractDocument: (taskId: string, fileId: string) => Promise<AgentResult<AgentDocumentTaskView>>
  compareDocumentsLocally: (taskId: string, firstDocumentId: string, secondDocumentId: string) => Promise<AgentResult<AgentLocalComparisonView>>
  /** Opens the trusted card. The provider and model are chosen in main. Nothing is sent. */
  createDocumentDisclosure: (taskId: string, documentIds: string[], purpose: string) => Promise<AgentResult<AgentDocumentTaskView>>
  grantDocumentDisclosure: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDocumentTaskView>>
  declineDocumentDisclosure: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentDocumentTaskView>>
  runDocumentDisclosure: (taskId: string) => Promise<AgentResult<AgentDocumentTaskView>>
}
