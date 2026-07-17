import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { ReminderInput, ReminderRecord } from '../../shared/contracts'

interface LocalState {
  reminders: ReminderRecord[]
}

const EMPTY_STATE: LocalState = { reminders: [] }

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

  private async readState(): Promise<LocalState> {
    try {
      const raw = await readFile(this.statePath, 'utf8')
      const parsed: unknown = JSON.parse(raw)
      if (!isLocalState(parsed)) {
        return { ...EMPTY_STATE }
      }

      return { reminders: parsed.reminders }
    } catch (error: unknown) {
      if (isNodeError(error, 'ENOENT')) {
        return { ...EMPTY_STATE }
      }

      throw error
    }
  }

  private async writeState(state: LocalState): Promise<void> {
    await mkdir(dirname(this.statePath), { recursive: true })
    await writeFile(this.statePath, JSON.stringify(state, null, 2), 'utf8')
  }
}

function isNodeError(value: unknown, code: string): value is NodeJS.ErrnoException {
  return typeof value === 'object' && value !== null && 'code' in value && value.code === code
}

function isLocalState(value: unknown): value is LocalState {
  if (typeof value !== 'object' || value === null || !('reminders' in value) || !Array.isArray(value.reminders)) {
    return false
  }

  return value.reminders.every(isReminderRecord)
}

function isReminderRecord(value: unknown): value is ReminderRecord {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const candidate = value as Partial<ReminderRecord>
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.title === 'string' &&
    typeof candidate.dueAt === 'string' &&
    typeof candidate.createdAt === 'string' &&
    typeof candidate.sourceContext === 'object' &&
    candidate.sourceContext !== null
  )
}
