import { AGENT_IPC_CHANNELS, type AgentResult, type AgentRuntimeView } from '../../shared/agent-contracts'
import {
  PREFERENCE_KEYS,
  type AgentPreferenceView,
  type ModelDiagnosticView,
  type PreferenceKey
} from '../../shared/model-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import type { AgentTaskController } from './agent-tasks'
import type { VoiceTaskController } from './voice-task-controller'

/**
 * Fixed agent IPC channels. Each handler takes positional primitives, checks
 * the sender first, validates inside the controller, and returns a closed
 * `AgentResult`. There is no channel that takes a method name or a route.
 */

export interface IpcMainLike {
  handle(channel: string, listener: (event: never, ...args: unknown[]) => unknown): void
}

export interface AgentIpcDependencies {
  ipcMain: IpcMainLike
  assertTrustedSender: (event: never) => void
  controller: AgentTaskController
  voice: Pick<VoiceTaskController, 'handle'>
  runtimeStatus: () => AgentRuntimeView
  restartRuntime: () => Promise<AgentRuntimeView>
  /** Typed requests. Absent in builds without the interpreter. */
  text?: { submit(requestId: unknown, text: unknown): Promise<AgentResult<VoiceTaskOutcome>> }
  memory?: {
    preferences(): Promise<AgentPreferenceView[]>
    forget(key: PreferenceKey): Promise<AgentPreferenceView[]>
  }
  diagnostics?: () => ModelDiagnosticView[]
}

const UNAVAILABLE = { code: 'request_failed', message: 'That is not available in this build.' } as const

export function registerAgentIpc({
  ipcMain, assertTrustedSender, controller, voice, runtimeStatus, restartRuntime, text, memory, diagnostics
}: AgentIpcDependencies): void {
  const handle = (channel: string, listener: (...args: unknown[]) => unknown): void => {
    ipcMain.handle(channel, (event, ...args) => {
      assertTrustedSender(event)
      return listener(...args)
    })
  }

  handle(AGENT_IPC_CHANNELS.getRuntimeStatus, () => runtimeStatus())
  handle(AGENT_IPC_CHANNELS.restartRuntime, async (): Promise<AgentResult<AgentRuntimeView>> => {
    try {
      return { ok: true, value: await restartRuntime() }
    } catch {
      return { ok: false, error: { code: 'runtime_unavailable', message: 'The Lumi agent runtime could not be started.' } }
    }
  })
  handle(AGENT_IPC_CHANNELS.loadActiveTask, (afterSequence) => controller.loadActiveTask(afterSequence))
  handle(AGENT_IPC_CHANNELS.createBookingTask, (criteria) => controller.createBookingTask(criteria))
  handle(AGENT_IPC_CHANNELS.closeActiveTask, () => controller.closeActiveTask())
  handle(AGENT_IPC_CHANNELS.searchAppointments, () => controller.searchAppointments())
  handle(AGENT_IPC_CHANNELS.prepareBooking, (slotId) => controller.prepareBooking(slotId))
  handle(AGENT_IPC_CHANNELS.requestApproval, (actionId, revision) => controller.requestApproval(actionId, revision))
  handle(AGENT_IPC_CHANNELS.approveAction, (actionId, revision) => controller.approveAction(actionId, revision))
  handle(AGENT_IPC_CHANNELS.rejectAction, (actionId, revision) => controller.rejectAction(actionId, revision))
  handle(AGENT_IPC_CHANNELS.executeAction, (actionId, revision) => controller.executeAction(actionId, revision))
  handle(AGENT_IPC_CHANNELS.reconcileAction, (actionId, revision) => controller.reconcileAction(actionId, revision))
  // One closed command object, parsed field by field in main. It has no
  // approve or execute variant; see voice-task-controller.ts.
  handle(AGENT_IPC_CHANNELS.voiceCommand, (command) => voice.handle(command))
  // A typed request is interpreted in main into the same closed commands.
  handle(AGENT_IPC_CHANNELS.submitTextRequest, (requestId, request) =>
    text ? text.submit(requestId, request) : { ok: false, error: UNAVAILABLE })
  handle(AGENT_IPC_CHANNELS.lookupClinicInfo, () => controller.lookupClinicInfo())
  // Page inspection: positional primitives, validated inside the controller.
  // Approval and execution are separate channels, each bound to the revision
  // on screen; no channel accepts a proposal, digest, selector or script.
  handle(AGENT_IPC_CHANNELS.createPageInspection, (url, question) => controller.createPageInspection(url, question))
  handle(AGENT_IPC_CHANNELS.approveInspection, (actionId, revision) => controller.approveInspection(actionId, revision))
  handle(AGENT_IPC_CHANNELS.rejectInspection, (actionId, revision) => controller.rejectInspection(actionId, revision))
  handle(AGENT_IPC_CHANNELS.executeInspection, (actionId, revision) => controller.executeInspection(actionId, revision))
  handle(AGENT_IPC_CHANNELS.answerInspection, (actionId) => controller.answerInspection(actionId))
  handle(AGENT_IPC_CHANNELS.inspectPageAgain, () => controller.inspectPageAgain())
  handle(AGENT_IPC_CHANNELS.listPreferences, async (): Promise<AgentResult<AgentPreferenceView[]>> => {
    if (!memory) return { ok: true, value: [] }
    try {
      return { ok: true, value: await memory.preferences() }
    } catch {
      return { ok: false, error: { code: 'request_failed', message: 'Lumi could not read its saved preferences.' } }
    }
  })
  handle(AGENT_IPC_CHANNELS.forgetPreference, async (key): Promise<AgentResult<AgentPreferenceView[]>> => {
    if (typeof key !== 'string' || !(PREFERENCE_KEYS as readonly string[]).includes(key)) {
      return { ok: false, error: { code: 'invalid_request', message: 'That preference is invalid.' } }
    }
    if (!memory) return { ok: true, value: [] }
    try {
      return { ok: true, value: await memory.forget(key as PreferenceKey) }
    } catch {
      return { ok: false, error: { code: 'request_failed', message: 'Lumi could not update its saved preferences.' } }
    }
  })
  handle(AGENT_IPC_CHANNELS.getDiagnostics, (): AgentResult<ModelDiagnosticView[]> => ({ ok: true, value: diagnostics ? diagnostics() : [] }))
}
