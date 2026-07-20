import type { ClassifiedIntent, GuardedTool, ToolPolicyCode, ToolPolicyDecision } from './intent'
import {
  isSearchKind,
  isSearchRecency,
  normalizeContainsText,
  normalizePeopleFilter,
  normalizePeopleLabels,
  type FileKind,
  type PeopleFilter,
  type SearchKind,
  type SearchRecency
} from './search-query'

export const IPC_CHANNELS = {
  captureScreen: 'lifelens:capture-screen',
  analyzeCapture: 'lifelens:analyze-capture',
  discardCapture: 'lifelens:discard-capture',
  listCaptureSources: 'lifelens:list-capture-sources',
  createRealtimeSession: 'lifelens:create-realtime-session',
  noteUserRequest: 'lifelens:note-user-request',
  evaluateToolRequest: 'lifelens:evaluate-tool-request',
  createPendingAction: 'lifelens:create-pending-action',
  approvePendingAction: 'lifelens:approve-pending-action',
  cancelPendingAction: 'lifelens:cancel-pending-action',
  chooseDocumentRoot: 'lifelens:choose-document-root',
  listDocumentRoots: 'lifelens:list-document-roots',
  removeDocumentRoot: 'lifelens:remove-document-root',
  beginFileSearch: 'lifelens:begin-file-search',
  cancelFileSearch: 'lifelens:cancel-file-search',
  fileSearchResolved: 'lifelens:file-search-resolved',
  getResultThumbnails: 'lifelens:get-result-thumbnails',
  cancelPhotoAnalysis: 'lifelens:cancel-photo-analysis',
  getPhotoSearchStatus: 'lifelens:get-photo-search-status',
  enablePhotoSearch: 'lifelens:enable-photo-search',
  downloadPhotoSearchModel: 'lifelens:download-photo-search-model',
  cancelPhotoSearchDownload: 'lifelens:cancel-photo-search-download',
  pausePhotoIndex: 'lifelens:pause-photo-index',
  resumePhotoIndex: 'lifelens:resume-photo-index',
  rebuildPhotoIndex: 'lifelens:rebuild-photo-index',
  setPhotoTextSearchEnabled: 'lifelens:set-photo-text-search-enabled',
  setPhotoFaceCountEnabled: 'lifelens:set-photo-face-count-enabled',
  rebuildPhotoTextIndex: 'lifelens:rebuild-photo-text-index',
  rebuildPhotoFaceIndex: 'lifelens:rebuild-photo-face-index',
  disablePhotoSearch: 'lifelens:disable-photo-search',
  setPhotoIndexOnlyWhilePluggedIn: 'lifelens:set-photo-index-only-while-plugged-in',
  setRealtimeActive: 'lifelens:set-realtime-active',
  photoSearchStatusChanged: 'lifelens:photo-search-status-changed',
  listReminders: 'lifelens:list-reminders',
  getTelegramStatus: 'lifelens:get-telegram-status',
  connectTelegram: 'lifelens:connect-telegram',
  cancelTelegramConnect: 'lifelens:cancel-telegram-connect',
  submitTelegramPassword: 'lifelens:submit-telegram-password',
  logoutTelegram: 'lifelens:logout-telegram',
  searchTelegramRecipients: 'lifelens:search-telegram-recipients',
  telegramAuthUpdate: 'lifelens:telegram-auth-update',
  setPanelOpen: 'lifelens:set-panel-open',
  resetWindowPosition: 'lifelens:reset-window-position',
  registerDroppedFile: 'lifelens:register-dropped-file',
  removeDroppedFile: 'lifelens:remove-dropped-file',

  // --- Phase 3: labelled people --------------------------------------------
  // Each channel does exactly one thing and takes the narrowest payload that
  // can express it. There is deliberately no general "people command" channel:
  // a single entry point taking an action name is one validation slip away from
  // being a way to reach anything.
  getPeopleSearchStatus: 'lifelens:get-people-search-status',
  setPeopleSearchEnabled: 'lifelens:set-people-search-enabled',
  pausePeopleScan: 'lifelens:pause-people-scan',
  resumePeopleScan: 'lifelens:resume-people-scan',
  listPeopleProfiles: 'lifelens:list-people-profiles',
  beginPeopleEnrolment: 'lifelens:begin-people-enrolment',
  /** Opens a draft that appends one reference to an already-created profile. */
  beginPersonReferenceAddition: 'lifelens:begin-person-reference-addition',
  addPeopleReference: 'lifelens:add-people-reference',
  selectPeopleFace: 'lifelens:select-people-face',
  confirmPeopleEnrolment: 'lifelens:confirm-people-enrolment',
  cancelPeopleEnrolment: 'lifelens:cancel-people-enrolment',
  renamePeopleProfile: 'lifelens:rename-people-profile',
  rescanPeopleProfile: 'lifelens:rescan-people-profile',
  deletePeopleProfile: 'lifelens:delete-people-profile',
  deleteAllPeopleData: 'lifelens:delete-all-people-data',
  peopleSearchStatusChanged: 'lifelens:people-search-status-changed'
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

export interface ScreenReasoningSummary {
  sourceCaptureId: string
  summary: string
  dates: string[]
  links: string[]
  risks: string[]
  nextActions: string[]
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
  /** One to three topic words. The model never supplies a folder or a path. */
  queryTerms: string
  kind?: SearchKind
  recency?: SearchRecency
  /** Up to three short visual concepts copied from the user's request. */
  concepts?: string[]
  /** Words or a number expected to appear inside the image itself. */
  containsText?: string
  /** How many visible faces the photo should contain. Counting only, never identity. */
  people?: PeopleFilter
  /** Names of people the user has labelled. Never a profile id; resolved in main only. */
  peopleLabels?: string[]
}

export interface OpenFileInput {
  resultId: string
}

export interface AnalyzePhotoInput {
  /** An opaque identifier from a previous approved-folder search. */
  resultId: string
  /** The user's current question about the selected photo. */
  question?: string
}

/**
 * One downscaled image the user explicitly approved for this Realtime turn.
 * Built in the main process from a revalidated approved-folder path.
 */
export interface ApprovedImagePayload {
  resultId: string
  name: string
  dataUrl: string
  mimeType: 'image/jpeg'
  width: number
  height: number
}

export type ThumbnailStatus = 'ok' | 'unsupported' | 'unavailable' | 'too_large'

/** A local-only preview. Thumbnails are never sent to the model. */
export interface ResultThumbnail {
  resultId: string
  status: ThumbnailStatus
  dataUrl?: string
  width?: number
  height?: number
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

export interface SendTelegramAttachmentInput {
  recipientResultId: string
  fileResultId: string
  caption?: string
}

export type AttachmentMediaKind = 'photo' | 'document'

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

/** A trusted local result. Only the renderer UI ever sees these fields. */
export interface DocumentSearchResult {
  id: string
  rootId: string
  name: string
  relativePath: string
  modifiedAt: string
  kind: FileKind
  /** Main-authored bounded explanation; never a raw score. */
  reason?: string
}

/**
 * The only file shape that may cross the network to the model: an ordinal, a
 * filename, and a coarse age. No identifier, root, or path is ever included.
 */
export interface CompactSearchResult {
  ordinal: number
  name: string
  modifiedAgo: string
  reason?: string
}

export type PhotoSearchState = 'off' | 'consent_required' | 'downloading' | 'verifying' | 'indexing' | 'paused' | 'ready' | 'error' | 'rebuild_required'

export interface PhotoSearchStatus {
  state: PhotoSearchState
  enabled: boolean
  modelInstalled: boolean
  modelDownloadBytes: number
  downloadedBytes: number
  indexed: number
  total: number
  failed: number
  skipped: number
  lastIndexedAt?: string
  onlyWhilePluggedIn: boolean
  powerStateKnown: boolean
  onBattery: boolean
  message?: string

  // --- Phase 2 -------------------------------------------------------------
  /** Reported separately from visual indexing, because they finish separately. */
  textSearchEnabled: boolean
  faceCountEnabled: boolean
  extrasInstalled: boolean
  extrasDownloadBytes: number
  /** Images whose text has been read. Compare against `total`. */
  textIndexed: number
  /** Images whose visible faces have been counted. Compare against `total`. */
  faceScanned: number
}

// --- Phase 3: labelled people ----------------------------------------------

/**
 * Why labelled-person matching is or is not producing answers.
 *
 * Deliberately more granular than "on/off". Every one of these states leads to
 * different, honest wording in the UI, and collapsing them would mean telling
 * someone "no matches" when the truth is "the scan has not started", "your
 * profiles could not be decrypted", or "the model is not installed". Those are
 * different problems with different fixes and only one of them is about the
 * photos.
 */
export const PEOPLE_SEARCH_STATES = [
  'off',
  'model_required',
  'downloading',
  'no_profiles',
  'not_started',
  'scanning',
  'partially_checked',
  'complete',
  'paused',
  'profile_store_unavailable'
] as const
export type PeopleSearchState = (typeof PEOPLE_SEARCH_STATES)[number]

/**
 * One enrolled person, as the renderer is allowed to see them.
 *
 * The id is here because the settings UI has to be able to say *which* profile
 * a Rename or Delete applies to, and there is no safer handle than an opaque
 * one. It is inert everywhere else: it is not accepted as a search term, it is
 * not accepted as a Realtime tool argument, and main resolves search requests
 * from labels only. See the tests in people-ipc.test.ts.
 *
 * Note what is absent: embeddings, reference paths, similarity values, face
 * previews, and the case-folded label used for uniqueness.
 */
export interface PeopleProfileView {
  id: string
  label: string
  referenceCount: number
  /** Whether this profile can currently produce matches. */
  status: 'ready' | 'needs_rescan' | 'needs_reenrolment'
  createdAt: string
  updatedAt: string
  /** Photos checked against this profile, out of `total` in PeopleSearchStatus. */
  checked: number
  /** Photos that reached `likely` or `possible`. Never a score. */
  matched: number
}

export interface PeopleSearchStatus {
  state: PeopleSearchState
  enabled: boolean
  modelInstalled: boolean
  modelDownloadBytes: number
  downloadedBytes: number
  paused: boolean
  /** Eligible photos in the library. The denominator for every profile. */
  total: number
  profiles: PeopleProfileView[]
  /** App-authored. Never quotes a label, a path, or an error from disk. */
  message?: string
}

/** A face offered for selection during enrolment. Bounded and temporary. */
export interface PeopleFaceCandidateView {
  candidateId: string
  /** A small locally-rendered crop. Never leaves the device. */
  previewDataUrl: string
  /** True when this face passed the quality gates and may be chosen. */
  selectable: boolean
  /** App-authored reason it cannot be chosen, when it cannot. */
  note?: string
}

/** The live state of one enrolment draft, as the renderer sees it. */
export interface PeopleEnrolmentView {
  enrolmentId: string
  label: string
  acceptedReferences: number
  requiredReferences: number
  maximumReferences: number
  /** Present while the user must point at one face in a multi-face photo. */
  candidates?: PeopleFaceCandidateView[]
  /** True once enough references are accepted for Create to be offered. */
  readyToCreate: boolean
  /** App-authored rejection for the most recent attempt, when one failed. */
  lastRejection?: string
}

export type FileSearchOrigin = 'model' | 'user'

export interface FileSearchRequest {
  queryTerms: string
  kind?: SearchKind
  recency?: SearchRecency
  concepts?: string[]
  containsText?: string
  people?: PeopleFilter
  peopleLabels?: string[]
  /** The Realtime function call awaiting a terminal result, when model-driven. */
  callId?: string
  origin: FileSearchOrigin
}

export interface FileSearchResults {
  results: DocumentSearchResult[]
  compactResults: CompactSearchResult[]
  /** True when nothing matched by name and recent files are offered instead. */
  fallback: boolean
  message: string
}

export type FileSearchOutcome =
  | ({ status: 'completed' } & FileSearchResults)
  | { status: 'awaiting_folder'; pendingId: string }
  | { status: 'needs_confirmation'; input: SearchDocumentsInput }
  | { status: 'failed'; message: string }

export type PendingSearchStatus = 'completed' | 'declined' | 'expired' | 'superseded' | 'failed'

/** Emitted by main when a held search reaches exactly one terminal state. */
export type PendingSearchResolution =
  | ({ status: 'completed'; callId?: string } & FileSearchResults)
  | { status: Exclude<PendingSearchStatus, 'completed'>; callId?: string; message: string }

export interface SavedContextRecord {
  id: string
  label: string
  sourceContext: SourceContext
  createdAt: string
}

export const TOOL_NAMES = ['create_reminder', 'search_documents', 'open_file', 'open_url', 'save_context', 'send_telegram_message', 'send_telegram_attachment', 'analyze_photo'] as const
export type ToolName = (typeof TOOL_NAMES)[number]

export interface ToolArguments {
  create_reminder: ReminderInput
  search_documents: SearchDocumentsInput
  open_file: OpenFileInput
  open_url: OpenUrlInput
  save_context: SaveContextInput
  send_telegram_message: SendTelegramMessageInput
  send_telegram_attachment: SendTelegramAttachmentInput
  analyze_photo: AnalyzePhotoInput
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
  code?: ToolPolicyCode
  reminder?: ReminderRecord
  /** Trusted local results for the renderer UI only; never sent to the model. */
  searchResults?: DocumentSearchResult[]
  /** The redacted result view that may be returned to the model. */
  compactResults?: CompactSearchResult[]
  searchFallback?: boolean
  openedResultId?: string
  /** The one approved image the renderer may hand to the Realtime session. */
  analysisImage?: ApprovedImagePayload
  openedUrl?: string
  savedContext?: SavedContextRecord
  telegramSent?: boolean
}

interface PendingActionBase {
  approvalId: string
  actionType: ToolName
  createdAt: string
  expiresAt: string
}

export type PendingActionPreview =
  | (PendingActionBase & { actionType: 'create_reminder'; title: string; dueAt: string; sourceContextSummary: string })
  | (PendingActionBase & { actionType: 'search_documents'; folderLabel: string; query: string })
  | (PendingActionBase & { actionType: 'open_file'; fileName: string; relativePath: string; folderLabel: string; source?: TrustedSourceKind })
  | (PendingActionBase & { actionType: 'open_url'; url: string; domain: string })
  | (PendingActionBase & { actionType: 'save_context'; label: string; summary: string })
  | (PendingActionBase & {
    actionType: 'analyze_photo'
    fileName: string
    relativePath: string
    folderLabel: string
    question: string
    source?: TrustedSourceKind
    /** A trusted local preview built in main, not supplied by the renderer. */
    previewDataUrl?: string
  })
  | (PendingActionBase & {
    actionType: 'send_telegram_message'
    account: TelegramAccount
    recipient: Pick<TelegramRecipient, 'displayName' | 'username'>
    message: string
  })
  | (PendingActionBase & {
    actionType: 'send_telegram_attachment'
    account: TelegramAccount
    recipient: Pick<TelegramRecipient, 'displayName' | 'username' | 'kind'>
    fileName: string
    mediaKind: AttachmentMediaKind
    fileSizeBytes: number
    fileTypeLabel: string
    caption?: string
    source?: TrustedSourceKind
    /** A main-built local preview. It is never included in model events. */
    previewDataUrl?: string
  })

export interface LifeLensApi {
  listCaptureSources: () => Promise<CaptureSource[]>
  captureScreen: (sourceId?: string) => Promise<CaptureResult>
  analyzeCapture: (captureId: string) => Promise<ScreenReasoningSummary>
  discardCapture: () => Promise<void>
  createRealtimeSession: () => Promise<RealtimeSessionCredential>
  noteUserRequest: (request: string) => Promise<ClassifiedIntent>
  evaluateToolRequest: (toolName: GuardedTool) => Promise<ToolPolicyDecision>
  createPendingAction: (proposal: ToolProposal) => Promise<PendingActionPreview>
  approvePendingAction: (approvalId: string) => Promise<ToolExecutionResult>
  cancelPendingAction: (approvalId: string) => Promise<void>
  chooseDocumentRoot: () => Promise<ApprovedDocumentRoot | undefined>
  listDocumentRoots: () => Promise<ApprovedDocumentRoot[]>
  removeDocumentRoot: (rootId: string) => Promise<boolean>
  beginFileSearch: (request: FileSearchRequest) => Promise<FileSearchOutcome>
  cancelFileSearch: () => Promise<void>
  getResultThumbnails: (resultIds: string[]) => Promise<ResultThumbnail[]>
  cancelPhotoAnalysis: () => Promise<void>
  getPhotoSearchStatus: () => Promise<PhotoSearchStatus>
  enablePhotoSearch: () => Promise<PhotoSearchStatus>
  downloadPhotoSearchModel: () => Promise<PhotoSearchStatus>
  cancelPhotoSearchDownload: () => Promise<PhotoSearchStatus>
  pausePhotoIndex: () => Promise<PhotoSearchStatus>
  resumePhotoIndex: () => Promise<PhotoSearchStatus>
  rebuildPhotoIndex: () => Promise<PhotoSearchStatus>
  setPhotoTextSearchEnabled: (enabled: boolean) => Promise<PhotoSearchStatus>
  setPhotoFaceCountEnabled: (enabled: boolean) => Promise<PhotoSearchStatus>
  rebuildPhotoTextIndex: () => Promise<PhotoSearchStatus>
  rebuildPhotoFaceIndex: () => Promise<PhotoSearchStatus>
  disablePhotoSearch: () => Promise<PhotoSearchStatus>
  setPhotoIndexOnlyWhilePluggedIn: (enabled: boolean) => Promise<PhotoSearchStatus>
  setRealtimeActive: (active: boolean) => Promise<void>
  onPhotoSearchStatusChanged: (listener: (status: PhotoSearchStatus) => void) => () => void
  onFileSearchResolved: (listener: (resolution: PendingSearchResolution) => void) => () => void
  listReminders: () => Promise<ReminderRecord[]>
  getTelegramStatus: () => Promise<TelegramStatus>
  connectTelegram: () => Promise<TelegramStatus>
  cancelTelegramConnect: () => Promise<TelegramStatus>
  submitTelegramPassword: (password: string) => Promise<TelegramStatus>
  logoutTelegram: () => Promise<TelegramStatus>
  searchTelegramRecipients: (query: string) => Promise<TelegramRecipient[]>
  onTelegramAuthUpdate: (listener: (status: TelegramStatus) => void) => () => void
  setPanelOpen: (open: boolean) => void
  resetWindowPosition: () => Promise<void>
  /**
   * Resolves a dropped `File` to a path inside preload and hands it to main.
   * The path is never returned to renderer application code.
   */
  registerDroppedFile: (file: File) => Promise<DroppedFileDescriptor>
  removeDroppedFile: (droppedId: string) => Promise<void>

  // --- Phase 3: labelled people --------------------------------------------
  // Every call below is app-authored and narrow. Note what the renderer never
  // supplies: a photo path, a model path, an embedding, a threshold, or a
  // similarity. Reference photos are named by trusted ids the renderer already
  // holds from an approved-root search result or a live dropped file.
  getPeopleSearchStatus: () => Promise<PeopleSearchStatus>
  setPeopleSearchEnabled: (enabled: boolean) => Promise<PeopleSearchStatus>
  pausePeopleScan: () => Promise<PeopleSearchStatus>
  resumePeopleScan: () => Promise<PeopleSearchStatus>
  listPeopleProfiles: () => Promise<PeopleProfileView[]>
  /** Opens a draft. Creates nothing until `confirmPeopleEnrolment`. */
  beginPeopleEnrolment: (label: string) => Promise<PeopleEnrolmentView>
  /** Opens a draft that appends one reference to an existing profile. */
  beginPersonReferenceAddition: (profileId: string) => Promise<PeopleEnrolmentView>
  addPeopleReference: (enrolmentId: string, trustedId: string) => Promise<PeopleEnrolmentView>
  selectPeopleFace: (enrolmentId: string, candidateId: string) => Promise<PeopleEnrolmentView>
  /** The explicit act that creates a profile. Nothing else does. */
  confirmPeopleEnrolment: (enrolmentId: string) => Promise<PeopleProfileView>
  cancelPeopleEnrolment: (enrolmentId: string) => Promise<void>
  renamePeopleProfile: (profileId: string, label: string) => Promise<PeopleProfileView>
  rescanPeopleProfile: (profileId: string) => Promise<PeopleSearchStatus>
  deletePeopleProfile: (profileId: string) => Promise<PeopleSearchStatus>
  deleteAllPeopleData: () => Promise<PeopleSearchStatus>
  onPeopleSearchStatusChanged: (listener: (status: PeopleSearchStatus) => void) => () => void
}

/**
 * Everything the renderer may know about a dropped file. Deliberately carries
 * no path — not even a relative one.
 */
export interface DroppedFileDescriptor {
  readonly droppedId: string
  readonly fileName: string
  readonly fileTypeLabel: string
  readonly sizeBytes: number
  readonly mediaKind: AttachmentMediaKind
  /** When the temporary record lapses, so the card can say so plainly. */
  readonly expiresAt: string
  /**
   * A bounded, main-rendered preview for images only. Documents get an
   * app-authored glyph in the renderer; their contents are never read.
   */
  readonly thumbnailDataUrl?: string
}

/**
 * Which kind of trust produced a file in a confirmation card.
 *
 * Absent means an approved-folder search result, which is every existing
 * caller. Only main ever sets this — the renderer cannot choose its own trust.
 */
export type TrustedSourceKind = 'approved-folder' | 'dropped-file'

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

function parseSearchDocumentsInput(value: unknown, allowedExtra: readonly string[] = []): SearchDocumentsInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Search arguments must be an object.')
  }

  assertOnlyKeys(
    value,
    ['queryTerms', 'kind', 'recency', 'concepts', 'containsText', 'people', 'peopleLabels', ...allowedExtra],
    'Search arguments'
  )

  const input: SearchDocumentsInput = { queryTerms: requiredString(value.queryTerms, 'queryTerms', 250) }

  if (value.kind !== undefined) {
    if (!isSearchKind(value.kind)) {
      throw new PayloadValidationError('kind must be document, photo, screenshot, or any.')
    }
    input.kind = value.kind
  }

  if (value.recency !== undefined) {
    if (!isSearchRecency(value.recency)) {
      throw new PayloadValidationError('recency must be latest or any.')
    }
    input.recency = value.recency
  }

  if (value.concepts !== undefined) {
    if (!Array.isArray(value.concepts) || value.concepts.length === 0 || value.concepts.length > 3) {
      throw new PayloadValidationError('concepts must contain one to three short concepts.')
    }
    input.concepts = value.concepts.map((concept) => {
      const parsed = requiredString(concept, 'concepts', 64)
      if (/[\\/]|^[a-f0-9]{20,}$/i.test(parsed) || /[\[\]{}]/.test(parsed)) {
        throw new PayloadValidationError('concepts must contain natural-language descriptions only.')
      }
      return parsed
    })
  }

  // Phase-2 fields reuse the query normalizer's own validators, so the rules
  // live in exactly one place and the tool boundary cannot drift from them.
  if (value.containsText !== undefined) {
    try {
      const containsText = normalizeContainsText(value.containsText)
      if (containsText.length > 0) {
        input.containsText = containsText
      }
    } catch (error) {
      throw new PayloadValidationError(
        error instanceof Error ? error.message : 'contains_text is not valid.'
      )
    }
  }

  if (value.people !== undefined) {
    try {
      const people = normalizePeopleFilter(value.people)
      if (people) {
        input.people = people
      }
    } catch (error) {
      throw new PayloadValidationError(error instanceof Error ? error.message : 'people is not valid.')
    }
  }

  if (value.peopleLabels !== undefined) {
    try {
      const peopleLabels = normalizePeopleLabels(value.peopleLabels)
      if (peopleLabels.length > 0) {
        input.peopleLabels = peopleLabels
      }
    } catch (error) {
      throw new PayloadValidationError(error instanceof Error ? error.message : 'peopleLabels is not valid.')
    }
  }

  return input
}

/** Validates a renderer-supplied search request at the main-process boundary. */
export function parseFileSearchRequest(value: unknown): FileSearchRequest {
  if (!isRecord(value)) {
    throw new PayloadValidationError('A file search request must be an object.')
  }

  if (value.origin !== 'model' && value.origin !== 'user') {
    throw new PayloadValidationError('A file search request must state a model or user origin.')
  }

  const input = parseSearchDocumentsInput(value, ['origin', 'callId'])
  return {
    ...input,
    origin: value.origin,
    callId: value.callId === undefined ? undefined : requiredString(value.callId, 'callId', 250)
  }
}

function parseOpenFileInput(value: unknown): OpenFileInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Open-file arguments must be an object.')
  }

  return { resultId: requiredString(value.resultId, 'resultId', 250) }
}

function parseAnalyzePhotoInput(value: unknown): AnalyzePhotoInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Photo-analysis arguments must be an object.')
  }

  // Only an opaque result identifier and a question are accepted. Paths,
  // filenames, and image bytes are resolved in main and never taken from here.
  return {
    resultId: requiredString(value.resultId, 'resultId', 250),
    question: value.question === undefined ? undefined : requiredString(value.question, 'question', 1_000)
  }
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

function parseSendTelegramAttachmentInput(value: unknown): SendTelegramAttachmentInput {
  if (!isRecord(value)) {
    throw new PayloadValidationError('Telegram attachment arguments must be an object.')
  }
  assertOnlyKeys(value, ['recipientResultId', 'fileResultId', 'caption'], 'Telegram attachment arguments')

  return {
    recipientResultId: requiredString(value.recipientResultId, 'recipientResultId', 250),
    fileResultId: requiredString(value.fileResultId, 'fileResultId', 250),
    caption: value.caption === undefined ? undefined : optionalBoundedString(value.caption, 'caption', 1_024)
  }
}

function optionalBoundedString(value: unknown, field: string, maximum: number): string {
  if (typeof value !== 'string' || value.length > maximum) {
    throw new PayloadValidationError(`${field} must be a string no longer than ${maximum} characters.`)
  }
  return value
}

function assertOnlyKeys(value: Record<string, unknown>, allowed: readonly string[], label: string): void {
  const allowedKeys = new Set(allowed)
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw new PayloadValidationError(`${label} contains unsupported properties.`)
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
    case 'send_telegram_attachment':
      return { ...common, toolName, arguments: parseSendTelegramAttachmentInput(value.arguments) }
    case 'analyze_photo':
      return { ...common, toolName, arguments: parseAnalyzePhotoInput(value.arguments) }
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
