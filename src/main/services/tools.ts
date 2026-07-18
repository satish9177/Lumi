import { nativeImage, Notification, shell } from 'electron'
import { extname } from 'node:path'
import {
  canonicalizeApprovedRoots,
  resolveApprovedDocumentPath
} from '../../features/document-tools/search'
import { isImageExtension } from '../../shared/search-query'
import { encodeCaptureImage, type CaptureImage } from './capture'
import { resolveTrustedResultPath } from './thumbnails'
import {
  parseToolProposal,
  type ReminderRecord,
  type ToolExecutionResult
} from '../../shared/contracts'
import { normalizeSearchQuery } from '../../shared/search-query'
import { runDocumentSearch } from './document-search'
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
      const roots = await store.listDocumentRoots()
      if (roots.length === 0) {
        return { ok: false, message: 'No folder is approved yet. Approve a folder before searching.' }
      }

      const search = await runDocumentSearch(store, normalizeSearchQuery(proposal.arguments))
      return {
        ok: true,
        message: search.fallback
          ? `No filename matched. Showing ${search.results.length} recent possible matches.`
          : `Found ${search.results.length} matching ${search.results.length === 1 ? 'file' : 'files'}.`,
        searchResults: search.results,
        compactResults: search.compactResults,
        searchFallback: search.fallback
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
    case 'analyze_photo': {
      const storedResult = await store.getSearchResult(proposal.arguments.resultId)
      if (!storedResult) {
        return { ok: false, message: 'That photo is not a result from an approved search. Search again first.' }
      }

      // Everything is re-derived from trusted state: the renderer supplied only
      // an opaque result identifier, never a path, a filename, or image bytes.
      const safePath = await resolveTrustedResultPath(store, proposal.arguments.resultId)
      if (!safePath) {
        return { ok: false, message: 'That photo is no longer available inside its approved folder.' }
      }

      if (!isImageExtension(extname(safePath))) {
        return { ok: false, message: 'That result is not an image Lumi can analyse.' }
      }

      const image = loadImageForAnalysis(safePath)
      if (!image) {
        return { ok: false, message: 'Lumi could not read that image. It may be corrupt or an unsupported format.' }
      }

      let encoded: { dataUrl: string; width: number; height: number }
      try {
        // The same bounded ladder used for screen captures: the original
        // full-resolution file is never uploaded.
        encoded = encodeCaptureImage(image)
      } catch {
        return { ok: false, message: 'That photo is too large to share safely. Try a smaller image.' }
      }

      return {
        ok: true,
        message: `Prepared ${storedResult.name} for one analysis.`,
        analysisImage: {
          resultId: storedResult.id,
          name: storedResult.name,
          dataUrl: encoded.dataUrl,
          mimeType: 'image/jpeg',
          width: encoded.width,
          height: encoded.height
        }
      }
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

/** Overridable so tests can exercise the approval path without Electron. */
let loadImageForAnalysis: (path: string) => CaptureImage | undefined = (path) => {
  const image = nativeImage.createFromPath(path)
  return image.isEmpty() ? undefined : (image as unknown as CaptureImage)
}

export function setAnalysisImageLoader(loader: (path: string) => CaptureImage | undefined): void {
  loadImageForAnalysis = loader
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

