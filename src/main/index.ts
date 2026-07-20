import { app, BrowserWindow, dialog, ipcMain, nativeImage, powerMonitor, safeStorage, screen, utilityProcess } from 'electron'
import { existsSync } from 'node:fs'
import { constants as osPriority, setPriority } from 'node:os'
import { basename, join } from 'node:path'
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
import { createRealtimeSessionCredential } from './services/realtime'
import { RetainedCaptureStore } from './services/retained-captures'
import { createScreenReasoningSummary } from './services/screen-reasoning'
import { SearchOrchestrator } from './services/search-orchestrator'
import { LocalStore } from './services/store'
import { createResultThumbnails, MAX_THUMBNAILS } from './services/thumbnails'
import { restoreReminderTimers } from './services/tools'
import { TelegramService } from './services/telegram'
import { PendingActionStore } from './services/pending-actions'
import { DroppedFileStore } from './services/dropped-files'
import { PhotoIndexCoordinator } from './vision/coordinator'
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
let windowState: WindowStateStore
let droppedFiles: DroppedFileStore
let panelOpen = false
const retainedCapture = new RetainedCaptureStore()

const CLOSED_WINDOW_SIZE = { width: 88, height: 88 }
const OPEN_WINDOW_SIZE = { width: 390, height: 640 }

function currentWindowSize(): Size {
  return panelOpen ? OPEN_WINDOW_SIZE : CLOSED_WINDOW_SIZE
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

  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void window.loadFile(join(__dirname, '../renderer/index.html'))
  }

  window.webContents.on('will-navigate', (event, url) => {
    const expectedRendererUrl = process.env.ELECTRON_RENDERER_URL ?? pathToFileURL(join(__dirname, '../renderer/index.html')).toString()
    if (!url.startsWith(expectedRendererUrl)) {
      event.preventDefault()
    }
  })
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
  if (!mainWindow || BrowserWindow.fromWebContents(event.sender) !== mainWindow) {
    throw new Error('Rejected IPC request from an unexpected window.')
  }
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

  ipcMain.handle(IPC_CHANNELS.discardCapture, (event) => {
    requireMainWindow(event)
    retainedCapture.clear()
  })

  ipcMain.handle(IPC_CHANNELS.createRealtimeSession, async (event) => {
    requireMainWindow(event)
    return createRealtimeSessionCredential(app.getPath('userData'))
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
  // Must equal the electron-builder appId. Kept as the original identifier so
  // the rename to Lumi stays a visible-branding change and never relocates the
  // user's profile directory — see docs/UI-UX-POLISH.md §6.
  app.setAppUserModelId('com.lifelens.app')
  localStore = new LocalStore(app.getPath('userData'))
  windowState = new WindowStateStore(app.getPath('userData'))
  await windowState.load()
  // In memory only: a dropped file is never written to disk and never joins
  // the approved-folder search scope.
  droppedFiles = new DroppedFileStore((path) => {
    const image = nativeImage.createFromPath(path)
    return image.isEmpty() ? undefined : image.getSize()
  })
  photoIndexCoordinator = new PhotoIndexCoordinator({
    userDataDir: app.getPath('userData'),
    listRoots: () => localStore.listStoredDocumentRoots(),
    createEngine: createVisionEngine,
    decodeThumbnail: (path, size) => nativeImage.createThumbnailFromPath(path, size),
    modelRuntime: { fetch },
    isOnBattery: () => powerMonitor.isOnBatteryPower(),
    emitStatus: emitPhotoSearchStatus
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
})

app.on('before-quit', () => retainedCapture.clear())

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
