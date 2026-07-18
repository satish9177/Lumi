export const IPC_CHANNELS = {
  captureScreen: 'lifelens:capture-screen',
  listCaptureSources: 'lifelens:list-capture-sources',
  createRealtimeSession: 'lifelens:create-realtime-session',
  executeConfirmedTool: 'lifelens:execute-confirmed-tool',
  chooseDocumentRoot: 'lifelens:choose-document-root',
  listDocumentRoots: 'lifelens:list-document-roots',
  listReminders: 'lifelens:list-reminders',
  getTelegramStatus: 'lifelens:get-telegram-status',
  connectTelegram: 'lifelens:connect-telegram',
  cancelTelegramConnect: 'lifelens:cancel-telegram-connect',
  submitTelegramPassword: 'lifelens:submit-telegram-password',
  logoutTelegram: 'lifelens:logout-telegram',
  searchTelegramRecipients: 'lifelens:search-telegram-recipients',
  telegramAuthUpdate: 'lifelens:telegram-auth-update',
  setPanelOpen: 'lifelens:set-panel-open'
} as const

export const COMPANION_STATES = ['idle', 'listening', 'thinking', 'speaking', 'success', 'error'] as const
export type CompanionState = (typeof COMPANION_STATES)[number]

export type RealtimeMode = 'live' | 'mock'

export type CaptureSourceKind = 'screen' | 'window'

export interface CaptureSource {
  id: string
  label: string
  kind: CaptureSourceKind
  thumbnailDataUrl: string
}

export interface CaptureResult {
  id: string
  sourceId: string
  sourceKind: CaptureSourceKind
  label: string
  dataUrl: string
  mimeType: 'image/png' | 'image/jpeg'
  width: number
  height: number
  capturedAt: string
}

export interface RealtimeSessionCredential {
  mode: RealtimeMode
  model: string
  token?: string
  expiresAt?: string
}

export type SignalKind = 'date' | 'link' | 'next_action'

export interface ExtractedSignal {
  kind: SignalKind
  label: string
  value: string
}

export interface Explanation {
  summary: string
  sourceCaptureId: string
  signals: ExtractedSignal[]
}

export interface SourceContext {
  captureId: string
  summary: string
  capturedAt: string
  signals: ExtractedSignal[]
}

export interface ReminderInput {
  title: string
  dueAt: string
  sourceContext: SourceContext
}

export interface SearchDocumentsInput {
  rootId: string
  query: string
}

export interface OpenFileInput {
  resultId: string
}

export interface OpenUrlInput {
  url: string
}

export interface SaveContextInput {
  label: string
  sourceContext: SourceContext
}

export interface SendTelegramMessageInput {
  recipientResultId: string
  message: string
}

export interface TelegramAccount {
  displayName: string
  username?: string
}

export interface TelegramRecipient {
  resultId: string
  displayName: string
  username?: string
  kind: 'user' | 'group' | 'channel'
  recentRank: number
}

export type TelegramConnectionState = 'disconnected' | 'connecting' | 'awaiting_2fa' | 'connected' | 'error'

export interface TelegramStatus {
  state: TelegramConnectionState
  account?: TelegramAccount
  qrUrl?: string
  expiresAt?: string
  message?: string
}

export interface ApprovedDocumentRoot {
  id: string
  label: string
}

export interface DocumentSearchResult {
  id: string
  rootId: string
  name: string
  relativePath: string
  modifiedAt: string
}

export interface SavedContextRecord {
  id: string
  label: string
  sourceContext: SourceContext
  createdAt: string
}

export const TOOL_NAMES = ['create_reminder', 'search_documents', 'open_file', 'open_url', 'save_context', 'send_telegram_message'] as const
export type ToolName = (typeof TOOL_NAMES)[number]

export interface ToolArguments {
  create_reminder: ReminderInput
  search_documents: SearchDocumentsInput
  open_file: OpenFileInput
  open_url: OpenUrlInput
  save_context: SaveContextInput
  send_telegram_message: SendTelegramMessageInput
}

export type ToolProposal<T extends ToolName = ToolName> = {
  [K in T]: {
    id: string
    toolName: K
    reason: string
    arguments: ToolArguments[K]
    requiresConfirmation: true
    callId?: string
  }
}[T]

export interface ReminderRecord extends ReminderInput {
  id: string
  createdAt: string
}

export interface ToolExecutionResult {
  ok: boolean
  message: string
  reminder?: ReminderRecord
  searchResults?: DocumentSearchResult[]
  openedResultId?: string
  openedUrl?: string
  savedContext?: SavedContextRecord
  telegramSent?: boolean
}

export interface LifeLensApi {
  listCaptureSources: () => Promise<CaptureSource[]>
  captureScreen: (sourceId?: string) => Promise<CaptureResult>
  createRealtimeSession: () => Promise<RealtimeSessionCredential>
  executeConfirmedTool: (proposal: ToolProposal) => Promise<ToolExecutionResult>
  chooseDocumentRoot: () => Promise<ApprovedDocumentRoot | undefined>
  listDocumentRoots: () => Promise<ApprovedDocumentRoot[]>
  listReminders: () => Promise<ReminderRecord[]>
  getTelegramStatus: () => Promise<TelegramStatus>
  connectTelegram: () => Promise<TelegramStatus>
  cancelTelegramConnect: () => Promise<TelegramStatus>
  submitTelegramPassword: (password: string) => Promise<TelegramStatus>
  logoutTelegram: () => Promise<TelegramStatus>
  searchTelegramRecipients: (query: string) => Promise<TelegramRecipient[]>
  onTelegramAuthUpdate: (listener: (status: TelegramStatus) => void) => () => void
  setPanelOpen: (open: boolean) => void
}

export class PayloadValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PayloadValidationError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function requiredString(value: unknown, field: string, maximum = 4_000): string {
  if (typeof value !== 'string' || value.trim().length === 0 || value.length > maximum) {
    throw new PayloadValidationError(`${field} must be a non-empty string shorter than ${maximum} characters.`)
  }

  return value.trim()
}

function parseSignals(value: unknown): ExtractedSignal[] {
  if (!Array.isArray(value) || value.length > 20) {
    throw new PayloadValidationError('sourceContext.signals must be a short list.')
  }

  return value.map((signal) => {
    if (!isRecord(signal)) {
      throw new PayloadValidationError('A source signal must be an object.')
    }

    const kind = requiredString(signal.kind, 'sourceContext.signals.kind', 32)
    if (kind !== 'date' && kind !== 'link' && kind !== 'next_action') {
      throw new PayloadValidationError('sourceContext.signals.kind is not supported.')
    }

    return {
      kind,
      label: requiredString(signal.label, 'sourceContext.signals.label', 250),
      value: requiredString(signal.value, 'sourceContext.signals.value', 1_000)
    } as ExtractedSignal
  })
}

function parseSourceContext(value: unknown): SourceContext {
  if (!isRecord(value)) {
    throw new PayloadValidationError('sourceContext must be an object.')
  }

  return {
    captureId: requiredString(value.captureId, 'sourceContext.captureId', 250),
    summary: requiredString(value.summary, 'sourceContext.summary', 4_000),
    capturedAt: requiredString(value.capturedAt, 'sourceContext.capturedAt', 100),
    signals: parseSignals(value.signals)
  }
}

function parseReminderInput(value: unknown): ReminderInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Reminder arguments must be an object.')
  }

  const dueAt = requiredString(value.dueAt, 'dueAt', 100)
  if (Number.isNaN(Date.parse(dueAt))) {
    throw new PayloadValidationError('dueAt must be a valid date-time string.')
  }

  return {
    title: requiredString(value.title, 'title', 250),
    dueAt: new Date(dueAt).toISOString(),
    sourceContext: parseSourceContext(value.sourceContext)
  }
}

function parseSearchDocumentsInput(value: unknown): SearchDocumentsInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Search arguments must be an object.')
  }

  return {
    rootId: requiredString(value.rootId, 'rootId', 250),
    query: requiredString(value.query, 'query', 250)
  }
}

function parseOpenFileInput(value: unknown): OpenFileInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Open-file arguments must be an object.')
  }

  return { resultId: requiredString(value.resultId, 'resultId', 250) }
}

function parseOpenUrlInput(value: unknown): OpenUrlInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Open-URL arguments must be an object.')
  }

  const url = requiredString(value.url, 'url', 2_000)
  let protocol: string
  try {
    protocol = new URL(url).protocol
  } catch {
    throw new PayloadValidationError('url must be a valid URL.')
  }

  if (protocol !== 'https:' && protocol !== 'http:') {
    throw new PayloadValidationError('Only http and https URLs may be opened.')
  }

  return { url }
}

function parseSaveContextInput(value: unknown): SaveContextInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Save-context arguments must be an object.')
  }

  return {
    label: requiredString(value.label, 'label', 250),
    sourceContext: parseSourceContext(value.sourceContext)
  }
}

function parseSendTelegramMessageInput(value: unknown): SendTelegramMessageInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Telegram message arguments must be an object.')
  }

  return {
    recipientResultId: requiredString(value.recipientResultId, 'recipientResultId', 250),
    message: requiredString(value.message, 'message', 4_096)
  }
}

export function parseToolProposal(value: unknown): ToolProposal {
  if (!isRecord(value)) {
    throw new PayloadValidationError('A tool proposal must be an object.')
  }

  const id = requiredString(value.id, 'id', 250)
  const reason = requiredString(value.reason, 'reason', 1_000)
  const callId = value.callId === undefined ? undefined : requiredString(value.callId, 'callId', 250)

  if (value.requiresConfirmation !== true) {
    throw new PayloadValidationError('Tool proposals must explicitly require confirmation.')
  }

  const toolName = requiredString(value.toolName, 'toolName', 100)
  const common = { id, reason, requiresConfirmation: true as const, callId }

  switch (toolName) {
    case 'create_reminder':
      return { ...common, toolName, arguments: parseReminderInput(value.arguments) }
    case 'search_documents':
      return { ...common, toolName, arguments: parseSearchDocumentsInput(value.arguments) }
    case 'open_file':
      return { ...common, toolName, arguments: parseOpenFileInput(value.arguments) }
    case 'open_url':
      return { ...common, toolName, arguments: parseOpenUrlInput(value.arguments) }
    case 'save_context':
      return { ...common, toolName, arguments: parseSaveContextInput(value.arguments) }
    case 'send_telegram_message':
      return { ...common, toolName, arguments: parseSendTelegramMessageInput(value.arguments) }
    default:
      throw new PayloadValidationError('The requested tool is not allowed.')
  }
}

export function extractSignals(text: string): ExtractedSignal[] {
  const signals: ExtractedSignal[] = []
  const links = text.match(/https?:\/\/[^\s)\]}>,]+/gi) ?? []
  for (const url of [...new Set(links)].slice(0, 5)) {
    signals.push({ kind: 'link', label: 'Link', value: url })
  }

  const dates = text.match(/\b(?:today|tomorrow|(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b/gi) ?? []
  for (const date of [...new Set(dates)].slice(0, 5)) {
    signals.push({ kind: 'date', label: 'Date', value: date })
  }

  const nextAction = text.match(/(?:please|remember to|next step(?: is)?|prepare to)\s+([^.!?]{4,180})/i)
  if (nextAction?.[1]) {
    signals.push({ kind: 'next_action', label: 'Next action', value: nextAction[1].trim() })
  }

  return signals
}
