import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { dirname, join } from 'node:path'
import type {
  ApprovedDocumentRoot,
  DocumentSearchResult,
  ReminderInput,
  ReminderRecord,
  SaveContextInput,
  SavedContextRecord
} from '../../shared/contracts'
import type { FileKind } from '../../shared/search-query'

export interface StoredDocumentRoot extends ApprovedDocumentRoot {
  path: string
  createdAt: string
}

/** A result awaiting storage. Absolute paths never leave the main process. */
export type SearchResultInput = Omit<DocumentSearchResult, 'id'> & { absolutePath: string }

interface StoredSearchResult extends Omit<DocumentSearchResult, 'kind'> {
  /** Optional so a state file written before kinds existed still loads. */
  kind?: FileKind
  absolutePath: string
  createdAt: string
}

interface LocalState {
  reminders: ReminderRecord[]
  documentRoots: StoredDocumentRoot[]
  searchResults: StoredSearchResult[]
  savedContexts: SavedContextRecord[]
}

const EMPTY_STATE = (): LocalState => ({ reminders: [], documentRoots: [], searchResults: [], savedContexts: [] })
const MAX_STORED_SEARCH_RESULTS = 100

export class LocalStore {
  private readonly statePath: string

  constructor(userDataPath: string) {
    this.statePath = join(userDataPath, 'lifelens-state.json')
  }

  async listReminders(): Promise<ReminderRecord[]> {
    const state = await this.readState()
    return [...state.reminders].sort((left, right) => left.dueAt.localeCompare(right.dueAt))
  }

  async addReminder(input: ReminderInput): Promise<ReminderRecord> {
    const state = await this.readState()
    const reminder: ReminderRecord = {
      ...input,
      id: randomUUID(),
      createdAt: new Date().toISOString()
    }

    state.reminders.unshift(reminder)
    await this.writeState(state)
    return reminder
  }

  async listDocumentRoots(): Promise<ApprovedDocumentRoot[]> {
    const state = await this.readState()
    return state.documentRoots.map(toPublicDocumentRoot)
  }

  /** Main-process only: includes the approved absolute path. */
  async listStoredDocumentRoots(): Promise<StoredDocumentRoot[]> {
    const state = await this.readState()
    return [...state.documentRoots]
  }

  async addDocumentRoot(path: string, label: string): Promise<ApprovedDocumentRoot> {
    const state = await this.readState()
    const normalizedPath = path.trim()
    const existing = state.documentRoots.find((root) => root.path.toLocaleLowerCase() === normalizedPath.toLocaleLowerCase())
    if (existing) {
      return toPublicDocumentRoot(existing)
    }

    const root: StoredDocumentRoot = {
      id: randomUUID(),
      path: normalizedPath,
      label: label.trim() || 'Approved folder',
      createdAt: new Date().toISOString()
    }
    state.documentRoots.unshift(root)
    await this.writeState(state)
    return toPublicDocumentRoot(root)
  }

  async getDocumentRoot(rootId: string): Promise<StoredDocumentRoot | undefined> {
    const state = await this.readState()
    return state.documentRoots.find((root) => root.id === rootId)
  }

  /**
   * Replaces the previous result set. Ordinals shown to the user always refer
   * to the most recent search, so a stale result can never be reopened by
   * position.
   */
  async saveSearchResults(results: readonly SearchResultInput[]): Promise<DocumentSearchResult[]> {
    const state = await this.readState()
    const createdAt = new Date().toISOString()
    const stored = results.slice(0, MAX_STORED_SEARCH_RESULTS).map<StoredSearchResult>((result) => ({
      id: randomUUID(),
      rootId: result.rootId,
      name: result.name,
      relativePath: result.relativePath,
      modifiedAt: result.modifiedAt,
      kind: result.kind,
      absolutePath: result.absolutePath,
      createdAt
    }))

    state.searchResults = stored
    await this.writeState(state)
    return stored.map(toPublicSearchResult)
  }

  async getSearchResult(resultId: string): Promise<StoredSearchResult | undefined> {
    const state = await this.readState()
    return state.searchResults.find((result) => result.id === resultId)
  }

  async addSavedContext(input: SaveContextInput): Promise<SavedContextRecord> {
    const state = await this.readState()
    const context: SavedContextRecord = {
      id: randomUUID(),
      label: input.label,
      sourceContext: input.sourceContext,
      createdAt: new Date().toISOString()
    }

    state.savedContexts.unshift(context)
    await this.writeState(state)
    return context
  }

  private async readState(): Promise<LocalState> {
    try {
      const raw = await readFile(this.statePath, 'utf8')
      return normalizeState(JSON.parse(raw) as unknown)
    } catch (error: unknown) {
      if (isNodeError(error, 'ENOENT')) {
        return EMPTY_STATE()
      }

      if (error instanceof SyntaxError) {
        return EMPTY_STATE()
      }

      throw error
    }
  }

  private async writeState(state: LocalState): Promise<void> {
    await mkdir(dirname(this.statePath), { recursive: true })
    await writeFile(this.statePath, JSON.stringify(state, null, 2), 'utf8')
  }
}

function toPublicDocumentRoot(root: StoredDocumentRoot): ApprovedDocumentRoot {
  return { id: root.id, label: root.label }
}

function toPublicSearchResult(result: StoredSearchResult): DocumentSearchResult {
  return {
    id: result.id,
    rootId: result.rootId,
    name: result.name,
    relativePath: result.relativePath,
    modifiedAt: result.modifiedAt,
    kind: result.kind ?? 'other'
  }
}

function normalizeState(value: unknown): LocalState {
  if (!isRecord(value)) {
    return EMPTY_STATE()
  }

  return {
    reminders: Array.isArray(value.reminders) ? value.reminders.filter(isReminderRecord) : [],
    documentRoots: Array.isArray(value.documentRoots) ? value.documentRoots.filter(isStoredDocumentRoot) : [],
    searchResults: Array.isArray(value.searchResults) ? value.searchResults.filter(isStoredSearchResult).slice(0, MAX_STORED_SEARCH_RESULTS) : [],
    savedContexts: Array.isArray(value.savedContexts) ? value.savedContexts.filter(isSavedContextRecord) : []
  }
}

function isNodeError(value: unknown, code: string): value is NodeJS.ErrnoException {
  return typeof value === 'object' && value !== null && 'code' in value && value.code === code
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isReminderRecord(value: unknown): value is ReminderRecord {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.dueAt === 'string' &&
    typeof value.createdAt === 'string' &&
    isRecord(value.sourceContext)
}

function isStoredDocumentRoot(value: unknown): value is StoredDocumentRoot {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.path === 'string' &&
    typeof value.label === 'string' &&
    typeof value.createdAt === 'string'
}

function isStoredSearchResult(value: unknown): value is StoredSearchResult {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.rootId === 'string' &&
    typeof value.name === 'string' &&
    typeof value.relativePath === 'string' &&
    typeof value.modifiedAt === 'string' &&
    typeof value.absolutePath === 'string' &&
    typeof value.createdAt === 'string'
}

function isSavedContextRecord(value: unknown): value is SavedContextRecord {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.label === 'string' &&
    typeof value.createdAt === 'string' &&
    isRecord(value.sourceContext)
}
