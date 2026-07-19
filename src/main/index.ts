import { app, BrowserWindow, dialog, ipcMain, nativeImage, powerMonitor, safeStorage, screen, utilityProcess } from 'electron'
import { existsSync } from 'node:fs'
import { constants as osPriority, setPriority } from 'node:os'
import { basename, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  IPC_CHANNELS,
  parseFileSearchRequest,
  type CaptureResult,
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
import { SearchOrchestrator } from './services/search-orchestrator'
import { LocalStore } from './services/store'
import { createResultThumbnails, MAX_THUMBNAILS } from './services/thumbnails'
import { restoreReminderTimers } from './services/tools'
import { TelegramService } from './services/telegram'
import { PendingActionStore } from './services/pending-actions'
import { PhotoIndexCoordinator } from './vision/coordinator'
import { VisionEngine, type VisionWorkerHandle } from './vision/engine'
import { isModelPackInstalled, resolveAssetPath } from './vision/model-pack'

let mainWindow: BrowserWindow | undefined
let localStore: LocalStore
let telegramService: TelegramService
let pendingActions: PendingActionStore
let intentTracker: IntentTracker
let searchOrchestrator: SearchOrchestrator
let photoIndexCoordinator: PhotoIndexCoordinator
const captures = new Map<string, Pick<CaptureResult, 'capturedAt'>>()

const CLOSED_WINDOW_SIZE = { width: 88, height: 88 }
const OPEN_WINDOW_SIZE = { width: 390, height: 640 }
const WINDOW_MARGIN = 20

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    title: 'LifeLens',
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

  return window
}

function positionWindow(window: BrowserWindow, size: { width: number; height: number }): void {
  const display = screen.getDisplayMatching(window.getBounds())
  const { x, y, width, height } = display.workArea
  window.setBounds({
    x: x + width - size.width - WINDOW_MARGIN,
    y: y + height - size.height - WINDOW_MARGIN,
    width: size.width,
    height: size.height
  })
}

function setPanelOpen(open: boolean): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }

  resizeWindowAtCurrentPosition(mainWindow, open ? OPEN_WINDOW_SIZE : CLOSED_WINDOW_SIZE)
}

function resizeWindowAtCurrentPosition(window: BrowserWindow, requestedSize: { width: number; height: number }): void {
  const currentBounds = window.getBounds()
  const display = screen.getDisplayMatching(currentBounds)
  const workArea = display.workArea
  const width = Math.min(requestedSize.width, workArea.width)
  const height = Math.min(requestedSize.height, workArea.height)
  window.setBounds({
    x: clamp(currentBounds.x, workArea.x, workArea.x + workArea.width - width),
    y: clamp(currentBounds.y, workArea.y, workArea.y + workArea.height - height),
    width,
    height
  })
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
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
      rememberCapture(capture)
      return capture
    } finally {
      if (shouldRestoreWindow && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.showInactive()
      }
    }
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
      throw new Error('LifeLens window is unavailable.')
    }

    const selection = await dialog.showOpenDialog(mainWindow, {
      title: 'Choose a folder LifeLens may search',
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
    return createResultThumbnails(localStore, resultIds as string[])
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

function rememberCapture(capture: CaptureResult): void {
  // The renderer keeps only the active, in-session image context. Main retains
  // just enough provenance to validate a confirmed reminder or saved context.
  captures.set(capture.id, { capturedAt: capture.capturedAt })
  while (captures.size > 12) {
    const oldestId = captures.keys().next().value
    if (!oldestId) {
      break
    }
    captures.delete(oldestId)
  }
}

function validateCaptureProvenance(proposal: ToolProposal): void {
  if (proposal.toolName !== 'create_reminder' && proposal.toolName !== 'save_context') {
    return
  }

  const captured = captures.get(proposal.arguments.sourceContext.captureId)
  if (!captured || captured.capturedAt !== proposal.arguments.sourceContext.capturedAt) {
    throw new Error('The requested action does not refer to a screen capture from this LifeLens session.')
  }
}

app.whenReady().then(async () => {
  app.setAppUserModelId('com.lifelens.app')
  localStore = new LocalStore(app.getPath('userData'))
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
  pendingActions = new PendingActionStore(localStore, telegramService, validateCaptureProvenance)
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

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow()
    }
  })
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

function createVisionEngine(): VisionEngine {
  const userDataDir = app.getPath('userData')
  return new VisionEngine({
    resolveModelPaths: async () => await isModelPackInstalled(userDataDir)
      ? { image: resolveAssetPath(userDataDir, 'imageModel'), text: resolveAssetPath(userDataDir, 'textModel') }
      : undefined,
    spawn: (): VisionWorkerHandle => {
      const child = utilityProcess.fork(join(__dirname, 'vision-worker.cjs'), [], { serviceName: 'LifeLens local photo search' })
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
