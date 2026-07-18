import { contextBridge, ipcRenderer } from 'electron'
import { IPC_CHANNELS, type LifeLensApi, type TelegramStatus, type ToolProposal } from '../shared/contracts'

const lifeLensApi: LifeLensApi = {
  listCaptureSources: () => ipcRenderer.invoke(IPC_CHANNELS.listCaptureSources),
  captureScreen: (sourceId?: string) => ipcRenderer.invoke(IPC_CHANNELS.captureScreen, sourceId),
  createRealtimeSession: () => ipcRenderer.invoke(IPC_CHANNELS.createRealtimeSession),
  createPendingAction: (proposal: ToolProposal) => ipcRenderer.invoke(IPC_CHANNELS.createPendingAction, proposal),
  approvePendingAction: (approvalId: string) => ipcRenderer.invoke(IPC_CHANNELS.approvePendingAction, approvalId),
  cancelPendingAction: (approvalId: string) => ipcRenderer.invoke(IPC_CHANNELS.cancelPendingAction, approvalId),
  chooseDocumentRoot: () => ipcRenderer.invoke(IPC_CHANNELS.chooseDocumentRoot),
  listDocumentRoots: () => ipcRenderer.invoke(IPC_CHANNELS.listDocumentRoots),
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
  setPanelOpen: (open: boolean) => ipcRenderer.send(IPC_CHANNELS.setPanelOpen, open)
}

contextBridge.exposeInMainWorld('lifeLens', lifeLensApi)
