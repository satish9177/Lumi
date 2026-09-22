import { contextBridge, ipcRenderer, webUtils } from 'electron'
import {
  IPC_CHANNELS,
  type FileSearchRequest,
  type LifeLensApi,
  type PendingSearchResolution,
  type TelegramStatus,
  type ToolProposal
} from '../shared/contracts'
import type { GuardedTool } from '../shared/intent'
import { AGENT_IPC_CHANNELS, type AgentApi, type AgentBookingCriteria, type AgentProtectedDataKind, type AgentRuntimeView } from '../shared/agent-contracts'
import type { VoiceTaskCommand } from '../shared/voice-task-contracts'
import type { PreferenceKey } from '../shared/model-contracts'
import { VOICE_RELAY_CHANNELS, type VoiceRelayApi, type VoiceRelayServerEvent } from '../shared/voice-relay-contracts'

// Fixed channels and positional primitives only. Main validates everything;
// nothing here can name a runtime route, a URL, or a booked value.
const agentApi: AgentApi = {
  getRuntimeStatus: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getRuntimeStatus),
  onRuntimeStatus: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, status: AgentRuntimeView) => listener(status)
    ipcRenderer.on(AGENT_IPC_CHANNELS.runtimeStatusChanged, handler)
    return () => ipcRenderer.removeListener(AGENT_IPC_CHANNELS.runtimeStatusChanged, handler)
  },
  restartRuntime: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.restartRuntime),
  loadActiveTask: (afterSequence: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.loadActiveTask, afterSequence),
  createBookingTask: (criteria: AgentBookingCriteria) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createBookingTask, { specialty: criteria.specialty, day: criteria.day }),
  closeActiveTask: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.closeActiveTask),
  searchAppointments: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.searchAppointments),
  prepareBooking: (slotId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.prepareBooking, slotId),
  requestApproval: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.requestApproval, actionId, expectedRevision),
  approveAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.approveAction, actionId, expectedRevision),
  rejectAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.rejectAction, actionId, expectedRevision),
  executeAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.executeAction, actionId, expectedRevision),
  reconcileAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.reconcileAction, actionId, expectedRevision),
  // A structured-clone copy of one closed command; main re-validates it.
  voiceCommand: (command: VoiceTaskCommand) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.voiceCommand, command),
  // Text only; main interprets it and can never approve or execute from it.
  submitTextRequest: (requestId: string, text: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.submitTextRequest, requestId, text),
  // The main composer's text; main alone decides which path owns it.
  routeTypedRequest: (requestId: string, text: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.routeTypedRequest, requestId, text),
  lookupClinicInfo: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.lookupClinicInfo),
  // Two strings the user typed; main validates the URL against its destination
  // policy and the runtime validates it again. Nothing here can approve.
  createPageInspection: (url: string, question: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createPageInspection, url, question),
  approveInspection: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.approveInspection, actionId, expectedRevision),
  rejectInspection: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.rejectInspection, actionId, expectedRevision),
  executeInspection: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.executeInspection, actionId, expectedRevision),
  answerInspection: (actionId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.answerInspection, actionId),
  inspectPageAgain: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.inspectPageAgain),
  // Public research. One string the user typed, then two id/revision pairs
  // from the card that was on screen. `grantResearchScope` is the trusted
  // click and the only route to an active scope; nothing here can widen one,
  // and no channel accepts a step, a selector, a script or an address.
  createResearchTask: (objective: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createResearchTask, objective),
  grantResearchScope: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantResearchScope, grantId, expectedRevision),
  declineResearchScope: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineResearchScope, grantId, expectedRevision),
  runResearch: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runResearch),
  stopResearch: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.stopResearch),
  // Authenticated account reading. The user's question, an opaque profile id
  // and one provider id main itself offered; then an id/revision pair from the
  // card on screen. `grantAuthenticatedScope` is the trusted click and the only
  // route to an active scope. No channel accepts a URL, a selector, a cookie, a
  // profile path, page content, a step, a scope or a free-form provider name.
  getAuthenticatedOptions: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getAuthenticatedOptions),
  createAuthenticatedTask: (objective: string, profileId: string, recipientId: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createAuthenticatedTask, objective, profileId, recipientId),
  grantAuthenticatedScope: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantAuthenticatedScope, grantId, expectedRevision),
  declineAuthenticatedScope: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineAuthenticatedScope, grantId, expectedRevision),
  runAuthenticated: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runAuthenticated),
  stopAuthenticated: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.stopAuthenticated),
  // Milestone 8b S5: closed ids and revisions only. No manifest, value, origin, field or provider.
  prepareFormPlanning: (allowedDataRefs: AgentProtectedDataKind[]) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.prepareFormPlanning, allowedDataRefs),
  grantFormPlanning: (grantId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantFormPlanning, grantId, expectedRevision),
  declineFormPlanning: (grantId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineFormPlanning, grantId, expectedRevision),
  runFormPlanning: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runFormPlanning),
  approveFieldDisclosure: (actionId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.approveFieldDisclosure, actionId, expectedRevision),
  rejectFieldDisclosure: (actionId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.rejectFieldDisclosure, actionId, expectedRevision),
  startFormPreparationMode: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.startFormPreparationMode),
  stopFormPreparation: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.stopFormPreparation),
  discardFormDraft: (draftId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.discardFormDraft, draftId, expectedRevision),
  prepareFormHandover: (draftId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.prepareFormHandover, draftId, expectedRevision),
  approveFormHandover: (actionId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.approveFormHandover, actionId, expectedRevision),
  rejectFormHandover: (actionId: string, expectedRevision: number) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.rejectFormHandover, actionId, expectedRevision),
  listPreferences: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.listPreferences),
  forgetPreference: (key: PreferenceKey) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.forgetPreference, key),
  getDiagnostics: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getDiagnostics),
  // Milestone 8a S2: manual login and human takeover. No hostname, path,
  // credential or browser argument crosses here -- only ids and the
  // revision a trusted card already showed. `listBrowserProfiles` takes no
  // argument at all.
  listBrowserProfiles: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.listBrowserProfiles),
  openLoginWindow: (profileId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.openLoginWindow, profileId, expectedRevision),
  confirmSignedIn: (profileId: string, attemptId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.confirmSignedIn, profileId, attemptId, expectedRevision),
  cancelLogin: (profileId: string, attemptId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.cancelLogin, profileId, attemptId, expectedRevision),
  getLoginTakeover: (profileId: string, attemptId: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.getLoginTakeover, profileId, attemptId),
  // Milestone 9 S2: exact desktop disclosure. Six exact methods. The question is typed locally and
  // stays out of the conversation model; the provider is never a parameter; approval names only a
  // grant id and the revision shown. There is no function that focuses, invokes, types into, selects,
  // scrolls, clicks or launches anything.
  listDesktopSurfaces: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.listDesktopSurfaces),
  createDesktopRead: (objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createDesktopRead, objective, workerGeneration, surfaceRef, surfaceEpoch),
  getDesktopRead: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getDesktopRead),
  grantDesktopDisclosure: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantDesktopDisclosure, grantId, expectedRevision),
  declineDesktopDisclosure: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineDesktopDisclosure, grantId, expectedRevision),
  runDesktopRead: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runDesktopRead),
  // Milestone 9 S3: trusted focus, semantic scroll and registered-app launch. Eight exact methods. None
  // takes a handle, a process, a path, an argument, a coordinate, a key or a selector: a surface identity
  // the renderer was listed, a control of a fresh local observation, a closed scroll step, a registered
  // app id, or the action id and revision the trusted card showed.
  listDesktopApps: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.listDesktopApps),
  findDesktopScrollTargets: (workerGeneration: string, surfaceRef: string, surfaceEpoch: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.findDesktopScrollTargets, workerGeneration, surfaceRef, surfaceEpoch),
  proposeDesktopFocus: (workerGeneration: string, surfaceRef: string, surfaceEpoch: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.proposeDesktopFocus, workerGeneration, surfaceRef, surfaceEpoch),
  proposeDesktopScroll: (workerGeneration: string, observationId: string, controlRef: string, step: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.proposeDesktopScroll, workerGeneration, observationId, controlRef, step),
  proposeDesktopLaunch: (appId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.proposeDesktopLaunch, appId),
  getDesktopAction: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getDesktopAction),
  approveDesktopAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.approveDesktopAction, actionId, expectedRevision),
  declineDesktopAction: (actionId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineDesktopAction, actionId, expectedRevision),
  reconcileDesktopAction: (actionId: string, expectedRevision: number, outcome: 'succeeded' | 'failed' | 'still_unknown') =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.reconcileDesktopAction, actionId, expectedRevision, outcome),
  // Milestone 9 S4: bounded desktop-action planning. Six exact methods (plus the execution-side
  // `proposeDesktopActionFromPlan` above). Disclosure authority only: none of these performs a
  // desktop action. The renderer supplies an objective, a surface identity and its own typed
  // candidate values; the provider is never a parameter.
  createDesktopPlan: (
    objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number,
    values: Array<{ classification: string; value: string }>
  ) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.createDesktopPlan, objective, workerGeneration, surfaceRef, surfaceEpoch, values),
  getDesktopPlan: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getDesktopPlan),
  grantDesktopPlan: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantDesktopPlan, grantId, expectedRevision),
  declineDesktopPlan: (grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineDesktopPlan, grantId, expectedRevision),
  runDesktopPlan: () => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runDesktopPlan),
  proposeDesktopActionFromPlan: (planId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.proposeDesktopActionFromPlan, planId),
  // Milestone 9 S5: scoped desktop visual fallback. Nine exact methods. A capture requires its own
  // approval before a single pixel is taken; a vision-provider disclosure requires a SEPARATE
  // approval naming the provider, model and purpose. The renderer never supplies an image, a
  // coordinate or a candidate: it only ever reviews and approves what the runtime already narrowed.
  createDesktopCapture: (
    objective: string, workerGeneration: string, surfaceRef: string, surfaceEpoch: number, targetHint?: string
  ) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.createDesktopCapture, objective, workerGeneration, surfaceRef, surfaceEpoch, targetHint),
  getDesktopCapture: (taskId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.getDesktopCapture, taskId),
  grantDesktopCapture: (taskId: string, grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantDesktopCapture, taskId, grantId, expectedRevision),
  declineDesktopCapture: (taskId: string, grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineDesktopCapture, taskId, grantId, expectedRevision),
  runDesktopCapture: (taskId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runDesktopCapture, taskId),
  createDesktopVisionDisclosure: (taskId: string, purpose: string) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.createDesktopVisionDisclosure, taskId, purpose),
  grantDesktopVisionDisclosure: (taskId: string, grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.grantDesktopVisionDisclosure, taskId, grantId, expectedRevision),
  declineDesktopVisionDisclosure: (taskId: string, grantId: string, expectedRevision: number) =>
    ipcRenderer.invoke(AGENT_IPC_CHANNELS.declineDesktopVisionDisclosure, taskId, grantId, expectedRevision),
  runDesktopVisionDisclosure: (taskId: string) => ipcRenderer.invoke(AGENT_IPC_CHANNELS.runDesktopVisionDisclosure, taskId)
}

// The Gemini Live relay: fixed channels, closed message kinds, validated in
// main. No credential, endpoint or model name is ever passed here.
const voiceRelay: VoiceRelayApi = {
  open: () => ipcRenderer.invoke(VOICE_RELAY_CHANNELS.open),
  send: (sessionId, message) => ipcRenderer.invoke(VOICE_RELAY_CHANNELS.send, sessionId, message),
  close: (sessionId) => ipcRenderer.invoke(VOICE_RELAY_CHANNELS.close, sessionId),
  onEvent: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, sessionId: string, event: VoiceRelayServerEvent) => listener(sessionId, event)
    ipcRenderer.on(VOICE_RELAY_CHANNELS.event, handler)
    return () => ipcRenderer.removeListener(VOICE_RELAY_CHANNELS.event, handler)
  }
}

const lifeLensApi: LifeLensApi = {
  agent: agentApi,
  voiceRelay,
  listCaptureSources: () => ipcRenderer.invoke(IPC_CHANNELS.listCaptureSources),
  captureScreen: (sourceId?: string) => ipcRenderer.invoke(IPC_CHANNELS.captureScreen, sourceId),
  analyzeCapture: (captureId: string) => ipcRenderer.invoke(IPC_CHANNELS.analyzeCapture, captureId),
  checkCaptureForScam: (captureId: string) => ipcRenderer.invoke(IPC_CHANNELS.checkCaptureForScam, captureId),
  discardCapture: () => ipcRenderer.invoke(IPC_CHANNELS.discardCapture),
  createRealtimeSession: () => ipcRenderer.invoke(IPC_CHANNELS.createRealtimeSession),
  noteUserRequest: (request: string) => ipcRenderer.invoke(IPC_CHANNELS.noteUserRequest, request),
  evaluateToolRequest: (toolName: GuardedTool) => ipcRenderer.invoke(IPC_CHANNELS.evaluateToolRequest, toolName),
  createPendingAction: (proposal: ToolProposal) => ipcRenderer.invoke(IPC_CHANNELS.createPendingAction, proposal),
  approvePendingAction: (approvalId: string) => ipcRenderer.invoke(IPC_CHANNELS.approvePendingAction, approvalId),
  cancelPendingAction: (approvalId: string) => ipcRenderer.invoke(IPC_CHANNELS.cancelPendingAction, approvalId),
  chooseDocumentRoot: () => ipcRenderer.invoke(IPC_CHANNELS.chooseDocumentRoot),
  listDocumentRoots: () => ipcRenderer.invoke(IPC_CHANNELS.listDocumentRoots),
  removeDocumentRoot: (rootId: string) => ipcRenderer.invoke(IPC_CHANNELS.removeDocumentRoot, rootId),
  beginFileSearch: (request: FileSearchRequest) => ipcRenderer.invoke(IPC_CHANNELS.beginFileSearch, request),
  cancelFileSearch: () => ipcRenderer.invoke(IPC_CHANNELS.cancelFileSearch),
  getResultThumbnails: (resultIds: string[]) => ipcRenderer.invoke(IPC_CHANNELS.getResultThumbnails, resultIds),
  cancelPhotoAnalysis: () => ipcRenderer.invoke(IPC_CHANNELS.cancelPhotoAnalysis),
  getPhotoSearchStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getPhotoSearchStatus),
  enablePhotoSearch: () => ipcRenderer.invoke(IPC_CHANNELS.enablePhotoSearch),
  downloadPhotoSearchModel: () => ipcRenderer.invoke(IPC_CHANNELS.downloadPhotoSearchModel),
  cancelPhotoSearchDownload: () => ipcRenderer.invoke(IPC_CHANNELS.cancelPhotoSearchDownload),
  pausePhotoIndex: () => ipcRenderer.invoke(IPC_CHANNELS.pausePhotoIndex),
  resumePhotoIndex: () => ipcRenderer.invoke(IPC_CHANNELS.resumePhotoIndex),
  rebuildPhotoIndex: () => ipcRenderer.invoke(IPC_CHANNELS.rebuildPhotoIndex),
  setPhotoTextSearchEnabled: (enabled: boolean) => ipcRenderer.invoke(IPC_CHANNELS.setPhotoTextSearchEnabled, enabled),
  setPhotoFaceCountEnabled: (enabled: boolean) => ipcRenderer.invoke(IPC_CHANNELS.setPhotoFaceCountEnabled, enabled),
  rebuildPhotoTextIndex: () => ipcRenderer.invoke(IPC_CHANNELS.rebuildPhotoTextIndex),
  rebuildPhotoFaceIndex: () => ipcRenderer.invoke(IPC_CHANNELS.rebuildPhotoFaceIndex),
  disablePhotoSearch: () => ipcRenderer.invoke(IPC_CHANNELS.disablePhotoSearch),
  setPhotoIndexOnlyWhilePluggedIn: (enabled: boolean) => ipcRenderer.invoke(IPC_CHANNELS.setPhotoIndexOnlyWhilePluggedIn, enabled),
  setRealtimeActive: (active: boolean) => ipcRenderer.invoke(IPC_CHANNELS.setRealtimeActive, active),
  onPhotoSearchStatusChanged: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, status: Parameters<typeof listener>[0]) => listener(status)
    ipcRenderer.on(IPC_CHANNELS.photoSearchStatusChanged, handler)
    return () => ipcRenderer.removeListener(IPC_CHANNELS.photoSearchStatusChanged, handler)
  },
  onFileSearchResolved: (listener: (resolution: PendingSearchResolution) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, resolution: PendingSearchResolution) => listener(resolution)
    ipcRenderer.on(IPC_CHANNELS.fileSearchResolved, handler)
    return () => ipcRenderer.removeListener(IPC_CHANNELS.fileSearchResolved, handler)
  },
  listReminders: () => ipcRenderer.invoke(IPC_CHANNELS.listReminders),
  getTelegramStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getTelegramStatus),
  connectTelegram: () => ipcRenderer.invoke(IPC_CHANNELS.connectTelegram),
  cancelTelegramConnect: () => ipcRenderer.invoke(IPC_CHANNELS.cancelTelegramConnect),
  submitTelegramPassword: (password: string) => ipcRenderer.invoke(IPC_CHANNELS.submitTelegramPassword, password),
  logoutTelegram: () => ipcRenderer.invoke(IPC_CHANNELS.logoutTelegram),
  searchTelegramRecipients: (query: string) => ipcRenderer.invoke(IPC_CHANNELS.searchTelegramRecipients, query),
  onTelegramAuthUpdate: (listener: (status: TelegramStatus) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, status: TelegramStatus) => listener(status)
    ipcRenderer.on(IPC_CHANNELS.telegramAuthUpdate, handler)
    return () => ipcRenderer.removeListener(IPC_CHANNELS.telegramAuthUpdate, handler)
  },
  setPanelOpen: (open: boolean) => ipcRenderer.send(IPC_CHANNELS.setPanelOpen, open),
  resetWindowPosition: () => ipcRenderer.invoke(IPC_CHANNELS.resetWindowPosition),
  /**
   * The one place a dropped file's path exists outside main.
   *
   * `webUtils.getPathForFile` is called here and the result is forwarded
   * straight to main. It is never returned to the renderer, never stored, and
   * never logged. A drag with no local backing file — an Outlook attachment, a
   * browser image — yields an empty string, which main rejects.
   */
  registerDroppedFile: (file: File) =>
    ipcRenderer.invoke(IPC_CHANNELS.registerDroppedFile, webUtils.getPathForFile(file)),
  removeDroppedFile: (droppedId: string) => ipcRenderer.invoke(IPC_CHANNELS.removeDroppedFile, droppedId),

  // --- Phase 3: labelled people --------------------------------------------
  // Straight pass-throughs. Preload adds no validation of its own, because
  // preload runs in the renderer's process and a check here would be a check
  // an attacker controls. Every payload is parsed in main; see people-ipc.ts.
  getPeopleSearchStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getPeopleSearchStatus),
  setPeopleSearchEnabled: (enabled: boolean) =>
    ipcRenderer.invoke(IPC_CHANNELS.setPeopleSearchEnabled, enabled),
  pausePeopleScan: () => ipcRenderer.invoke(IPC_CHANNELS.pausePeopleScan),
  resumePeopleScan: () => ipcRenderer.invoke(IPC_CHANNELS.resumePeopleScan),
  listPeopleProfiles: () => ipcRenderer.invoke(IPC_CHANNELS.listPeopleProfiles),
  beginPeopleEnrolment: (label: string) => ipcRenderer.invoke(IPC_CHANNELS.beginPeopleEnrolment, label),
  beginPersonReferenceAddition: (profileId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.beginPersonReferenceAddition, profileId),
  addPeopleReference: (enrolmentId: string, trustedId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.addPeopleReference, enrolmentId, trustedId),
  selectPeopleFace: (enrolmentId: string, candidateId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.selectPeopleFace, enrolmentId, candidateId),
  confirmPeopleEnrolment: (enrolmentId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.confirmPeopleEnrolment, enrolmentId),
  cancelPeopleEnrolment: (enrolmentId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.cancelPeopleEnrolment, enrolmentId),
  renamePeopleProfile: (profileId: string, label: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.renamePeopleProfile, profileId, label),
  rescanPeopleProfile: (profileId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.rescanPeopleProfile, profileId),
  deletePeopleProfile: (profileId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.deletePeopleProfile, profileId),
  deleteAllPeopleData: () => ipcRenderer.invoke(IPC_CHANNELS.deleteAllPeopleData),
  onPeopleSearchStatusChanged: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, status: Parameters<typeof listener>[0]) => listener(status)
    ipcRenderer.on(IPC_CHANNELS.peopleSearchStatusChanged, handler)
    return () => ipcRenderer.removeListener(IPC_CHANNELS.peopleSearchStatusChanged, handler)
  }
}

contextBridge.exposeInMainWorld('lifeLens', lifeLensApi)
