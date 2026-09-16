import { AGENT_IPC_CHANNELS, type AgentResult, type AgentRuntimeView } from '../../shared/agent-contracts'
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
}

export function registerAgentIpc({ ipcMain, assertTrustedSender, controller, voice, runtimeStatus, restartRuntime }: AgentIpcDependencies): void {
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
}
