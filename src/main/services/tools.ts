import { Notification } from 'electron'
import { parseToolProposal, type ReminderRecord, type ToolExecutionResult, type ToolProposal } from '../../shared/contracts'
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
    default:
      return {
        ok: false,
        message: `${proposal.toolName} is intentionally not enabled until its bounded implementation is complete.`
      }
  }
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
