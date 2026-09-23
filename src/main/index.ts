// Must remain the first import: Realtime and reasoning read configuration at
// module evaluation, so development env files have to load before them.
import './development-env'

import { app, BrowserWindow, dialog, ipcMain, nativeImage, powerMonitor, safeStorage, screen, session, utilityProcess } from 'electron'
import { existsSync } from 'node:fs'
import { stat } from 'node:fs/promises'
import { constants as osPriority, setPriority } from 'node:os'
import { basename, isAbsolute, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  IPC_CHANNELS,
  parseFileSearchRequest,
  type PendingSearchResolution,
  type TelegramStatus,
  type ToolProposal
} from '../shared/contracts'
import { GUARDED_TOOLS, type GuardedTool } from '../shared/intent'
import type { NormalizedSearchQuery } from '../shared/search-query'
import { captureScreen, listCaptureSources } from './services/capture'
import { runDocumentSearch } from './services/document-search'
import { IntentTracker } from './services/intent-policy'
import { createRealtimeSessionCredential, GEMINI_LIVE_MODEL } from './services/realtime'
import { GeminiLiveRelay } from './voice/gemini-live-relay'
import { ScriptedGeminiSocket } from './voice/scripted-gemini-socket'
import { ApplicationDefaultCredentials } from './models/google-auth'
import { vertexEnabled, vertexLocation } from './models/model-config'
import { VOICE_RELAY_CHANNELS } from '../shared/voice-relay-contracts'
import { RetainedCaptureStore } from './services/retained-captures'
import { createScamCheckAssessment } from './services/scam-check'
import { createScreenReasoningSummary } from './services/screen-reasoning'
import { SearchOrchestrator } from './services/search-orchestrator'
import { LocalStore } from './services/store'
import { createResultThumbnails, MAX_THUMBNAILS, resolveTrustedPath } from './services/thumbnails'
import {
  parseBoolean,
  parseCandidateId,
  parseEnrolmentId,
  parseLabel,
  parseProfileId,
  parseTrustedId,
  projectEnrolment,
  projectProfile
} from './services/people-ipc'
import { restoreReminderTimers } from './services/tools'
import { TelegramService } from './services/telegram'
import { PendingActionStore } from './services/pending-actions'
import { DroppedFileStore } from './services/dropped-files'
import {
  AgentRuntimeSupervisor,
  developmentAgentRuntimePaths,
  packagedAgentRuntimePaths,
  RuntimeUnavailableError,
  type AgentRuntimeSettings,
  type AgentRuntimeStatus
} from './services/agent-runtime-supervisor'
import { ActiveTaskStore, AgentTaskController } from './services/agent-tasks'
import { BrowserProfileController } from './services/browser-profile-controller'
import { TakeoverCaptureGuard } from './services/takeover-capture-guard'
import { registerAgentIpc, type IpcMainLike } from './services/agent-ipc'
import { VoiceTaskController } from './services/voice-task-controller'
import { AgentMemoryStore } from './agent/agent-memory'
import { DiagnosticsLog } from './agent/diagnostics'
import { TaskRequestInterpreter } from './agent/task-request-interpreter'
import { trustedCalendarClock } from './agent/trusted-clock'
import { DemoClinicSite, readRuntimeConfig } from './agent/packaged-runtime'
import { createModelRouter } from './models/model-config'
import type { ModelRouter } from './models/model-router'
import { PageAnswerer } from './agent/page-answer'
import { AuthenticatedAnswerer } from './agent/authenticated-answer'
import { AuthenticatedPlanner } from './agent/authenticated-planner'
import { FormPlanner } from './agent/form-planner'
import { DesktopReader } from './agent/desktop-reader'
import { DesktopReadController } from './services/desktop-read-controller'
import { DesktopPlanner } from './agent/desktop-planner'
import { DesktopPlanningController } from './services/desktop-planning-controller'
import { DesktopVisionReasoner } from './agent/desktop-vision'
import { DesktopVisionController } from './services/desktop-vision-controller'
import { DocumentController } from './services/document-controller'
import { TransferController } from './services/transfer-controller'
import { DocumentComparer } from './agent/document-comparer'
import { LocalOcrEngine } from './vision/ocr-engine'
import { extrasLanguageDirectory, isExtrasPackInstalled } from './vision/model-pack'
import { DesktopActionController } from './services/desktop-action-controller'
import { ResearchAnswerer } from './agent/research-answer'
import { ResearchPlanner } from './agent/research-planner'
import {
  PublicUrlPolicy,
  RESEARCH_POLICY_VERSION,
  parseAllowedHosts,
  parseTestOrigins
} from './agent/public-url-policy'
import { isTrustedRendererUrl, isTrustedSenderFrame, type RendererLocation } from './services/ipc-sender'
import { developmentContentSecurityPolicy } from './services/content-security-policy'
import { AGENT_IPC_CHANNELS, type AgentRuntimeView } from '../shared/agent-contracts'
import { PhotoIndexCoordinator } from './vision/coordinator'
import { letterbox } from './vision/face-image'
import { PersonEnrollmentService, EnrolmentError } from './vision/person-enrollment'
import { PersonProfileStore, PersonProfileError, MIN_REFERENCES, MAX_REFERENCES } from './vision/person-profiles'
import { VisionEngine, type VisionWorkerHandle } from './vision/engine'
import { isModelPackInstalled, resolveAssetPath } from './vision/model-pack'
import {
  anchorOf,
  boundsForAnchor,
  clampToDisplays,
  defaultBounds,
  WindowStateStore,
  type Size
} from './services/window-state'

let mainWindow: BrowserWindow | undefined
let localStore: LocalStore
let telegramService: TelegramService
let pendingActions: PendingActionStore
let intentTracker: IntentTracker
let searchOrchestrator: SearchOrchestrator
let photoIndexCoordinator: PhotoIndexCoordinator
/** The only store of biometric data, and the only thing that reads it. */
let personProfiles: PersonProfileStore
let personEnrollment: PersonEnrollmentService
let windowState: WindowStateStore
let droppedFiles: DroppedFileStore
let agentRuntime: AgentRuntimeSupervisor | undefined
/**
 * Milestone 7a destination policy for page inspection, from main's trusted
 * configuration only. Empty (no inspection) until configured; the runtime and
 * browser worker receive the same lists and enforce them independently.
 */
let publicInspectionPolicy = new PublicUrlPolicy()
/**
 * Milestone 7b destination policy for public research, from main's trusted
 * configuration only. Empty (no research) until configured. It is a *separate*
 * policy from inspection's: whatever research allows, Milestone 7a keeps its
 * own host allowlist.
 */
let publicResearchPolicy = new PublicUrlPolicy({ version: RESEARCH_POLICY_VERSION })
let agentRuntimeShutdownStarted = false
/** Packaged builds only: why there is no runtime, when there is none. */
let agentRuntimeUnconfigured = false
/**
 * Milestone 8a S2: set once startup has *established* that this installation
 * has no agent runtime at all -- never merely because startup has not
 * finished deciding. It is the one non-runtime answer the screen-capture
 * takeover guard accepts; see `takeover-capture-guard.ts` for why.
 */
let agentRuntimeAbsent = false
/** Refuses screen capture until durable takeover state has been reconciled. */
let takeoverCaptureGuard: TakeoverCaptureGuard | undefined
let demoClinicSite: DemoClinicSite | undefined
let panelOpen = false
const retainedCapture = new RetainedCaptureStore()
// The scripted Gemini socket exists only in unpackaged acceptance builds.
const scriptedGemini = !app.isPackaged && process.env.LUMI_REALTIME_SCRIPTED === 'gemini'
// Redacted by construction; shown only in development or when explicitly enabled.
const diagnosticsVisible = !app.isPackaged || process.env.LUMI_DIAGNOSTICS === '1'
const diagnostics = new DiagnosticsLog({ echo: !app.isPackaged && process.env.LUMI_DIAGNOSTICS === '1' })
const voiceRelay = new GeminiLiveRelay({
  tokens: scriptedGemini
    ? { accessToken: async () => 'scripted-token', projectId: async () => 'scripted-project' }
    : vertexEnabled(process.env) ? new ApplicationDefaultCredentials() : undefined,
  location: vertexLocation(process.env),
  model: GEMINI_LIVE_MODEL,
  voice: process.env.LUMI_GEMINI_VOICE?.trim() || undefined,
  ...(scriptedGemini ? { socketFactory: () => new ScriptedGeminiSocket() } : {}),
  emit: (sessionId, relayEvent) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(VOICE_RELAY_CHANNELS.event, sessionId, relayEvent)
  },
  diagnostics
})
// Development and acceptance tests may isolate the profile. It must be set
// before the single-instance lock, which is keyed on the profile directory.
const userDataOverride = app.isPackaged ? undefined : process.env.LUMI_USER_DATA_DIR
if (userDataOverride && isAbsolute(userDataOverride)) app.setPath('userData', userDataOverride)
const ownsSingleInstance = app.requestSingleInstanceLock()
if (!ownsSingleInstance) {
  // Exit at once: quitting before "ready" can leave a windowless process.
  console.error('Another Lumi instance owns this profile; exiting.')
  app.exit(0)
}

app.on('second-instance', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show()
    mainWindow.focus()
  }
})

const CLOSED_WINDOW_SIZE = { width: 88, height: 88 }
const OPEN_WINDOW_SIZE = { width: 390, height: 640 }

function currentWindowSize(): Size {
  return panelOpen ? OPEN_WINDOW_SIZE : CLOSED_WINDOW_SIZE
}

function developmentRendererUrl(): string | undefined {
  return app.isPackaged ? undefined : process.env.ELECTRON_RENDERER_URL
}

function rendererLocation(): RendererLocation {
  return {
    developmentUrl: developmentRendererUrl(),
    fileUrl: pathToFileURL(join(__dirname, '../renderer/index.html')).toString()
  }
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    title: 'Lumi',
    // Packaged builds inherit the icon electron-builder embeds in the
    // executable; this only dresses the dev run, where there is no exe icon.
    ...(app.isPackaged ? {} : { icon: join(__dirname, '../../build/icon.ico') }),
    width: CLOSED_WINDOW_SIZE.width,
    height: CLOSED_WINDOW_SIZE.height,
    show: false,
    transparent: true,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: false,
    hasShadow: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: join(__dirname, '../preload/index.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  window.setAlwaysOnTop(true, 'floating')
  window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  positionWindow(window, CLOSED_WINDOW_SIZE)
  window.once('ready-to-show', () => window.showInactive())
  // Debounced inside the store, so a drag never writes on every frame.
  window.on('move', rememberWindowPosition)

  const developmentUrl = developmentRendererUrl()
  if (developmentUrl) {
    void window.loadURL(developmentUrl)
  } else {
    void window.loadFile(join(__dirname, '../renderer/index.html'))
  }

  window.webContents.on('will-navigate', (event, url) => {
    if (!isTrustedRendererUrl(url, rendererLocation())) {
      event.preventDefault()
    }
  })
  window.webContents.on('will-frame-navigate', (event) => {
    // Lumi renders no frames; no subframe may load anything.
    if (!event.isMainFrame) event.preventDefault()
  })
  window.webContents.on('will-attach-webview', (event) => event.preventDefault())
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.on('closed', () => {
    retainedCapture.clear()
    if (mainWindow === window) {
      mainWindow = undefined
    }
  })

  return window
}

/**
 * Places the window at its remembered bottom-right anchor, or at the default
 * corner of the primary display when there is nothing usable to restore.
 *
 * Electron reports window bounds and `display.workArea` in the same DIP space,
 * so no scale-factor arithmetic belongs here.
 */
function positionWindow(window: BrowserWindow, size: Size): void {
  const stored = windowState?.current()
  const primary = screen.getPrimaryDisplay()
  if (!stored) {
    window.setBounds(defaultBounds(primary, size))
    return
  }

  window.setBounds(clampToDisplays({ x: stored.anchorX, y: stored.anchorY }, size, screen.getAllDisplays(), primary))
}

function setPanelOpen(open: boolean): void {
  panelOpen = open
  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }

  resizeWindowAtCurrentPosition(mainWindow, open ? OPEN_WINDOW_SIZE : CLOSED_WINDOW_SIZE)
  rememberWindowPosition()
}

/**
 * Resizes around the window's bottom-right corner, so the orb appears to stay
 * where the user left it while the panel grows up and to the left from it.
 */
function resizeWindowAtCurrentPosition(window: BrowserWindow, requestedSize: Size): void {
  const anchor = anchorOf(window.getBounds())
  window.setBounds(clampToDisplays(anchor, requestedSize, screen.getAllDisplays(), screen.getPrimaryDisplay()))
}

/** Records the window's current corner so the next launch can restore it. */
function rememberWindowPosition(): void {
  if (!mainWindow || mainWindow.isDestroyed() || !windowState) {
    return
  }

  const anchor = anchorOf(mainWindow.getBounds())
  windowState.save({
    version: 1,
    anchorX: anchor.x,
    anchorY: anchor.y,
    open: panelOpen,
    alwaysOnTop: mainWindow.isAlwaysOnTop()
  })
}

/**
 * Re-clamps the live window after the display layout changes, so unplugging a
 * monitor or changing scale mid-session can never strand Lumi off-screen.
 */
function reclampWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }

  const anchor = anchorOf(mainWindow.getBounds())
  const bounds = clampToDisplays(anchor, currentWindowSize(), screen.getAllDisplays(), screen.getPrimaryDisplay())
  mainWindow.setBounds(bounds)
  rememberWindowPosition()
}

/** Recovery for a window the user can no longer reach. */
function resetWindowPosition(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }

  windowState?.clear()
  mainWindow.setBounds(defaultBounds(screen.getPrimaryDisplay(), currentWindowSize()))
  mainWindow.showInactive()
  rememberWindowPosition()
}

function requireMainWindow(event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent): void {
  if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents ||
    BrowserWindow.fromWebContents(event.sender) !== mainWindow) {
    throw new Error('Rejected IPC request from an unexpected window.')
  }
  // The top frame of Lumi's own renderer document, never a subframe or a
  // document the window was navigated to.
  if (!isTrustedSenderFrame({
    senderFrame: event.senderFrame,
    mainFrame: mainWindow.webContents.mainFrame,
    location: rendererLocation()
  })) {
    throw new Error('Rejected IPC request from an unexpected frame.')
  }
}

function agentRuntimeView(status?: AgentRuntimeStatus): AgentRuntimeView {
  if (!agentRuntime) return { state: agentRuntimeUnconfigured ? 'not_configured' : 'not_installed' }
  const current = status ?? agentRuntime.status()
  return { state: current.state, ...(current.generation ? { generation: current.generation } : {}) }
}

function emitAgentRuntimeStatus(status: AgentRuntimeStatus): void {
  // A runtime that just became reachable is the first chance to learn whether
  // a takeover is open, and -- after a runtime restart, which settles every
  // attempt from the dead generation as INTERRUPTED -- the first chance to
  // learn that one is over. A runtime going *away* is deliberately not a
  // trigger: it can only ever leave the guard where it already is, since a
  // takeover cannot begin without a runtime and an open one keeps the guard
  // refused until something durable says otherwise.
  if (status.state === 'running') void takeoverCaptureGuard?.reconcileNow()
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(AGENT_IPC_CHANNELS.runtimeStatusChanged, agentRuntimeView(status))
  }
}

/**
 * Development runtime configuration from main's own environment. The fixture
 * origin, headed mode and database override never reach the renderer.
 */
function developmentAgentRuntimeSettings(): AgentRuntimeSettings {
  const settings: AgentRuntimeSettings = {
    browserSiteOrigin: process.env.LUMI_APPOINTMENT_FIXTURE_ORIGIN || 'http://127.0.0.1:8801',
    browserHeadless: process.env.LUMI_BROWSER_HEADED !== '1'
  }
  if (process.env.LUMI_AGENT_DATABASE_URL) settings.databaseUrl = process.env.LUMI_AGENT_DATABASE_URL
  // Milestone 9 S2: Windows semantic observation is opt-in, in development builds only for now.
  if (process.env.LUMI_DESKTOP_OBSERVATION === '1') settings.desktopObservation = true
  if (process.env.LUMI_DESKTOP_REGISTERED_APPS) settings.desktopRegisteredApps = process.env.LUMI_DESKTOP_REGISTERED_APPS
  try {
    const hosts = parseAllowedHosts(process.env.LUMI_PUBLIC_INSPECTION_HOSTS ?? '')
    const testOrigins = parseTestOrigins(process.env.LUMI_INSPECTION_TEST_ORIGINS ?? '')
    publicInspectionPolicy = new PublicUrlPolicy({ allowedHosts: hosts, testOrigins })
    settings.publicInspectionHosts = hosts
    settings.inspectionTestOrigins = testOrigins
    const researchHosts = parseAllowedHosts(process.env.LUMI_RESEARCH_HOSTS ?? '')
    const researchTestOrigins = parseTestOrigins(process.env.LUMI_RESEARCH_TEST_ORIGINS ?? '')
    const researchAnyPublicHost = process.env.LUMI_RESEARCH_ANY_PUBLIC_HOST === '1'
    publicResearchPolicy = new PublicUrlPolicy({
      allowedHosts: researchHosts,
      testOrigins: researchTestOrigins,
      allowAnyPublicHost: researchAnyPublicHost,
      version: RESEARCH_POLICY_VERSION
    })
    if (researchAnyPublicHost) settings.researchAnyPublicHost = true
    if (researchHosts.length > 0) settings.researchHosts = researchHosts
    if (researchTestOrigins.length > 0) settings.researchTestOrigins = researchTestOrigins
    if (process.env.LUMI_RESEARCH_SEARCH_ENDPOINT) {
      settings.researchSearchEndpoint = process.env.LUMI_RESEARCH_SEARCH_ENDPOINT
    }
    const authTestOrigins = parseTestOrigins(process.env.LUMI_AUTH_TEST_ORIGINS ?? '')
    if (authTestOrigins.length > 0) settings.authTestOrigins = authTestOrigins
  } catch {
    // Fail closed: a malformed list disables page inspection and research.
    publicInspectionPolicy = new PublicUrlPolicy()
    publicResearchPolicy = new PublicUrlPolicy({ version: RESEARCH_POLICY_VERSION })
    console.error('Lumi public browsing configuration is invalid; inspection and research are disabled.')
  }
  return settings
}

/**
 * Packaged builds: the bundled Python runtime under resources/agent-runtime,
 * configured from the user's agent-runtime.json. No repository checkout,
 * developer shell or manually started server is involved.
 */
async function startPackagedAgentRuntime(): Promise<void> {
  const config = await readRuntimeConfig(app.getPath('userData'))
  if (config.kind !== 'ok') {
    agentRuntimeUnconfigured = true
    // Established, not assumed: with no configuration there is no runtime to
    // start, so no takeover can exist for the capture guard to protect.
    agentRuntimeAbsent = true
    void takeoverCaptureGuard?.reconcileNow()
    console.error(`Lumi agent runtime is not configured (${config.kind === 'missing' ? 'agent-runtime.json missing' : config.reason}).`)
    emitAgentRuntimeStatus({ state: 'stopped' })
    return
  }
  const paths = packagedAgentRuntimePaths(process.resourcesPath)
  const settings: AgentRuntimeSettings = {
    databaseUrl: config.config.databaseUrl,
    browserHeadless: config.config.headless,
    browsersPath: paths.browsersPath,
    migrate: true,
    // Packaged builds offer public hosts only; loopback test origins never.
    publicInspectionHosts: config.config.publicInspectionHosts,
    ...(config.config.research ? { researchAnyPublicHost: true } : {}),
    ...(config.config.researchHosts.length > 0 ? { researchHosts: config.config.researchHosts } : {}),
    ...(config.config.researchSearchEndpoint ? { researchSearchEndpoint: config.config.researchSearchEndpoint } : {})
  }
  publicInspectionPolicy = new PublicUrlPolicy({ allowedHosts: config.config.publicInspectionHosts })
  publicResearchPolicy = new PublicUrlPolicy({
    allowedHosts: config.config.researchHosts,
    allowAnyPublicHost: config.config.research,
    version: RESEARCH_POLICY_VERSION
  })
  try {
    if (config.config.clinicSite === 'demo') {
      demoClinicSite = new DemoClinicSite(paths.pythonPath, paths.agentRoot)
      settings.browserSiteOrigin = await demoClinicSite.start()
    } else if (config.config.clinicSite !== 'none') {
      settings.browserSiteOrigin = config.config.clinicSite.origin
    }
    agentRuntime = new AgentRuntimeSupervisor({
      agentRoot: paths.agentRoot,
      pythonPath: paths.pythonPath,
      runtimeSettings: settings,
      onStatus: emitAgentRuntimeStatus
    })
  } catch {
    console.error('Lumi agent runtime could not be prepared.')
    agentRuntimeUnconfigured = true
    agentRuntimeAbsent = true
    void takeoverCaptureGuard?.reconcileNow()
    emitAgentRuntimeStatus({ state: 'failed' })
    return
  }
  startAgentRuntime()
}

function startAgentRuntime(): void {
  if (!agentRuntime) return
  if (!agentRuntime.installed()) {
    // The runtime's own files are not on this machine, so no runtime process
    // has ever run here and no login takeover can exist. That is an answer for
    // the capture guard, unlike a runtime that is merely not responding.
    agentRuntimeAbsent = true
    void takeoverCaptureGuard?.reconcileNow()
  }
  void agentRuntime.start().catch(() => {
    // Status reaches the UI through onStatus. Never log child output or
    // environment because both can contain credentials.
    console.error('Lumi agent runtime did not start.')
  })
}

function registerIpcHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.listCaptureSources, async (event) => {
    requireMainWindow(event)
    return listCaptureSources()
  })

  ipcMain.handle(IPC_CHANNELS.captureScreen, async (event, sourceId: unknown) => {
    requireMainWindow(event)
    if (sourceId !== undefined && (typeof sourceId !== 'string' || sourceId.length === 0 || sourceId.length > 500)) {
      throw new Error('Capture source must be a short source identifier.')
    }

    const shouldRestoreWindow = Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible())
    try {
      if (shouldRestoreWindow) {
        mainWindow?.hide()
        await waitForDesktopRepaint()
      }
      const capture = await captureScreen(sourceId)
      retainedCapture.replace(capture)
      return capture
    } finally {
      if (shouldRestoreWindow && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.showInactive()
      }
    }
  })

  ipcMain.handle(IPC_CHANNELS.analyzeCapture, async (event, captureId: unknown) => {
    requireMainWindow(event)
    if (!isCaptureId(captureId)) {
      throw new Error('Screen reasoning requires a valid capture from this session.')
    }
    const capture = retainedCapture.get(captureId)
    if (!capture) {
      throw new Error('That screen capture is no longer available. Capture it again before asking Lumi to review it.')
    }
    return createScreenReasoningSummary({ id: captureId, dataUrl: capture.dataUrl }, app.getPath('userData'))
  })

  /**
   * Reviews one already-confirmed capture for scam warning signs.
   *
   * Deliberately identical in shape to `analyzeCapture`: a capture id and
   * nothing else crosses from the renderer, and the image is resolved from
   * main's own memory. This handler reads a capture and returns an
   * assessment — it opens nothing, sends nothing, and stores nothing.
   */
  ipcMain.handle(IPC_CHANNELS.checkCaptureForScam, async (event, captureId: unknown) => {
    requireMainWindow(event)
    if (!isCaptureId(captureId)) {
      throw new Error('A scam check needs a valid capture from this session.')
    }
    const capture = retainedCapture.get(captureId)
    if (!capture) {
      throw new Error('That screen capture is no longer available. Capture it again before asking Lumi to check it.')
    }
    return createScamCheckAssessment({ id: captureId, dataUrl: capture.dataUrl }, app.getPath('userData'))
  })

  ipcMain.handle(IPC_CHANNELS.discardCapture, (event) => {
    requireMainWindow(event)
    retainedCapture.clear()
  })

  ipcMain.handle(IPC_CHANNELS.createRealtimeSession, async (event) => {
    requireMainWindow(event)
    return createRealtimeSessionCredential(app.getPath('userData'), {
      allowScripted: !app.isPackaged,
      geminiConfigured: voiceRelay.configured()
    })
  })

  // Gemini Live relay. Main owns the socket and the Google token; the
  // renderer sends only closed, validated message kinds.
  ipcMain.handle(VOICE_RELAY_CHANNELS.open, async (event) => {
    requireMainWindow(event)
    try {
      return { ok: true, sessionId: await voiceRelay.open() }
    } catch {
      return { ok: false, message: 'Gemini Live could not be reached. Check the Vertex AI configuration.' }
    }
  })
  ipcMain.handle(VOICE_RELAY_CHANNELS.send, (event, sessionId: unknown, message: unknown) => {
    requireMainWindow(event)
    if (typeof sessionId !== 'string') return false
    return voiceRelay.send(sessionId, message, scriptedGemini)
  })
  ipcMain.handle(VOICE_RELAY_CHANNELS.close, (event, sessionId: unknown) => {
    requireMainWindow(event)
    if (typeof sessionId === 'string') voiceRelay.close(sessionId)
  })

  ipcMain.handle(IPC_CHANNELS.noteUserRequest, (event, request: unknown) => {
    requireMainWindow(event)
    if (typeof request !== 'string' || request.trim().length === 0 || request.length > 4_000) {
      throw new Error('A user request must be a short non-empty text.')
    }
    return intentTracker.noteUserRequest(request)
  })

  ipcMain.handle(IPC_CHANNELS.evaluateToolRequest, async (event, toolName: unknown) => {
    requireMainWindow(event)
    if (typeof toolName !== 'string' || !GUARDED_TOOLS.includes(toolName as GuardedTool)) {
      throw new Error('Tool policy evaluation is only available for guarded tools.')
    }
    const hasApprovedFolder = (await localStore.listDocumentRoots()).length > 0
    return intentTracker.evaluateToolRequest(toolName as GuardedTool, hasApprovedFolder)
  })

  ipcMain.handle(IPC_CHANNELS.createPendingAction, async (event, proposal: unknown) => {
    requireMainWindow(event)
    return pendingActions.create(proposal)
  })

  ipcMain.handle(IPC_CHANNELS.approvePendingAction, async (event, approvalId: unknown) => {
    requireMainWindow(event)
    return pendingActions.approve(approvalId)
  })

  ipcMain.handle(IPC_CHANNELS.cancelPendingAction, (event, approvalId: unknown) => {
    requireMainWindow(event)
    pendingActions.cancel(approvalId)
  })

  ipcMain.handle(IPC_CHANNELS.chooseDocumentRoot, async (event) => {
    requireMainWindow(event)
    if (!mainWindow) {
      throw new Error('The Lumi window is unavailable.')
    }

    const selection = await dialog.showOpenDialog(mainWindow, {
      title: 'Choose a folder Lumi may search',
      buttonLabel: 'Approve this folder',
      properties: ['openDirectory'],
      // Only where the chooser opens. Access still comes from the user's pick.
      defaultPath: suggestedFolderHint()
    })
    if (selection.canceled || !selection.filePaths[0]) {
      searchOrchestrator.notifyFolderDeclined()
      return undefined
    }

    const path = selection.filePaths[0]
    const root = await localStore.addDocumentRoot(path, basename(path) || 'Approved folder')
    void photoIndexCoordinator.reconcile()
    // Approving a folder is what the held search was waiting for; it resumes
    // here so the user never repeats the original request.
    await searchOrchestrator.notifyFolderApproved()
    return root
  })

  ipcMain.handle(IPC_CHANNELS.beginFileSearch, async (event, request: unknown) => {
    requireMainWindow(event)
    return searchOrchestrator.begin(parseFileSearchRequest(request))
  })

  ipcMain.handle(IPC_CHANNELS.cancelFileSearch, (event) => {
    requireMainWindow(event)
    searchOrchestrator.clear()
  })

  ipcMain.handle(IPC_CHANNELS.getResultThumbnails, async (event, resultIds: unknown) => {
    requireMainWindow(event)
    if (!Array.isArray(resultIds) || resultIds.length > MAX_THUMBNAILS) {
      throw new Error('Thumbnails are only available for a short list of search results.')
    }
    return createResultThumbnails(localStore, resultIds as string[], undefined, droppedFiles)
  })

  ipcMain.handle(IPC_CHANNELS.cancelPhotoAnalysis, (event) => {
    requireMainWindow(event)
    pendingActions.clearPhotoAnalysis()
  })

  ipcMain.handle(IPC_CHANNELS.getPhotoSearchStatus, (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.status()
  })
  ipcMain.handle(IPC_CHANNELS.enablePhotoSearch, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.enable()
  })
  ipcMain.handle(IPC_CHANNELS.downloadPhotoSearchModel, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.downloadModel()
  })
  ipcMain.handle(IPC_CHANNELS.cancelPhotoSearchDownload, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.cancelDownload()
  })
  ipcMain.handle(IPC_CHANNELS.pausePhotoIndex, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.pause()
  })
  ipcMain.handle(IPC_CHANNELS.resumePhotoIndex, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.resume()
  })
  ipcMain.handle(IPC_CHANNELS.rebuildPhotoIndex, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.rebuild()
  })
  ipcMain.handle(IPC_CHANNELS.setPhotoTextSearchEnabled, async (event, enabled: unknown) => {
    requireMainWindow(event)
    if (typeof enabled !== 'boolean') throw new Error('The text search preference must be a boolean.')
    return photoIndexCoordinator.setTextSearchEnabled(enabled)
  })
  ipcMain.handle(IPC_CHANNELS.setPhotoFaceCountEnabled, async (event, enabled: unknown) => {
    requireMainWindow(event)
    if (typeof enabled !== 'boolean') throw new Error('The visible-face counting preference must be a boolean.')
    return photoIndexCoordinator.setFaceCountEnabled(enabled)
  })
  ipcMain.handle(IPC_CHANNELS.rebuildPhotoTextIndex, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.rebuildTextIndex()
  })
  ipcMain.handle(IPC_CHANNELS.rebuildPhotoFaceIndex, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.rebuildFaceIndex()
  })
  ipcMain.handle(IPC_CHANNELS.disablePhotoSearch, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.disable()
  })
  ipcMain.handle(IPC_CHANNELS.setPhotoIndexOnlyWhilePluggedIn, async (event, enabled: unknown) => {
    requireMainWindow(event)
    if (typeof enabled !== 'boolean') throw new Error('The plugged-in indexing preference must be a boolean.')
    return photoIndexCoordinator.setOnlyWhilePluggedIn(enabled)
  })
  ipcMain.handle(IPC_CHANNELS.setRealtimeActive, (event, active: unknown) => {
    requireMainWindow(event)
    if (typeof active !== 'boolean') throw new Error('Realtime activity must be a boolean.')
    photoIndexCoordinator.setRealtimeActive(active)
  })

  // --- Phase 3: labelled people ---------------------------------------------
  // Every payload is parsed before it reaches a service, and every reply is
  // projected rather than forwarded. See services/people-ipc.ts.

  ipcMain.handle(IPC_CHANNELS.getPeopleSearchStatus, (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.peopleStatus()
  })

  ipcMain.handle(IPC_CHANNELS.setPeopleSearchEnabled, async (event, enabled: unknown) => {
    requireMainWindow(event)
    return photoIndexCoordinator.setPeopleSearchEnabled(parseBoolean(enabled))
  })

  ipcMain.handle(IPC_CHANNELS.pausePeopleScan, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.pausePeopleScan()
  })

  ipcMain.handle(IPC_CHANNELS.resumePeopleScan, async (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.resumePeopleScan()
  })

  ipcMain.handle(IPC_CHANNELS.listPeopleProfiles, (event) => {
    requireMainWindow(event)
    return photoIndexCoordinator.peopleStatus().profiles
  })

  ipcMain.handle(IPC_CHANNELS.beginPeopleEnrolment, (event, label: unknown) => {
    requireMainWindow(event)
    try {
      const draft = personEnrollment.begin(parseLabel(label))
      return projectEnrolment(enrolmentSource(draft))
    } catch (error) {
      rethrowAsSafeError(error)
    }
  })

  ipcMain.handle(IPC_CHANNELS.beginPersonReferenceAddition, (event, profileId: unknown) => {
    requireMainWindow(event)
    const id = parseProfileId(profileId)
    const profile = photoIndexCoordinator.peopleStatus().profiles.find((candidate) => candidate.id === id)
    if (!profile) {
      throw new Error('That person is no longer saved.')
    }
    try {
      const draft = personEnrollment.beginAddition(id, profile.label)
      return projectEnrolment(enrolmentSource(draft))
    } catch (error) {
      rethrowAsSafeError(error)
    }
  })

  ipcMain.handle(IPC_CHANNELS.addPeopleReference, async (event, enrolmentId: unknown, trustedId: unknown) => {
    requireMainWindow(event)
    const draftId = parseEnrolmentId(enrolmentId)
    const trusted = parseTrustedId(trustedId)
    try {
      const draft = await personEnrollment.addReference(draftId, trusted)
      return projectEnrolment(enrolmentSource(draft))
    } catch (error) {
      // A rejection is an ordinary outcome here — the photo had no face, or the
      // wrong one — so it is reported on the draft rather than thrown away.
      try {
        const draft = personEnrollment.list(draftId)
        return projectEnrolment({ ...enrolmentSource(draft), lastRejection: peopleErrorMessage(error) })
      } catch {
        rethrowAsSafeError(error)
      }
    }
  })

  ipcMain.handle(IPC_CHANNELS.selectPeopleFace, async (event, enrolmentId: unknown, candidateId: unknown) => {
    requireMainWindow(event)
    const draftId = parseEnrolmentId(enrolmentId)
    const candidate = parseCandidateId(candidateId)
    try {
      const draft = await personEnrollment.selectFace(draftId, candidate)
      return projectEnrolment(enrolmentSource(draft))
    } catch (error) {
      try {
        const draft = personEnrollment.list(draftId)
        return projectEnrolment({ ...enrolmentSource(draft), lastRejection: peopleErrorMessage(error) })
      } catch {
        rethrowAsSafeError(error)
      }
    }
  })

  /**
   * The one call that creates a profile.
   *
   * Nothing above this line writes anything: begin, add and select all operate
   * on an in-memory draft that expires on its own. A profile exists only because
   * this handler ran, and this handler runs only because the user pressed a
   * button labelled "Create profile".
   */
  ipcMain.handle(IPC_CHANNELS.confirmPeopleEnrolment, async (event, enrolmentId: unknown) => {
    requireMainWindow(event)
    try {
      const summary = await personEnrollment.confirm(parseEnrolmentId(enrolmentId))
      await photoIndexCoordinator.profileCreated()
      return projectProfile({ ...summary, checked: 0, matched: 0 })
    } catch (error) {
      rethrowAsSafeError(error)
    }
  })

  ipcMain.handle(IPC_CHANNELS.cancelPeopleEnrolment, (event, enrolmentId: unknown) => {
    requireMainWindow(event)
    // Discards the draft and every preview and candidate id held for it.
    personEnrollment.cancel(parseEnrolmentId(enrolmentId))
  })

  ipcMain.handle(IPC_CHANNELS.renamePeopleProfile, async (event, profileId: unknown, label: unknown) => {
    requireMainWindow(event)
    try {
      const summary = await personProfiles.rename(parseProfileId(profileId), parseLabel(label))
      const view = photoIndexCoordinator.peopleStatus().profiles.find((profile) => profile.id === summary.id)
      // A rename does not invalidate anything, so the existing coverage stands.
      return view ?? projectProfile({ ...summary, checked: 0, matched: 0 })
    } catch (error) {
      rethrowAsSafeError(error)
    }
  })

  ipcMain.handle(IPC_CHANNELS.rescanPeopleProfile, async (event, profileId: unknown) => {
    requireMainWindow(event)
    await photoIndexCoordinator.rescanProfile(parseProfileId(profileId))
    return photoIndexCoordinator.peopleStatus()
  })

  ipcMain.handle(IPC_CHANNELS.deletePeopleProfile, async (event, profileId: unknown) => {
    requireMainWindow(event)
    const id = parseProfileId(profileId)
    // A draft appending a reference to this exact profile holds a preview and
    // candidate ids for a person who is about to no longer be enrolled.
    personEnrollment.cancelForProfile(id)
    await photoIndexCoordinator.deleteProfile(id)
    return photoIndexCoordinator.peopleStatus()
  })

  ipcMain.handle(IPC_CHANNELS.deleteAllPeopleData, async (event) => {
    requireMainWindow(event)
    // Drafts first: they hold previews and candidate ids in memory, and those
    // are people data too even though they were never written.
    personEnrollment.cancelAll()
    return photoIndexCoordinator.deleteAllPeopleData()
  })

  ipcMain.handle(IPC_CHANNELS.listDocumentRoots, async (event) => {
    requireMainWindow(event)
    return localStore.listDocumentRoots()
  })

  ipcMain.handle(IPC_CHANNELS.listReminders, async (event) => {
    requireMainWindow(event)
    return localStore.listReminders()
  })

  ipcMain.handle(IPC_CHANNELS.getTelegramStatus, (event) => {
    requireMainWindow(event)
    return telegramService.getStatus()
  })

  ipcMain.handle(IPC_CHANNELS.connectTelegram, async (event) => {
    requireMainWindow(event)
    return telegramService.connect()
  })

  ipcMain.handle(IPC_CHANNELS.cancelTelegramConnect, async (event) => {
    requireMainWindow(event)
    pendingActions.clearTelegram()
    return telegramService.cancelLogin()
  })

  ipcMain.handle(IPC_CHANNELS.submitTelegramPassword, (event, password: unknown) => {
    requireMainWindow(event)
    if (typeof password !== 'string' || password.length === 0 || password.length > 1_000) {
      throw new Error('Telegram password must be a short non-empty value.')
    }
    return telegramService.submitPassword(password)
  })

  ipcMain.handle(IPC_CHANNELS.logoutTelegram, async (event) => {
    requireMainWindow(event)
    pendingActions.clearTelegram()
    return telegramService.logout()
  })

  ipcMain.handle(IPC_CHANNELS.searchTelegramRecipients, async (event, query: unknown) => {
    requireMainWindow(event)
    if (typeof query !== 'string' || query.trim().length === 0 || query.length > 250) {
      throw new Error('Enter a short recipient name to search Telegram.')
    }
    return telegramService.searchRecipients(query)
  })

  ipcMain.on(IPC_CHANNELS.setPanelOpen, (event, open: unknown) => {
    requireMainWindow(event)
    if (typeof open !== 'boolean') {
      throw new Error('Panel state must be a boolean.')
    }

    setPanelOpen(open)
  })

  ipcMain.handle(IPC_CHANNELS.resetWindowPosition, (event) => {
    requireMainWindow(event)
    resetWindowPosition()
  })

  /**
   * Registering a dropped file validates it and retains it — and does nothing
   * else. No upload, no analysis, no send, no open, and no change to the
   * approved-folder list. Every later action confirms separately.
   */
  ipcMain.handle(IPC_CHANNELS.registerDroppedFile, async (event, path: unknown) => {
    requireMainWindow(event)
    if (typeof path !== 'string' || path.length > 32_000) {
      throw new Error('A dropped file must arrive as a single path.')
    }
    const descriptor = await droppedFiles.register(path)
    if (descriptor.mediaKind !== 'photo') {
      // Documents get an app-authored glyph in the renderer. Their contents are
      // never read.
      return descriptor
    }

    // A preview failure must not invalidate an otherwise valid dropped file.
    try {
      const [thumbnail] = await createResultThumbnails(localStore, [descriptor.droppedId], undefined, droppedFiles)
      return thumbnail?.status === 'ok' ? { ...descriptor, thumbnailDataUrl: thumbnail.dataUrl } : descriptor
    } catch {
      return descriptor
    }
  })

  ipcMain.handle(IPC_CHANNELS.removeDroppedFile, (event, droppedId: unknown) => {
    requireMainWindow(event)
    if (typeof droppedId !== 'string' || droppedId.length === 0 || droppedId.length > 250) {
      throw new Error('A dropped file identifier is invalid.')
    }
    droppedFiles.remove(droppedId)
  })
}

/**
 * Opens the chooser near where the requested files usually live. This is a
 * starting location only; nothing is approved until the user picks a folder.
 */
function suggestedFolderHint(): string | undefined {
  const kind = searchOrchestrator.pendingSearch()?.query.kind
  if (kind !== 'photo' && kind !== 'screenshot') {
    return undefined
  }

  try {
    const pictures = app.getPath('pictures')
    if (kind === 'screenshot') {
      const screenshots = join(pictures, 'Screenshots')
      return existsSync(screenshots) ? screenshots : pictures
    }
    return pictures
  } catch {
    return undefined
  }
}

function waitForDesktopRepaint(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 150))
}

function isCaptureId(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
}

function validateCaptureProvenance(proposal: ToolProposal): void {
  if (proposal.toolName !== 'create_reminder' && proposal.toolName !== 'save_context') {
    return
  }

  const captured = retainedCapture.get(proposal.arguments.sourceContext.captureId)
  if (!captured || captured.capturedAt !== proposal.arguments.sourceContext.capturedAt) {
    throw new Error('That action does not match a screen capture from this session. Nothing happened.')
  }
}

app.whenReady().then(async () => {
  // A process that lost the single-instance lock is already quitting; it must
  // not start a runtime, open a window or register handlers on the way out.
  if (!ownsSingleInstance) return
  // Must equal the electron-builder appId. Kept as the original identifier so
  // the rename to Lumi stays a visible-branding change and never relocates the
  // user's profile directory — see docs/UI-UX-POLISH.md §6.
  app.setAppUserModelId('com.lifelens.app')
  const devUrl = developmentRendererUrl()
  if (devUrl) {
    // Packaged builds carry the CSP in index.html; the dev server's documents
    // get an equivalent header scoped to the exact dev origin.
    const csp = developmentContentSecurityPolicy(devUrl)
    session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
      callback({ responseHeaders: { ...details.responseHeaders, 'Content-Security-Policy': [csp] } })
    })
  }
  if (app.isPackaged) {
    void startPackagedAgentRuntime()
  } else {
    // Development: the checkout's services/agent virtual environment.
    try {
      agentRuntime = new AgentRuntimeSupervisor({
        ...developmentAgentRuntimePaths(app.getAppPath()),
        runtimeSettings: developmentAgentRuntimeSettings(),
        onStatus: emitAgentRuntimeStatus
      })
    } catch {
      console.error('Lumi agent runtime configuration is invalid.')
      agentRuntimeAbsent = true
      void takeoverCaptureGuard?.reconcileNow()
    }
    startAgentRuntime()
  }
  localStore = new LocalStore(app.getPath('userData'))
  windowState = new WindowStateStore(app.getPath('userData'))
  await windowState.load()
  // In memory only: a dropped file is never written to disk and never joins
  // the approved-folder search scope.
  droppedFiles = new DroppedFileStore((path) => {
    const image = nativeImage.createFromPath(path)
    return image.isEmpty() ? undefined : image.getSize()
  })
  // safeStorage is passed in rather than reached for inside the store, so the
  // one place face data is encrypted is visible from here.
  personProfiles = new PersonProfileStore({
    userDataDir: app.getPath('userData'),
    secureStorage: safeStorage
  })

  photoIndexCoordinator = new PhotoIndexCoordinator({
    userDataDir: app.getPath('userData'),
    listRoots: () => localStore.listStoredDocumentRoots(),
    createEngine: createVisionEngine,
    decodeThumbnail: (path, size) => nativeImage.createThumbnailFromPath(path, size),
    modelRuntime: { fetch },
    isOnBattery: () => powerMonitor.isOnBatteryPower(),
    emitStatus: emitPhotoSearchStatus,
    emitPeopleStatus: emitPeopleSearchStatus,
    profileStore: personProfiles
  })

  personEnrollment = new PersonEnrollmentService({
    profiles: personProfiles,
    // An opaque id in, a path out, and only for ids main already issued. There
    // is no branch here that accepts a path from the renderer.
    resolveTrustedPath: (trustedId) => resolveTrustedPath(localStore, droppedFiles, trustedId),
    fingerprint: async (path) => {
      try {
        const details = await stat(path)
        return details.isFile() ? { sizeBytes: details.size, mtimeMs: details.mtimeMs } : undefined
      } catch {
        return undefined
      }
    },
    decodeImage: async (path) => {
      const image = nativeImage.createFromPath(path)
      return image.isEmpty() ? undefined : image
    },
    prepareDetectionBitmap: (image) => {
      const { width, height } = image.getSize()
      const scale = Math.min(1, 640 / width, 640 / height)
      const resized = image.resize({
        width: Math.max(1, Math.round(width * scale)),
        height: Math.max(1, Math.round(height * scale))
      })
      const size = resized.getSize()
      return { bitmap: letterbox(resized.toBitmap(), size.width, size.height), scale }
    },
    // The coordinator's engine, not a second one: enrolment and background
    // scanning must share one inference queue.
    detectFaces: (bitmap) => photoIndexCoordinator.visionEngine().detectFacesDetailed(bitmap),
    embedFaces: (tensors, count) => photoIndexCoordinator.visionEngine().embedFaces(tensors, count)
  })

  ipcMain.handle(IPC_CHANNELS.removeDocumentRoot, async (event, rootId: unknown) => {
    requireMainWindow(event)
    if (typeof rootId !== 'string' || rootId.length === 0 || rootId.length > 250) throw new Error('Folder approval identifier is invalid.')
    const removed = await localStore.removeDocumentRoot(rootId)
    if (removed) await photoIndexCoordinator.revokeRoot(rootId)
    return removed
  })
  await photoIndexCoordinator.initialize()
  telegramService = new TelegramService(app.getPath('userData'), safeStorage, emitTelegramStatus)
  pendingActions = new PendingActionStore(
    localStore,
    telegramService,
    validateCaptureProvenance,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    droppedFiles
  )
  intentTracker = new IntentTracker()
  searchOrchestrator = new SearchOrchestrator({
    listRoots: () => localStore.listDocumentRoots(),
    runSearch: (query) => runDocumentSearch(localStore, query, () => Date.now(), (semanticQuery) => photoIndexCoordinator.search(semanticQuery)),
    isTrustedIntent: (query) => intentTracker.supportsFileSearch(query),
    waitForTrust: waitForTrustedIntent,
    emit: emitFileSearchResolution
  })
  registerIpcHandlers()
  let modelRouter: ModelRouter | undefined
  try {
    modelRouter = createModelRouter({ allowScripted: !app.isPackaged, diagnostics }).router
  } catch {
    console.error('Lumi model routing configuration is invalid; typed requests and page answers are disabled.')
  }
  const pageAnswerer = modelRouter ? new PageAnswerer(modelRouter) : undefined
  // Both research model roles live in main, the only process holding provider
  // credentials. The planner proposes one step; the runtime decides whether it
  // is allowed and performs it.
  const researchPlanner = modelRouter ? new ResearchPlanner(modelRouter) : undefined
  const researchAnswerer = modelRouter ? new ResearchAnswerer(modelRouter) : undefined
  // Milestone 8a S3. The same shape, with one difference that is the point: the
  // recipient of every call comes from the confirmed grant, and the router
  // refuses to run these classes without it.
  const authenticatedPlanner = modelRouter ? new AuthenticatedPlanner(modelRouter) : undefined
  const authenticatedAnswerer = modelRouter ? new AuthenticatedAnswerer(modelRouter) : undefined
  // Milestone 8b S5. Proposes a `prepare_form` mapping and can act on nothing.
  const formPlanner = modelRouter ? new FormPlanner(modelRouter) : undefined
  const agentTasks = new AgentTaskController(
    {
      // Resolved per call: a packaged runtime is created asynchronously.
      request: (method, path, body, timeoutMs) => agentRuntime
        ? agentRuntime.request(method, path, body, timeoutMs)
        : Promise.reject(new RuntimeUnavailableError())
    },
    new ActiveTaskStore(app.getPath('userData')),
    {
      // Read per call: the packaged configuration is loaded asynchronously.
      get policy() { return publicInspectionPolicy },
      ...(pageAnswerer ? { answerer: pageAnswerer } : {})
    },
    {
      get policy() { return publicResearchPolicy },
      ...(researchPlanner ? { planner: researchPlanner } : {}),
      ...(researchAnswerer ? { answerer: researchAnswerer } : {})
    },
    {
      ...(authenticatedPlanner ? { planner: authenticatedPlanner } : {}),
      ...(authenticatedAnswerer ? { answerer: authenticatedAnswerer } : {}),
      ...(formPlanner ? { formPlanner } : {})
    }
  )
  const browserProfiles = new BrowserProfileController({
    request: (method, path, body, timeoutMs) => agentRuntime
      ? agentRuntime.request(method, path, body, timeoutMs)
      : Promise.reject(new RuntimeUnavailableError())
  })
  // Milestone 9 S2: exact desktop disclosure. Its own controller (never a member of the voice backend),
  // and its own reader: the typed question goes to the local runtime, and to the ONE approved provider
  // only after the trusted click.
  const desktopRead = new DesktopReadController({
    request: (method, path, body, timeoutMs) => agentRuntime
      ? agentRuntime.request(method, path, body, timeoutMs)
      : Promise.reject(new RuntimeUnavailableError())
  }, modelRouter ? new DesktopReader(modelRouter) : undefined)
  // Milestone 9 S3/S4: trusted focus, semantic scroll, registered-app launch and (S4) the SECOND,
  // separate execution approval for a validated plan's set-value/select/invoke. Its own controller,
  // never a member of the voice backend, and it never touches a provider.
  const desktopActions = new DesktopActionController({
    request: (method, path, body, timeoutMs) => agentRuntime
      ? agentRuntime.request(method, path, body, timeoutMs)
      : Promise.reject(new RuntimeUnavailableError())
  })
  // Milestone 9 S4: bounded desktop-action planning. Its own controller and its own planner, exactly
  // like S2's reader: the objective and candidate values go to the local runtime, and to the ONE
  // approved provider only after the trusted click. Disclosure authority only -- see
  // `DesktopActionController.proposeDesktopActionFromPlan` for where execution review begins.
  const desktopPlanning = new DesktopPlanningController({
    request: (method, path, body, timeoutMs) => agentRuntime
      ? agentRuntime.request(method, path, body, timeoutMs)
      : Promise.reject(new RuntimeUnavailableError())
  }, modelRouter ? new DesktopPlanner(modelRouter) : undefined)
  // Milestone 9 S5: scoped desktop visual fallback. Its own controller and its own reasoner, exactly
  // like S2/S4: a screenshot goes to the local runtime for local-only use (capture), and to the ONE
  // approved provider only after a SEPARATE trusted click (disclosure). Local OCR is best-effort and
  // only ever runs if the optional extras pack the person already installed is present; Lumi never
  // downloads anything to get it, matching the same fail-closed rule the photo indexer's own OCR use
  // already follows.
  let desktopOcrEngine: LocalOcrEngine | undefined
  isExtrasPackInstalled(app.getPath('userData')).then((installed) => {
    if (installed) desktopOcrEngine = new LocalOcrEngine({ languageDirectory: extrasLanguageDirectory(app.getPath('userData')) })
  }).catch(() => { desktopOcrEngine = undefined })
  const desktopVision = new DesktopVisionController({
    request: (method, path, body, timeoutMs) => agentRuntime
      ? agentRuntime.request(method, path, body, timeoutMs)
      : Promise.reject(new RuntimeUnavailableError())
  }, modelRouter ? new DesktopVisionReasoner(modelRouter) : undefined, () => desktopOcrEngine)
  // Milestone 10 S1: M10 file roots and approved documents. The folder dialog is opened HERE, in main;
  // the renderer never supplies or sees a path. A dropped file is resolved from main's own store.
  const documents = new DocumentController({
    runtime: {
      request: (method, path, body, timeoutMs) => agentRuntime
        ? agentRuntime.request(method, path, body, timeoutMs)
        : Promise.reject(new RuntimeUnavailableError())
    },
    comparer: modelRouter ? new DocumentComparer(modelRouter) : undefined,
    chooseFolder: async ({ label, canRead, canCreate }) => {
      if (!mainWindow) return undefined
      const selection = await dialog.showOpenDialog(mainWindow, {
        title: 'Choose a folder for Lumi’s file access',
        buttonLabel: 'Choose this folder',
        properties: ['openDirectory']
      })
      const folder = selection.canceled ? undefined : selection.filePaths[0]
      if (!folder) return undefined
      // A main-owned confirmation naming exactly what is being granted; the renderer's checkboxes alone
      // are never the authority.
      const allowed = [canRead ? '• read documents you choose from it' : undefined, canCreate ? '• save new downloads you approve into it' : undefined]
        .filter(Boolean).join('\n')
      const answer = await dialog.showMessageBox(mainWindow, {
        type: 'question',
        buttons: ['Allow', 'Cancel'],
        defaultId: 1,
        cancelId: 1,
        title: 'Lumi file access',
        message: `Give Lumi access to “${basename(folder) || folder}” as “${label}”?`,
        detail: `Lumi may:\n${allowed}\n\nLumi can never change, rename or delete files there. You can remove this access at any time.`
      })
      return answer.response === 0 ? folder : undefined
    },
    droppedFiles
  })
  // Milestone 10 S2: one controlled download. The renderer's card is confirmed again by a NATIVE dialog
  // built from what the runtime holds, so a compromised renderer cannot approve a download by itself.
  const transfers = new TransferController({
    runtime: {
      request: (method, path, body, timeoutMs) => agentRuntime
        ? agentRuntime.request(method, path, body, timeoutMs)
        : Promise.reject(new RuntimeUnavailableError())
    },
    confirmTransfer: async (card) => {
      if (!mainWindow) return false
      const answer = await dialog.showMessageBox(mainWindow, {
        type: 'question',
        buttons: ['Allow this download', 'Cancel'],
        defaultId: 1,
        cancelId: 1,
        title: 'Lumi download',
        message: `Download one ${card.expectedKind.toUpperCase()} file and save it as “${card.destName}” in “${card.destRootLabel}”?`,
        detail: `From: ${card.sourceUrl}\nAt most ${Math.ceil(card.maxBytes / 1024)} KB.\n\n`
          + 'Lumi checks the file’s contents first and refuses programs, scripts and macro documents. '
          + 'It never replaces an existing file and never opens the file.'
      })
      return answer.response === 0
    }
  })
  // Milestone 8a S2: screen capture is refused from this process's first
  // instruction and stays refused until durable takeover state has been read.
  // A main-process restart during a live takeover therefore cannot produce a
  // moment in which capture works and a sign-in window is on screen, and
  // nothing has to remember an attempt id -- not the renderer, not main.
  takeoverCaptureGuard = new TakeoverCaptureGuard({
    reconcile: () => browserProfiles.reconcileTakeovers(),
    agentRuntimeAbsent: () => agentRuntimeAbsent
  })
  takeoverCaptureGuard.start()
  const calendar = trustedCalendarClock({ allowFixedNow: !app.isPackaged })
  const memory = new AgentMemoryStore(app.getPath('userData'))
  // Voice and typed requests reach the same durable controller, never a second one.
  const voiceTasks = new VoiceTaskController(agentTasks, {
    calendarNow: calendar.now, timeZone: calendar.timeZone, memory, diagnostics
  })
  let interpreter: TaskRequestInterpreter | undefined
  if (modelRouter) {
    interpreter = new TaskRequestInterpreter({
      router: modelRouter,
      controller: voiceTasks,
      inspections: agentTasks,
      research: agentTasks,
      loadTask: async () => {
        const loaded = await agentTasks.loadActiveTask(0)
        return loaded.ok ? loaded.value : null
      },
      memory,
      diagnostics,
      now: calendar.now,
      timeZone: calendar.timeZone
    })
  }
  registerAgentIpc({
    ipcMain: ipcMain as unknown as IpcMainLike,
    assertTrustedSender: (event) => requireMainWindow(event as Electron.IpcMainInvokeEvent),
    controller: agentTasks,
    voice: voiceTasks,
    ...(interpreter ? { text: interpreter } : {}),
    memory,
    browserProfiles,
    desktopRead,
    desktopActions,
    desktopPlanning,
    desktopVision,
    documents,
    transfers,
    diagnostics: () => diagnosticsVisible ? diagnostics.list() : [],
    runtimeStatus: () => agentRuntimeView(),
    restartRuntime: async () => {
      if (!agentRuntime) throw new Error('The Lumi agent runtime is not installed.')
      await agentRuntime.restart()
      return agentRuntimeView()
    }
  })
  mainWindow = createWindow()
  await restoreReminderTimers(localStore)
  await telegramService.initialize()

  powerMonitor.on('on-battery', () => photoIndexCoordinator.powerChanged())
  powerMonitor.on('on-ac', () => photoIndexCoordinator.powerChanged())

  // A monitor can disappear or be rescaled while Lumi is running; the header
  // must stay reachable when it does.
  screen.on('display-removed', reclampWindow)
  screen.on('display-added', reclampWindow)
  screen.on('display-metrics-changed', reclampWindow)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow()
    }
  })
}).catch((error: unknown) => {
  // A startup failure must not leave a running process with no window.
  const name = error instanceof Error ? error.name : 'Error'
  const message = error instanceof Error ? error.message.replace(/[A-Za-z]:[\\/][^\s'"]*/g, '<path>').slice(0, 200) : ''
  console.error(`Lumi failed to start: ${name}: ${message}`)
  app.exit(1)
})

app.on('before-quit', () => retainedCapture.clear())
// Stops retrying; the guard is never cleared on the way out, because quitting
// is not an answer about whether a sign-in window is open.
app.on('before-quit', () => takeoverCaptureGuard?.stop())
app.on('will-quit', () => demoClinicSite?.stop())

app.on('before-quit', (event) => {
  if (agentRuntime === undefined || agentRuntimeShutdownStarted) return
  event.preventDefault()
  agentRuntimeShutdownStarted = true
  void agentRuntime.stop().catch(() => {
    console.error('Lumi agent runtime required forced shutdown.')
  }).finally(() => app.quit())
})

// A model search can outrun its own voice transcript, so the trusted intent may
// register a beat after the request arrives. These bound how long the search
// waits for that late transcript before it falls back to a confirmation card.
const TRUST_GRACE_MS = 600
const TRUST_POLL_MS = 50

/**
 * Polls the trusted intent tracker briefly so a late voice transcript's
 * noteUserRequest, processed on the main event loop while this awaits, can flip
 * the request to trusted before the orchestrator fails closed. Returns as soon
 * as the intent lands, or when the grace window elapses.
 */
async function waitForTrustedIntent(query: NormalizedSearchQuery): Promise<void> {
  const deadline = Date.now() + TRUST_GRACE_MS
  while (Date.now() < deadline) {
    if (intentTracker.supportsFileSearch(query)) {
      return
    }
    await delay(Math.min(TRUST_POLL_MS, Math.max(0, deadline - Date.now())))
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function emitFileSearchResolution(resolution: PendingSearchResolution): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.fileSearchResolved, resolution)
  }
}

function emitTelegramStatus(status: TelegramStatus): void {
  if (status.state !== 'connected') {
    pendingActions?.clearTelegram()
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.telegramAuthUpdate, status)
  }
}

function emitPhotoSearchStatus(status: ReturnType<PhotoIndexCoordinator['status']>): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.photoSearchStatusChanged, status)
  }
}

function emitPeopleSearchStatus(status: ReturnType<PhotoIndexCoordinator['peopleStatus']>): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.peopleSearchStatusChanged, status)
  }
}

/**
 * Turns a people-path failure into something safe to show.
 *
 * Both error classes carry app-authored messages keyed by a bounded code, so
 * they can be surfaced verbatim. Anything else is replaced: an arbitrary error
 * on this path may have been thrown by the profile store, and those messages can
 * quote the document they failed to parse.
 */
function peopleErrorMessage(error: unknown): string {
  if (error instanceof EnrolmentError || error instanceof PersonProfileError) {
    return error.message
  }
  if (error instanceof Error && error.name === 'PeopleIpcError') {
    return error.message
  }
  return 'Lumi could not complete that.'
}

function rethrowAsSafeError(error: unknown): never {
  throw new Error(peopleErrorMessage(error))
}

/**
 * Maps an enrolment draft onto the shape the projection accepts.
 *
 * Note that only the reference *count* crosses, never the reference entries:
 * the renderer has no use for a reference id, and every field not carried here
 * is a field that cannot leak later.
 */
function enrolmentSource(draft: ReturnType<PersonEnrollmentService['begin']>): {
  enrolmentId: string
  label: string
  acceptedReferences: number
  requiredReferences: number
  maximumReferences: number
  candidates?: Array<{ candidateId: string; previewDataUrl: string; selectable: boolean; note?: string }>
} {
  return {
    enrolmentId: draft.draftId,
    label: draft.label,
    acceptedReferences: draft.references.length,
    requiredReferences: MIN_REFERENCES,
    maximumReferences: MAX_REFERENCES,
    ...(draft.candidates ? { candidates: draft.candidates } : {})
  }
}

function createVisionEngine(): VisionEngine {
  const userDataDir = app.getPath('userData')
  return new VisionEngine({
    resolveModelPaths: async () => await isModelPackInstalled(userDataDir)
      ? { image: resolveAssetPath(userDataDir, 'imageModel'), text: resolveAssetPath(userDataDir, 'textModel') }
      : undefined,
    spawn: (): VisionWorkerHandle => {
      // Visible to the user in Windows Task Manager, so it carries the product name.
      const child = utilityProcess.fork(join(__dirname, 'vision-worker.cjs'), [], { serviceName: 'Lumi local photo search' })
      if (child.pid) {
        try { setPriority(child.pid, osPriority.priority.PRIORITY_BELOW_NORMAL) } catch { /* Best effort on supported platforms. */ }
      }
      return {
        postMessage: (message) => child.postMessage(message),
        onMessage: (listener) => child.on('message', listener),
        onExit: (listener) => child.on('exit', listener),
        kill: () => { child.kill() }
      }
    }
  })
}

app.on('before-quit', () => {
  // Persist any anchor still sitting in the debounce window.
  void windowState?.flush()
  droppedFiles?.clear()
  pendingActions?.clearAll()
  searchOrchestrator?.clear()
  void telegramService?.shutdown()
  void photoIndexCoordinator?.shutdown()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
