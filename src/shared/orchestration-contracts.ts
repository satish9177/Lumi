import type { AgentCapabilityId } from './agent-capabilities'
import type { AgentResult } from './agent-contracts'

/**
 * Milestone 11 S2 renderer-safe shapes: the durable read-only orchestration graph.
 *
 * An orchestration composes capabilities that already exist; it holds no authority of its own. A step
 * either links an existing child task another capability's own boundary created (`childTaskId`), or is
 * resolved synchronously with no new task at all. There is no path, URL, command or raw private evidence
 * anywhere in this shape -- `resultSummary` is a controller-authored, bounded summary, never a capability's
 * raw private answer.
 */

export const ORCHESTRATION_STATUSES = ['RUNNING', 'PAUSED', 'SUCCEEDED', 'FAILED', 'STOPPED'] as const
export type AgentOrchestrationStatus = typeof ORCHESTRATION_STATUSES[number]

export const ORCHESTRATION_STEP_STATUSES = ['PENDING', 'AWAITING_APPROVAL', 'SUCCEEDED', 'FAILED'] as const
export type AgentOrchestrationStepStatus = typeof ORCHESTRATION_STEP_STATUSES[number]

export const ORCHESTRATION_PAUSE_REASONS = [
  'approval_required', 'budget_exhausted', 'loop_detected', 'capability_unavailable'
] as const
export type AgentOrchestrationPauseReason = typeof ORCHESTRATION_PAUSE_REASONS[number]

export interface AgentOrchestrationStepView {
  sequence: number
  capabilityId: string
  status: AgentOrchestrationStepStatus
  childTaskId?: string
  resultHandle?: string
  resultSummary?: string
}

export interface AgentOrchestrationView {
  orchestrationId: string
  status: AgentOrchestrationStatus
  pauseReason?: AgentOrchestrationPauseReason
  live: boolean
  revision: number
  objective: string
  stepCount: number
  childTaskCount: number
  plannerCalls: number
  createdAt: string
  expiresAt: string
  stoppedAt?: string
  /** What the planner may choose right now -- a subset of the full Milestone 11 S1 catalog. */
  availableCapabilities: AgentCapabilityId[]
  steps: AgentOrchestrationStepView[]
}

/** The low-level bridge to the durable graph. `advanceOrchestration` records one already-decided step. */
export interface AgentOrchestrationApi {
  createOrchestration: (objective: string) => Promise<AgentResult<AgentOrchestrationView>>
  getOrchestration: (orchestrationId: string) => Promise<AgentResult<AgentOrchestrationView>>
  getLatestOrchestration: () => Promise<AgentResult<AgentOrchestrationView | null>>
  stopOrchestration: (orchestrationId: string) => Promise<AgentResult<AgentOrchestrationView>>
}
