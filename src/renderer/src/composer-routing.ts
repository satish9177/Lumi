import type { AgentInspectionView, AgentResult, TypedRequestRoute } from '../../shared/agent-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import { describeInspectionFailure } from './agent-task-view'

/**
 * The main composer's single decision: which path owns a typed request.
 *
 * Main decides (see `TaskRequestInterpreter.route`); this module only obeys.
 * A request main reports as handled never reaches the realtime conversation,
 * so no conversation tool (`open_url`, Telegram, capture) can act on it. If
 * main cannot be asked at all, the error propagates and the request goes
 * nowhere: failing closed, never falling through.
 */

export interface ComposerRoutingDependencies {
  route: (requestId: string, text: string) => Promise<TypedRequestRoute>
  /** The ordinary realtime conversation. Called at most once, only for unhandled requests. */
  converse: (text: string) => Promise<void>
  /** The durable agent owned the request; show its outcome. */
  agentHandled: (text: string, result: AgentResult<VoiceTaskOutcome>) => void
  newRequestId?: () => string
}

export type ComposerRoute = 'agent' | 'conversation'

export function newComposerRequestId(): string {
  return `req_${crypto.randomUUID().replaceAll('-', '')}`
}

export async function submitComposerRequest(text: string, deps: ComposerRoutingDependencies): Promise<ComposerRoute> {
  const route = await deps.route((deps.newRequestId ?? newComposerRequestId)(), text)
  if (route.handled) {
    deps.agentHandled(text, route.result)
    return 'agent'
  }
  await deps.converse(text)
  return 'conversation'
}

/**
 * One conversation line for a finished page inspection, or nothing while it is
 * still waiting or running. Every word is Lumi's except the verified answer,
 * which is shown as plain text after an app-authored label and never as a
 * control or an instruction.
 */
export function describeInspectionForConversation(inspection: AgentInspectionView): string | undefined {
  const host = inspection.proposal.host
  const attempt = inspection.attempts.at(-1)
  switch (inspection.status) {
    case 'SUCCEEDED': {
      const answer = inspection.answer
      if (!answer) return `Lumi read ${host} but has no answer yet. Use Answer from saved page in Agent tasks; the page will not be opened again.`
      if (answer.status === 'answered') return `From the inspected page on ${host}: ${answer.answer}`
      return `Lumi read ${host} but could not verify an answer to your question from it.`
    }
    case 'FAILED':
      return `Lumi could not read ${host}. ${describeInspectionFailure(attempt?.errorCode, attempt?.httpStatus)} Nothing was answered.`
    case 'OUTCOME_UNKNOWN':
    case 'RECONCILING':
      return `Lumi does not know what was read from ${host}. It will not retry by itself.`
    case 'REJECTED':
      return `You rejected the inspection of ${host}. Nothing was opened.`
    default:
      return undefined
  }
}
