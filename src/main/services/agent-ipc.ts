import {
  AGENT_IPC_CHANNELS,
  type AgentBrowserProfileView,
  type AgentLoginAttemptView,
  type AgentLoginTakeoverView,
  type AgentResult,
  type AgentRuntimeView,
  type TypedRequestRoute
} from '../../shared/agent-contracts'
import {
  PREFERENCE_KEYS,
  type AgentPreferenceView,
  type ModelDiagnosticView,
  type PreferenceKey
} from '../../shared/model-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import { extractInspectionRequest } from '../agent/task-request-interpreter'
import type { AgentTaskController } from './agent-tasks'
import type { BrowserProfileController } from './browser-profile-controller'
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
  text?: {
    submit(requestId: unknown, text: unknown): Promise<AgentResult<VoiceTaskOutcome>>
    route(requestId: unknown, text: unknown): Promise<TypedRequestRoute>
  }
  memory?: {
    preferences(): Promise<AgentPreferenceView[]>
    forget(key: PreferenceKey): Promise<AgentPreferenceView[]>
  }
  diagnostics?: () => ModelDiagnosticView[]
  /** Milestone 8a S2. Absent in builds without a browser-profile capability. */
  browserProfiles?: Pick<
    BrowserProfileController,
    'listBrowserProfiles' | 'openLoginWindow' | 'confirmSignedIn' | 'cancelLogin' | 'getLoginTakeover'
  >
}

const UNAVAILABLE = { code: 'request_failed', message: 'That is not available in this build.' } as const
const INSPECTION_UNAVAILABLE = {
  code: 'inspection_unavailable', message: 'Page inspection needs a configured text model. Nothing was opened.'
} as const

/**
 * Without an interpreter no durable capability can take a request, but a
 * page-inspection request is still claimed: it must not reach a conversation
 * tool that would open the address instead.
 */
function routeWithoutInterpreter(request: unknown): TypedRequestRoute {
  const text = typeof request === 'string' ? request.replace(/\s+/g, ' ').trim() : ''
  return extractInspectionRequest(text) ? { handled: true, result: { ok: false, error: INSPECTION_UNAVAILABLE } } : { handled: false }
}

export function registerAgentIpc({
  ipcMain, assertTrustedSender, controller, voice, runtimeStatus, restartRuntime, text, memory, diagnostics, browserProfiles
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
  // The main composer asks here first; only `handled: false` may go on to the
  // realtime conversation. The interpreter never rejects; if it somehow does,
  // the request is refused rather than passed on.
  handle(AGENT_IPC_CHANNELS.routeTypedRequest, async (requestId, request): Promise<TypedRequestRoute> => {
    if (!text) return routeWithoutInterpreter(request)
    try {
      return await text.route(requestId, request)
    } catch {
      return { handled: true, result: { ok: false, error: { code: 'request_failed', message: 'Lumi could not handle that request. Nothing was done.' } } }
    }
  })
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
  // Public research. `grantResearchScope` is the trusted click: it names the
  // grant on screen and the revision that was shown, and it is the only
  // channel that can make research possible. There is deliberately no channel
  // that submits a step, so neither the renderer nor a model can choose one.
  handle(AGENT_IPC_CHANNELS.createResearchTask, (objective) => controller.createResearchTask(objective))
  handle(AGENT_IPC_CHANNELS.grantResearchScope, (grantId, revision) => controller.grantResearchScope(grantId, revision))
  handle(AGENT_IPC_CHANNELS.declineResearchScope, (grantId, revision) => controller.declineResearchScope(grantId, revision))
  handle(AGENT_IPC_CHANNELS.runResearch, () => controller.runResearch())
  handle(AGENT_IPC_CHANNELS.stopResearch, () => controller.stopResearch())
  // Authenticated account reading. `grantAuthenticatedScope` is the trusted
  // click: it names the grant on screen and the revision that was shown, and it
  // is the only channel that can make account reading possible. There is
  // deliberately no channel that submits a step, a scope, a URL or a provider
  // name, and none of these six is reachable from voice: `VoiceTaskBackend` is
  // a narrow pick that does not include them (see voice-task-controller.ts).
  handle(AGENT_IPC_CHANNELS.getAuthenticatedOptions, () => controller.getAuthenticatedOptions())
  handle(AGENT_IPC_CHANNELS.createAuthenticatedTask, (objective, profileId, recipientId) =>
    controller.createAuthenticatedTask(objective, profileId, recipientId))
  handle(AGENT_IPC_CHANNELS.grantAuthenticatedScope, (grantId, revision) => controller.grantAuthenticatedScope(grantId, revision))
  handle(AGENT_IPC_CHANNELS.declineAuthenticatedScope, (grantId, revision) => controller.declineAuthenticatedScope(grantId, revision))
  handle(AGENT_IPC_CHANNELS.runAuthenticated, () => controller.runAuthenticated())
  handle(AGENT_IPC_CHANNELS.stopAuthenticated, () => controller.stopAuthenticated())
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
  // Milestone 8a S2: manual login and human takeover. `listBrowserProfiles`
  // takes no argument; the three mutations take only ids and the revision
  // shown on screen. None of these five channels is reachable from voice --
  // `VoiceTaskBackend` only ever picks from `AgentTaskController`, and
  // `BrowserProfileController` is not that class.
  handle(AGENT_IPC_CHANNELS.listBrowserProfiles, (): Promise<AgentResult<AgentBrowserProfileView[]>> =>
    browserProfiles ? browserProfiles.listBrowserProfiles() : Promise.resolve({ ok: true, value: [] }))
  handle(AGENT_IPC_CHANNELS.openLoginWindow, (profileId, expectedRevision): Promise<AgentResult<AgentLoginTakeoverView>> =>
    browserProfiles ? browserProfiles.openLoginWindow(profileId, expectedRevision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.confirmSignedIn, (profileId, attemptId, expectedRevision): Promise<AgentResult<AgentLoginTakeoverView>> =>
    browserProfiles ? browserProfiles.confirmSignedIn(profileId, attemptId, expectedRevision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.cancelLogin, (profileId, attemptId, expectedRevision): Promise<AgentResult<AgentLoginTakeoverView>> =>
    browserProfiles ? browserProfiles.cancelLogin(profileId, attemptId, expectedRevision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.getLoginTakeover, (profileId, attemptId): Promise<AgentResult<AgentLoginAttemptView>> =>
    browserProfiles ? browserProfiles.getLoginTakeover(profileId, attemptId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
}
