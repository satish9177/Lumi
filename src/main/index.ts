import { app, BrowserWindow, dialog, ipcMain, safeStorage, screen } from 'electron'
import { basename, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { IPC_CHANNELS, type CaptureResult, type TelegramStatus, type ToolProposal } from '../shared/contracts'
import { captureScreen, listCaptureSources } from './services/capture'
import { createRealtimeSessionCredential } from './services/realtime'
import { LocalStore } from './services/store'
import { restoreReminderTimers } from './services/tools'
import { TelegramService } from './services/telegram'
import { PendingActionStore } from './services/pending-actions'

let mainWindow: BrowserWindow | undefined
let localStore: LocalStore
let telegramService: TelegramService
let pendingActions: PendingActionStore
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
      properties: ['openDirectory']
    })
    if (selection.canceled || !selection.filePaths[0]) {
      return undefined
    }

    const path = selection.filePaths[0]
    return localStore.addDocumentRoot(path, basename(path) || 'Approved folder')
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
  telegramService = new TelegramService(app.getPath('userData'), safeStorage, emitTelegramStatus)
  pendingActions = new PendingActionStore(localStore, telegramService, validateCaptureProvenance)
  registerIpcHandlers()
  mainWindow = createWindow()
  await restoreReminderTimers(localStore)
  await telegramService.initialize()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow()
    }
  })
})

function emitTelegramStatus(status: TelegramStatus): void {
  if (status.state !== 'connected') {
    pendingActions?.clearTelegram()
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.telegramAuthUpdate, status)
  }
}

app.on('before-quit', () => {
  pendingActions?.clearAll()
  void telegramService?.shutdown()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
