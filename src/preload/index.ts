import { contextBridge, ipcRenderer } from 'electron'
import { IPC_CHANNELS, type LifeLensApi, type ToolProposal } from '../shared/contracts'

const lifeLensApi: LifeLensApi = {
  captureScreen: () => ipcRenderer.invoke(IPC_CHANNELS.captureScreen),
  createRealtimeSession: () => ipcRenderer.invoke(IPC_CHANNELS.createRealtimeSession),
  executeConfirmedTool: (proposal: ToolProposal) => ipcRenderer.invoke(IPC_CHANNELS.executeConfirmedTool, proposal),
  listReminders: () => ipcRenderer.invoke(IPC_CHANNELS.listReminders),
  setPanelOpen: (open: boolean) => ipcRenderer.send(IPC_CHANNELS.setPanelOpen, open)
}

contextBridge.exposeInMainWorld('lifeLens', lifeLensApi)
