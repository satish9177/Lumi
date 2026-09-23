import type { AgentResult, AgentTaskStatus } from './agent-contracts'

/**
 * Milestone 10 S2 renderer-safe shapes: one controlled download into one approved folder.
 *
 * Nothing here carries a quarantine path, an absolute destination path, a file index or a volume serial.
 * The destination is a folder id and label plus one validated file name; there is no overwrite flag the
 * renderer can set (`overwrite` is always false and shown only so the card can say so).
 */

export const TRANSFER_KINDS = ['pdf', 'docx', 'txt'] as const
export type AgentTransferKind = typeof TRANSFER_KINDS[number]

export const TRANSFER_PHASES = [
  'awaiting_approval', 'approved', 'declined', 'downloading', 'quarantined', 'placing', 'placed', 'failed',
  'download_unknown', 'placement_unknown'
] as const
export type AgentTransferPhase = typeof TRANSFER_PHASES[number]

export interface AgentTransferCardView {
  grantId: string
  grantRevision: number
  grantStatus: 'PENDING' | 'ACTIVE' | 'REVOKED' | 'EXPIRED' | 'COMPLETED'
  expiresAt?: string
  sourceUrl: string
  sourceOrigin: string
  intent: string
  destRootId: string
  destRootLabel: string
  destName: string
  expectedKind: AgentTransferKind
  maxBytes: number
  overwrite: false
}

export interface AgentTransferView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  transferId: string
  phase: AgentTransferPhase
  status: string
  errorCode?: string
  downloadStatus?: string
  placeStatus?: string
  length?: number
  sha256?: string
  kind?: string
  destRootLabel: string
  destName: string
  card?: AgentTransferCardView
}

/** The M10 S2 bridge methods. A URL, a folder id, a file name and a typed intent -- never a path. */
export interface AgentTransferApi {
  /** Opens the card. Nothing is fetched. */
  createTransfer: (url: string, rootId: string, fileName: string, intent: string) => Promise<AgentResult<AgentTransferView>>
  getTransfer: (taskId: string) => Promise<AgentResult<AgentTransferView>>
  getLatestTransfer: () => Promise<AgentResult<AgentTransferView | null>>
  /** The trusted approval. Main shows its own native confirmation naming the source, folder and name. */
  grantTransfer: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTransferView | null>>
  declineTransfer: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentTransferView>>
  /** Step 1: fetch once into the quarantine. */
  downloadTransfer: (taskId: string) => Promise<AgentResult<AgentTransferView>>
  /** Step 2: place the verified file into the approved folder. Never overwrites. */
  placeTransfer: (taskId: string) => Promise<AgentResult<AgentTransferView>>
  /** Read-only: settle an unknown step from local evidence. Never downloads again. */
  reconcileTransfer: (taskId: string) => Promise<AgentResult<AgentTransferView>>
}
