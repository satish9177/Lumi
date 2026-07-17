import { app, BrowserWindow, ipcMain, screen } from 'electron'
import { join } from 'node:path'
import { IPC_CHANNELS } from '../shared/contracts'
import { capturePrimaryScreen } from './services/capture'
import { createRealtimeSessionCredential } from './services/realtime'
import { LocalStore } from './services/store'
import { executeConfirmedTool, restoreReminderTimers } from './services/tools'

let mainWindow: BrowserWindow | undefined
let localStore: LocalStore

const CLOSED_WINDOW_SIZE = { width: 88, height: 88 }
const OPEN_WINDOW_SIZE = { width: 390, height: 640 }
const WINDOW_MARGIN = 20

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
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
      preload: join(__dirname, '../preload/index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
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

  positionWindow(mainWindow, open ? OPEN_WINDOW_SIZE : CLOSED_WINDOW_SIZE)
}

function requireMainWindow(event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent): void {
  if (!mainWindow || BrowserWindow.fromWebContents(event.sender) !== mainWindow) {
    throw new Error('Rejected IPC request from an unexpected window.')
  }
}

function registerIpcHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.captureScreen, async (event) => {
    requireMainWindow(event)
    return capturePrimaryScreen()
  })

  ipcMain.handle(IPC_CHANNELS.createRealtimeSession, async (event) => {
    requireMainWindow(event)
    return createRealtimeSessionCredential(app.getPath('userData'))
  })

  ipcMain.handle(IPC_CHANNELS.executeConfirmedTool, async (event, proposal: unknown) => {
    requireMainWindow(event)
    return executeConfirmedTool(localStore, proposal)
  })

  ipcMain.handle(IPC_CHANNELS.listReminders, async (event) => {
    requireMainWindow(event)
    return localStore.listReminders()
  })

  ipcMain.on(IPC_CHANNELS.setPanelOpen, (event, open: unknown) => {
    requireMainWindow(event)
    if (typeof open !== 'boolean') {
      throw new Error('Panel state must be a boolean.')
    }

    setPanelOpen(open)
  })
}

app.whenReady().then(async () => {
  app.setAppUserModelId('com.lifelens.app')
  localStore = new LocalStore(app.getPath('userData'))
  registerIpcHandlers()
  mainWindow = createWindow()
  await restoreReminderTimers(localStore)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow()
    }
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
