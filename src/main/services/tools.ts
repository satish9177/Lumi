import { Notification, shell } from 'electron'
import { stat } from 'node:fs/promises'
import {
  canonicalizeApprovedRoots,
  resolveApprovedDocumentPath,
  searchApprovedDocuments,
  type DocumentSearchRecord
} from '../../features/document-tools/search'
import {
  parseToolProposal,
  type DocumentSearchResult,
  type ReminderRecord,
  type ToolExecutionResult
} from '../../shared/contracts'
import { LocalStore } from './store'

const MAX_TIMER_DELAY = 2_147_000_000
const reminderTimers = new Map<string, NodeJS.Timeout>()

export async function executeConfirmedTool(store: LocalStore, rawProposal: unknown): Promise<ToolExecutionResult> {
  const proposal = parseToolProposal(rawProposal)

  switch (proposal.toolName) {
    case 'create_reminder': {
      const reminder = await store.addReminder(proposal.arguments)
      scheduleReminder(reminder)
      return {
        ok: true,
        message: `Reminder saved for ${new Date(reminder.dueAt).toLocaleString()}.`,
        reminder
      }
    }
    case 'search_documents': {
      const root = await store.getDocumentRoot(proposal.arguments.rootId)
      if (!root) {
        return { ok: false, message: 'That approved folder is no longer available. Choose a folder again.' }
      }

      const approvedRoots = await canonicalizeApprovedRoots([root.path])
      const search = await searchApprovedDocuments(approvedRoots, proposal.arguments.query, { maxDepth: 4, maxResults: 20 })
      const candidates = await toStoredSearchResults(search.results)
      candidates.sort((left, right) => right.modifiedAt.localeCompare(left.modifiedAt))
      const searchResults = await store.saveSearchResults(root.id, candidates)
      return {
        ok: true,
        message: searchResults.length === 1
          ? 'Found 1 matching file in the approved folder.'
          : `Found ${searchResults.length} matching files in the approved folder.`,
        searchResults
      }
    }
    case 'open_file': {
      const storedResult = await store.getSearchResult(proposal.arguments.resultId)
      if (!storedResult) {
        return { ok: false, message: 'That file is not a result from an approved search. Search again first.' }
      }

      const root = await store.getDocumentRoot(storedResult.rootId)
      if (!root) {
        return { ok: false, message: 'The folder that produced this result is no longer approved.' }
      }

      const approvedRoots = await canonicalizeApprovedRoots([root.path])
      const safePath = await resolveApprovedDocumentPath(storedResult.absolutePath, approvedRoots)
      if (!safePath) {
        return { ok: false, message: 'The selected result is no longer a safe file within its approved folder.' }
      }

      const failure = await shell.openPath(safePath)
      if (failure) {
        return { ok: false, message: `Windows could not open the selected file: ${failure}` }
      }

      return { ok: true, message: `Opened ${storedResult.name}.`, openedResultId: storedResult.id }
    }
    case 'open_url': {
      await shell.openExternal(proposal.arguments.url)
      return { ok: true, message: 'Opened the confirmed link in your default browser.', openedUrl: proposal.arguments.url }
    }
    case 'save_context': {
      const savedContext = await store.addSavedContext(proposal.arguments)
      return { ok: true, message: `Saved context: ${savedContext.label}.`, savedContext }
    }
    case 'send_telegram_message':
      // Telegram sends are intercepted in the main IPC boundary, where the
      // trusted recipient map and a native confirmation are available.
      return { ok: false, message: 'Telegram messages must be confirmed through the Telegram connection.' }
  }
}

export async function executeToolAfterConfirmation(
  store: LocalStore,
  rawProposal: unknown,
  confirmed: boolean
): Promise<ToolExecutionResult> {
  if (!confirmed) {
    return { ok: false, message: 'Action cancelled. Nothing was changed or opened.' }
  }

  return executeConfirmedTool(store, rawProposal)
}

export function scheduleReminder(reminder: ReminderRecord): void {
  if (reminderTimers.has(reminder.id)) {
    return
  }

  const delay = Date.parse(reminder.dueAt) - Date.now()
  if (!Number.isFinite(delay) || delay < 0 || delay > MAX_TIMER_DELAY) {
    return
  }

  const timer = setTimeout(() => {
    reminderTimers.delete(reminder.id)
    showReminderNotification(reminder)
  }, delay)
  reminderTimers.set(reminder.id, timer)
}

export function showReminderNotification(reminder: ReminderRecord): void {
  if (!Notification.isSupported()) {
    return
  }

  new Notification({
    title: `LifeLens reminder: ${reminder.title}`,
    body: reminder.sourceContext.summary.slice(0, 240)
  }).show()
}

export async function restoreReminderTimers(store: LocalStore): Promise<void> {
  const reminders = await store.listReminders()
  for (const reminder of reminders) {
    scheduleReminder(reminder)
  }
}

async function toStoredSearchResults(records: readonly DocumentSearchRecord[]): Promise<Array<Omit<DocumentSearchResult, 'id' | 'rootId'> & { absolutePath: string }>> {
  const results = await Promise.all(records.map(async (record) => {
    try {
      const details = await stat(record.path)
      if (!details.isFile()) {
        return undefined
      }

      return {
        name: record.name,
        relativePath: record.relativePath,
        modifiedAt: details.mtime.toISOString(),
        absolutePath: record.path
      }
    } catch {
      return undefined
    }
  }))

  return results.filter((result): result is Omit<DocumentSearchResult, 'id' | 'rootId'> & { absolutePath: string } => Boolean(result))
}
