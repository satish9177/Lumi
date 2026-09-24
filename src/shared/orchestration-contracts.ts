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
  'approval_required', 'budget_exhausted', 'loop_detected', 'capability_unavailable',
  /** Milestone 11 S4: a human must act outside Lumi (a login, a CAPTCHA, an unsupported control). */
  'manual_handoff_required',
  /** A linked capability's own effect is unresolved -- never guessed, only a person's own look settles it. */
  'outcome_unknown'
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

/**
 * Milestone 12 S1: a controller-issued, opaque resource this orchestration currently owns (not consumed,
 * not expired). `safeLabel` is controller-authored template text, never the underlying page, document,
 * account or desktop content. Seeing one here is never authority to use it with any particular capability.
 */
export interface AgentOrchestrationResourceView {
  ref: string
  kind: string
  privacyClass: 'public' | 'private' | 'none'
  safeLabel: string
  singleUse: boolean
  /**
   * Milestone 12 S2: model-invisible backing identity (a file/document id, or a checked URL). Main-process
   * use only -- `orchestration-coordinator.ts` never forwards these into the planner's own context, which
   * is built from `ref`/`kind`/`safeLabel` alone (see `orchestrationStateLines` in `orchestration-planner.ts`).
   */
  backingId?: string
  backingText?: string
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
  /** Milestone 12 S1: resources the planner may cite right now. Optional for callers that predate S1. */
  resources?: AgentOrchestrationResourceView[]
  /**
   * Milestone 12 S2: the one document task this orchestration's document resources refer into, if any.
   * Main-process use only (attaching a further document); never shown to the planner.
   */
  documentTaskId?: string
  steps: AgentOrchestrationStepView[]
}

/**
 * Milestone 11 S4: the renderer-facing bridge. `createOrchestration` and `continueOrchestration` each run
 * the whole "observe -> plan -> dispatch -> persist -> re-plan" loop to its next pause or terminal state in
 * one call (exactly like the existing `runResearch`), never exposing a raw single-step primitive the
 * renderer could misuse to skip a capability's own approval.
 */
export interface AgentOrchestrationApi {
  /** Creates a new orchestration for this objective and runs it to its first pause or terminal state. */
  createOrchestration: (objective: string) => Promise<AgentResult<AgentOrchestrationView>>
  getOrchestration: (orchestrationId: string) => Promise<AgentResult<AgentOrchestrationView>>
  getLatestOrchestration: () => Promise<AgentResult<AgentOrchestrationView | null>>
  /**
   * Resumes a paused orchestration and runs it to its next pause or terminal state. Always re-validates
   * durable state first -- never assumes a paused step resolved just because the user pressed Continue.
   */
  continueOrchestration: (orchestrationId: string) => Promise<AgentResult<AgentOrchestrationView>>
  stopOrchestration: (orchestrationId: string) => Promise<AgentResult<AgentOrchestrationView>>
  /**
   * Milestone 12 S2: makes one already-approved document (from an already-approved root) available to this
   * orchestration as a `document_ref` resource. Never reachable from the planner or the model -- this is a
   * trusted renderer action, exactly like approving a file root itself.
   */
  attachApprovedDocument: (
    orchestrationId: string, rootId: string, relativePath: string
  ) => Promise<AgentResult<AgentOrchestrationView>>
}
