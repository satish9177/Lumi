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
import type { DesktopReadController } from './desktop-read-controller'
import type { DesktopActionController } from './desktop-action-controller'
import type { DesktopPlanningController } from './desktop-planning-controller'
import type { DesktopVisionController } from './desktop-vision-controller'
import type { DocumentController } from './document-controller'
import type { TransferController } from './transfer-controller'
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
  /** Milestone 9 S2. Absent in builds without the desktop capability. */
  desktopRead?: Pick<
    DesktopReadController,
    'listDesktopSurfaces' | 'createDesktopRead' | 'getDesktopRead' | 'grantDesktopDisclosure' | 'declineDesktopDisclosure' | 'runDesktopRead'
  >
  /** Milestone 9 S3/S4. Absent in builds without the desktop capability. */
  desktopActions?: Pick<
    DesktopActionController,
    | 'listDesktopApps' | 'findDesktopScrollTargets' | 'proposeDesktopFocus' | 'proposeDesktopScroll'
    | 'proposeDesktopLaunch' | 'getDesktopAction' | 'approveDesktopAction' | 'declineDesktopAction'
    | 'proposeDesktopActionFromPlan' | 'reconcileDesktopAction'
  >
  /** Milestone 9 S4. Absent in builds without the desktop capability. */
  desktopPlanning?: Pick<
    DesktopPlanningController,
    'createDesktopPlan' | 'getDesktopPlan' | 'grantDesktopPlan' | 'declineDesktopPlan' | 'runDesktopPlan'
  >
  /** Milestone 9 S5. Absent in builds without the desktop capability. */
  desktopVision?: Pick<
    DesktopVisionController,
    | 'createDesktopCapture' | 'getDesktopCapture' | 'grantDesktopCapture' | 'declineDesktopCapture' | 'runDesktopCapture'
    | 'createDesktopVisionDisclosure' | 'grantDesktopVisionDisclosure' | 'declineDesktopVisionDisclosure' | 'runDesktopVisionDisclosure'
  >
  /** Milestone 10 S1. Absent in builds without the document capability. */
  documents?: Pick<
    DocumentController,
    | 'listFileRoots' | 'addFileRoot' | 'revokeFileRoot' | 'listFileRootFiles' | 'createDocumentTask' | 'getDocumentTask'
    | 'addDocumentFromRoot' | 'addDroppedDocument' | 'extractDocument' | 'compareDocumentsLocally'
    | 'createDocumentDisclosure' | 'grantDocumentDisclosure' | 'declineDocumentDisclosure' | 'runDocumentDisclosure'
  >
  /** Milestone 10 S2. Absent in builds without the download capability. */
  transfers?: Pick<
    TransferController,
    | 'createTransfer' | 'getTransfer' | 'getLatestTransfer' | 'grantTransfer' | 'declineTransfer'
    | 'downloadTransfer' | 'placeTransfer' | 'reconcileTransfer'
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
  ipcMain, assertTrustedSender, controller, voice, runtimeStatus, restartRuntime, text, memory, diagnostics,
  browserProfiles, desktopRead, desktopActions, desktopPlanning, desktopVision, documents, transfers
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
  // Milestone 8b S5: form planning and the exact disclosure approval. Every
  // handler takes closed ids and revisions only -- no manifest, value, origin,
  // field or provider -- and each checks the sender like every channel above.
  handle(AGENT_IPC_CHANNELS.prepareFormPlanning, (refs) => controller.prepareFormPlanning(refs))
  handle(AGENT_IPC_CHANNELS.grantFormPlanning, (grantId, revision) => controller.grantFormPlanning(grantId, revision))
  handle(AGENT_IPC_CHANNELS.declineFormPlanning, (grantId, revision) => controller.declineFormPlanning(grantId, revision))
  handle(AGENT_IPC_CHANNELS.runFormPlanning, () => controller.runFormPlanning())
  handle(AGENT_IPC_CHANNELS.approveFieldDisclosure, (actionId, revision) => controller.approveFieldDisclosure(actionId, revision))
  handle(AGENT_IPC_CHANNELS.rejectFieldDisclosure, (actionId, revision) => controller.rejectFieldDisclosure(actionId, revision))
  // Milestone 8b S6: the network-frozen local draft. Ids and revisions only; the sender is checked
  // on each, and none of them exists on the voice backend.
  handle(AGENT_IPC_CHANNELS.startFormPreparationMode, () => controller.startFormPreparationMode())
  handle(AGENT_IPC_CHANNELS.stopFormPreparation, () => controller.stopFormPreparation())
  handle(AGENT_IPC_CHANNELS.discardFormDraft, (draftId, revision) => controller.discardFormDraft(draftId, revision))
  handle(AGENT_IPC_CHANNELS.prepareFormHandover, (draftId, revision) => controller.prepareFormHandover(draftId, revision))
  handle(AGENT_IPC_CHANNELS.approveFormHandover, (actionId, revision) => controller.approveFormHandover(actionId, revision))
  handle(AGENT_IPC_CHANNELS.rejectFormHandover, (actionId, revision) => controller.rejectFormHandover(actionId, revision))
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
  // Milestone 9 S2: exact desktop disclosure. Six fixed channels, each checking the sender first.
  // `createDesktopRead` takes a typed question and an opaque surface identity, and nothing else: the
  // provider is chosen in main. Approval names a grant and the revision shown. None of these is
  // reachable from voice (`DesktopReadController` is not a member of `VoiceTaskBackend`'s pick), and
  // none can focus, invoke, type into, select, scroll, click or launch anything.
  handle(AGENT_IPC_CHANNELS.listDesktopSurfaces, () =>
    desktopRead ? desktopRead.listDesktopSurfaces() : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.createDesktopRead, (objective, workerGeneration, surfaceRef, surfaceEpoch) =>
    desktopRead ? desktopRead.createDesktopRead(objective, workerGeneration, surfaceRef, surfaceEpoch) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.getDesktopRead, () =>
    desktopRead ? desktopRead.getDesktopRead() : Promise.resolve({ ok: true, value: null }))
  handle(AGENT_IPC_CHANNELS.grantDesktopDisclosure, (grantId, revision) =>
    desktopRead ? desktopRead.grantDesktopDisclosure(grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.declineDesktopDisclosure, (grantId, revision) =>
    desktopRead ? desktopRead.declineDesktopDisclosure(grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.runDesktopRead, () =>
    desktopRead ? desktopRead.runDesktopRead() : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  // Milestone 9 S3: trusted focus, semantic scroll and registered-app launch. Eight fixed channels, each
  // checking the sender first. Not reachable from voice (`DesktopActionController` is not a member of the
  // voice backend). None takes a handle, a process, a path, an argument, a coordinate, a key or a selector.
  handle(AGENT_IPC_CHANNELS.listDesktopApps, () =>
    desktopActions ? desktopActions.listDesktopApps() : Promise.resolve({ ok: true, value: [] }))
  handle(AGENT_IPC_CHANNELS.findDesktopScrollTargets, (workerGeneration, surfaceRef, surfaceEpoch) =>
    desktopActions ? desktopActions.findDesktopScrollTargets(workerGeneration, surfaceRef, surfaceEpoch) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.proposeDesktopFocus, (workerGeneration, surfaceRef, surfaceEpoch) =>
    desktopActions ? desktopActions.proposeDesktopFocus(workerGeneration, surfaceRef, surfaceEpoch) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.proposeDesktopScroll, (workerGeneration, observationId, controlRef, step) =>
    desktopActions ? desktopActions.proposeDesktopScroll(workerGeneration, observationId, controlRef, step) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.proposeDesktopLaunch, (appId) =>
    desktopActions ? desktopActions.proposeDesktopLaunch(appId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.getDesktopAction, () =>
    desktopActions ? desktopActions.getDesktopAction() : Promise.resolve({ ok: true, value: null }))
  handle(AGENT_IPC_CHANNELS.approveDesktopAction, (actionId, revision) =>
    desktopActions ? desktopActions.approveDesktopAction(actionId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.declineDesktopAction, (actionId, revision) =>
    desktopActions ? desktopActions.declineDesktopAction(actionId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.reconcileDesktopAction, (actionId, revision, outcome) =>
    desktopActions ? desktopActions.reconcileDesktopAction(actionId, revision, outcome) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.proposeDesktopActionFromPlan, (planId) =>
    desktopActions ? desktopActions.proposeDesktopActionFromPlan(planId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  // Milestone 9 S4: bounded desktop-action planning. Disclosure authority only -- none of these six
  // channels performs any desktop action. Not reachable from voice.
  handle(AGENT_IPC_CHANNELS.createDesktopPlan, (objective, workerGeneration, surfaceRef, surfaceEpoch, values) =>
    desktopPlanning ? desktopPlanning.createDesktopPlan(objective, workerGeneration, surfaceRef, surfaceEpoch, values) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.getDesktopPlan, () =>
    desktopPlanning ? desktopPlanning.getDesktopPlan() : Promise.resolve({ ok: true, value: null }))
  handle(AGENT_IPC_CHANNELS.grantDesktopPlan, (grantId, revision) =>
    desktopPlanning ? desktopPlanning.grantDesktopPlan(grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.declineDesktopPlan, (grantId, revision) =>
    desktopPlanning ? desktopPlanning.declineDesktopPlan(grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.runDesktopPlan, () =>
    desktopPlanning ? desktopPlanning.runDesktopPlan() : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  // Milestone 9 S5: scoped desktop visual fallback. Nine exact channels. A capture requires its own
  // approval before a single pixel is taken; a vision-provider disclosure requires a SEPARATE
  // approval. Not reachable from voice.
  handle(AGENT_IPC_CHANNELS.createDesktopCapture, (objective, workerGeneration, surfaceRef, surfaceEpoch, targetHint) =>
    desktopVision ? desktopVision.createDesktopCapture(objective, workerGeneration, surfaceRef, surfaceEpoch, targetHint) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.getDesktopCapture, (taskId) =>
    desktopVision ? desktopVision.getDesktopCapture(taskId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.grantDesktopCapture, (taskId, grantId, revision) =>
    desktopVision ? desktopVision.grantDesktopCapture(taskId, grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.declineDesktopCapture, (taskId, grantId, revision) =>
    desktopVision ? desktopVision.declineDesktopCapture(taskId, grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.runDesktopCapture, (taskId) =>
    desktopVision ? desktopVision.runDesktopCapture(taskId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.createDesktopVisionDisclosure, (taskId, purpose) =>
    desktopVision ? desktopVision.createDesktopVisionDisclosure(taskId, purpose) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.grantDesktopVisionDisclosure, (taskId, grantId, revision) =>
    desktopVision ? desktopVision.grantDesktopVisionDisclosure(taskId, grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.declineDesktopVisionDisclosure, (taskId, grantId, revision) =>
    desktopVision ? desktopVision.declineDesktopVisionDisclosure(taskId, grantId, revision) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  handle(AGENT_IPC_CHANNELS.runDesktopVisionDisclosure, (taskId) =>
    desktopVision ? desktopVision.runDesktopVisionDisclosure(taskId) : Promise.resolve({ ok: false, error: UNAVAILABLE }))
  // Milestone 10 S1: M10 file roots and approved documents. Fourteen fixed channels, each checking the
  // sender first. None takes a path: a folder comes from a native dialog in main, a dropped file by its
  // opaque id, a root file by a root id plus the root-relative name the listing showed. Not reachable
  // from voice (`DocumentController` is not a member of the voice backend).
  const noDocuments = (): Promise<{ ok: false; error: typeof UNAVAILABLE }> => Promise.resolve({ ok: false, error: UNAVAILABLE })
  handle(AGENT_IPC_CHANNELS.listFileRoots, () => documents ? documents.listFileRoots() : Promise.resolve({ ok: true, value: [] }))
  handle(AGENT_IPC_CHANNELS.addFileRoot, (label, canRead, canCreate) => documents ? documents.addFileRoot(label, canRead, canCreate) : noDocuments())
  handle(AGENT_IPC_CHANNELS.revokeFileRoot, (rootId, revision) => documents ? documents.revokeFileRoot(rootId, revision) : noDocuments())
  handle(AGENT_IPC_CHANNELS.listFileRootFiles, (rootId) => documents ? documents.listFileRootFiles(rootId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.createDocumentTask, (objective) => documents ? documents.createDocumentTask(objective) : noDocuments())
  handle(AGENT_IPC_CHANNELS.getDocumentTask, (taskId) => documents ? documents.getDocumentTask(taskId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.addDocumentFromRoot, (taskId, rootId, relativePath) =>
    documents ? documents.addDocumentFromRoot(taskId, rootId, relativePath) : noDocuments())
  handle(AGENT_IPC_CHANNELS.addDroppedDocument, (taskId, droppedId) => documents ? documents.addDroppedDocument(taskId, droppedId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.extractDocument, (taskId, fileId) => documents ? documents.extractDocument(taskId, fileId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.compareDocumentsLocally, (taskId, first, second) =>
    documents ? documents.compareDocumentsLocally(taskId, first, second) : noDocuments())
  handle(AGENT_IPC_CHANNELS.createDocumentDisclosure, (taskId, documentIds, purpose) =>
    documents ? documents.createDocumentDisclosure(taskId, documentIds, purpose) : noDocuments())
  handle(AGENT_IPC_CHANNELS.grantDocumentDisclosure, (taskId, grantId, revision) =>
    documents ? documents.grantDocumentDisclosure(taskId, grantId, revision) : noDocuments())
  handle(AGENT_IPC_CHANNELS.declineDocumentDisclosure, (taskId, grantId, revision) =>
    documents ? documents.declineDocumentDisclosure(taskId, grantId, revision) : noDocuments())
  handle(AGENT_IPC_CHANNELS.runDocumentDisclosure, (taskId) => documents ? documents.runDocumentDisclosure(taskId) : noDocuments())
  // Milestone 10 S2: eight fixed channels for one controlled download. None takes a path or an overwrite
  // flag; the approval is re-confirmed by a native dialog in main. Not reachable from voice.
  handle(AGENT_IPC_CHANNELS.createTransfer, (url, rootId, fileName, intent) =>
    transfers ? transfers.createTransfer(url, rootId, fileName, intent) : noDocuments())
  handle(AGENT_IPC_CHANNELS.getTransfer, (taskId) => transfers ? transfers.getTransfer(taskId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.getLatestTransfer, () => transfers ? transfers.getLatestTransfer() : Promise.resolve({ ok: true, value: null }))
  handle(AGENT_IPC_CHANNELS.grantTransfer, (taskId, grantId, revision) =>
    transfers ? transfers.grantTransfer(taskId, grantId, revision) : noDocuments())
  handle(AGENT_IPC_CHANNELS.declineTransfer, (taskId, grantId, revision) =>
    transfers ? transfers.declineTransfer(taskId, grantId, revision) : noDocuments())
  handle(AGENT_IPC_CHANNELS.downloadTransfer, (taskId) => transfers ? transfers.downloadTransfer(taskId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.placeTransfer, (taskId) => transfers ? transfers.placeTransfer(taskId) : noDocuments())
  handle(AGENT_IPC_CHANNELS.reconcileTransfer, (taskId) => transfers ? transfers.reconcileTransfer(taskId) : noDocuments())
}
