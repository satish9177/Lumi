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

const lifeLensApi: LifeLensApi = {
  listCaptureSources: () => ipcRenderer.invoke(IPC_CHANNELS.listCaptureSources),
  captureScreen: (sourceId?: string) => ipcRenderer.invoke(IPC_CHANNELS.captureScreen, sourceId),
  analyzeCapture: (captureId: string) => ipcRenderer.invoke(IPC_CHANNELS.analyzeCapture, captureId),
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
  removeDroppedFile: (droppedId: string) => ipcRenderer.invoke(IPC_CHANNELS.removeDroppedFile, droppedId)
}

contextBridge.exposeInMainWorld('lifeLens', lifeLensApi)
