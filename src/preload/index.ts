import { contextBridge, ipcRenderer } from 'electron'
import { IPC_CHANNELS, type LifeLensApi, type ToolProposal } from '../shared/contracts'

const lifeLensApi: LifeLensApi = {
  listCaptureSources: () => ipcRenderer.invoke(IPC_CHANNELS.listCaptureSources),
  captureScreen: (sourceId?: string) => ipcRenderer.invoke(IPC_CHANNELS.captureScreen, sourceId),
  createRealtimeSession: () => ipcRenderer.invoke(IPC_CHANNELS.createRealtimeSession),
  executeConfirmedTool: (proposal: ToolProposal) => ipcRenderer.invoke(IPC_CHANNELS.executeConfirmedTool, proposal),
  chooseDocumentRoot: () => ipcRenderer.invoke(IPC_CHANNELS.chooseDocumentRoot),
  listDocumentRoots: () => ipcRenderer.invoke(IPC_CHANNELS.listDocumentRoots),
  listReminders: () => ipcRenderer.invoke(IPC_CHANNELS.listReminders),
  setPanelOpen: (open: boolean) => ipcRenderer.send(IPC_CHANNELS.setPanelOpen, open)
}

contextBridge.exposeInMainWorld('lifeLens', lifeLensApi)
