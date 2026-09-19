import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  PREFERENCE_KEYS,
  PREFERENCE_LANGUAGES,
  PREFERENCE_PARTS_OF_DAY,
  type AgentPreferenceView,
  type PreferenceKey,
  type PreferenceValue
} from '../../shared/model-contracts'

/**
 * Lumi's agent memory. Deliberately small, local and typed.
 *
 * Three kinds of context, kept apart on purpose:
 *
 * - **Durable task facts** live in PostgreSQL (the task timeline). They are
 *   never copied here; the context builder reads them fresh.
 * - **User preferences** ("I prefer evening appointments") are stored here,
 *   only from an explicit user statement, each with where and when it was
 *   said. A preference fills a gap in a new request; it never overrides what
 *   the user just asked for.
 * - **Episodic summaries** are short, app-authored descriptions of finished
 *   task steps, with the task and timeline position they came from. They help
 *   a model follow a conversation. They are never a source of truth: prices,
 *   times and availability always come from the website, now.
 *
 * Temporary conversation context (recent turns) is not stored at all; it
 * lives in the context builder's bounded window for one process.
 */

const MAX_EPISODES = 20
const MAX_SUMMARY = 240
const TURN_ID = /^[A-Za-z0-9_-]{1,64}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// App-authored summaries only: letters, digits, basic punctuation and ₹.
const SAFE_SUMMARY = /^[\p{L}\p{N} .,:;()/'₹+–-]{1,240}$/u

/**
 * How private the material behind a summary is. `account_private` material --
 * anything read through a signed-in account -- is never summarised into memory:
 * a paraphrase does not declassify it, and a summary is exactly what a later,
 * unrelated prompt would quote.
 */
export type EpisodeClassification = 'public' | 'account_private'

export interface EpisodeView {
  taskId: string
  kind: 'booking_search' | 'booking_outcome' | 'clinic_info'
  summary: string
  recordedAt: string
  /** The timeline position the summary was derived from. */
  provenance: { source: 'task_timeline'; taskId: string; sequence: number }
}

interface MemoryFile {
  version: 1
  preferences: AgentPreferenceView[]
  episodes: EpisodeView[]
}

export function parsePreferenceValue(value: unknown): PreferenceValue {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('That preference is invalid.')
  const record = value as Record<string, unknown>
  if (Object.keys(record).some((key) => key !== 'key' && key !== 'value')) throw new Error('That preference is invalid.')
  switch (record.key) {
    case 'preferred_part_of_day':
      if (!(PREFERENCE_PARTS_OF_DAY as readonly unknown[]).includes(record.value)) break
      return { key: 'preferred_part_of_day', value: record.value as typeof PREFERENCE_PARTS_OF_DAY[number] }
    case 'max_price_inr':
      if (typeof record.value !== 'number' || !Number.isSafeInteger(record.value) || record.value < 0 || record.value > 1_000_000) break
      return { key: 'max_price_inr', value: record.value }
    case 'reply_language':
      if (!(PREFERENCE_LANGUAGES as readonly unknown[]).includes(record.value)) break
      return { key: 'reply_language', value: record.value as typeof PREFERENCE_LANGUAGES[number] }
  }
  throw new Error('That preference is invalid.')
}

function readPreference(value: unknown): AgentPreferenceView | undefined {
  try {
    const record = value as Record<string, unknown>
    const preference = parsePreferenceValue({ key: record.key, value: record.value })
    const provenance = record.provenance as Record<string, unknown>
    if (provenance?.source !== 'user_statement' || typeof provenance.turnId !== 'string' || !TURN_ID.test(provenance.turnId) ||
        typeof provenance.recordedAt !== 'string' || Number.isNaN(Date.parse(provenance.recordedAt))) {
      return undefined
    }
    return { ...preference, provenance: { source: 'user_statement', turnId: provenance.turnId, recordedAt: provenance.recordedAt } } as AgentPreferenceView
  } catch {
    return undefined
  }
}

function readEpisode(value: unknown): EpisodeView | undefined {
  if (typeof value !== 'object' || value === null) return undefined
  const record = value as Record<string, unknown>
  const provenance = record.provenance as Record<string, unknown> | undefined
  if (typeof record.taskId !== 'string' || !UUID.test(record.taskId)) return undefined
  if (record.kind !== 'booking_search' && record.kind !== 'booking_outcome' && record.kind !== 'clinic_info') return undefined
  if (typeof record.summary !== 'string' || !SAFE_SUMMARY.test(record.summary)) return undefined
  if (typeof record.recordedAt !== 'string' || Number.isNaN(Date.parse(record.recordedAt))) return undefined
  if (provenance?.source !== 'task_timeline' || provenance.taskId !== record.taskId ||
      typeof provenance.sequence !== 'number' || !Number.isSafeInteger(provenance.sequence) || provenance.sequence < 1) {
    return undefined
  }
  return {
    taskId: record.taskId, kind: record.kind, summary: record.summary, recordedAt: record.recordedAt,
    provenance: { source: 'task_timeline', taskId: record.taskId, sequence: provenance.sequence }
  }
}

/** Remove anything an app-authored summary should never contain. */
export function sanitizeSummary(text: string): string {
  return text.normalize('NFKC').replace(/[^\p{L}\p{N} .,:;()/'₹+–-]/gu, ' ').replace(/\s+/gu, ' ').trim().slice(0, MAX_SUMMARY)
}

export interface PreferenceMemory {
  preferences(): Promise<AgentPreferenceView[]>
  remember(preference: PreferenceValue, turnId: string): Promise<AgentPreferenceView>
  forget(key: PreferenceKey): Promise<AgentPreferenceView[]>
  recordEpisode(episode: EpisodeInput): Promise<void>
  episodes(): Promise<EpisodeView[]>
}

export type EpisodeInput = Omit<EpisodeView, 'recordedAt' | 'provenance'> & {
  sequence: number
  /** Omitted means `public`. `account_private` is refused, unconditionally. */
  classification?: EpisodeClassification
}

export class AgentMemoryStore implements PreferenceMemory {
  private readonly path: string
  private queue: Promise<unknown> = Promise.resolve()

  constructor(userDataDir: string, private readonly now: () => number = () => Date.now()) {
    this.path = join(userDataDir, 'agent-memory.json')
  }

  private async load(): Promise<MemoryFile> {
    try {
      const value: unknown = JSON.parse(await readFile(this.path, 'utf8'))
      const record = value as Record<string, unknown>
      if (record?.version !== 1) return { version: 1, preferences: [], episodes: [] }
      const preferences = Array.isArray(record.preferences)
        ? record.preferences.map(readPreference).filter((item): item is AgentPreferenceView => item !== undefined)
        : []
      const episodes = Array.isArray(record.episodes)
        ? record.episodes.map(readEpisode).filter((item): item is EpisodeView => item !== undefined)
        : []
      // One value per key; the latest statement wins.
      const byKey = new Map<PreferenceKey, AgentPreferenceView>()
      for (const preference of preferences) byKey.set(preference.key, preference)
      return { version: 1, preferences: PREFERENCE_KEYS.flatMap((key) => byKey.get(key) ?? []), episodes: episodes.slice(-MAX_EPISODES) }
    } catch {
      return { version: 1, preferences: [], episodes: [] }
    }
  }

  private async save(file: MemoryFile): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true })
    const temporary = `${this.path}.${randomUUID()}.tmp`
    await writeFile(temporary, JSON.stringify(file), 'utf8')
    await rename(temporary, this.path)
  }

  private serial<T>(work: () => Promise<T>): Promise<T> {
    const run = this.queue.then(work)
    this.queue = run.catch(() => undefined)
    return run
  }

  async preferences(): Promise<AgentPreferenceView[]> {
    return (await this.load()).preferences
  }

  async remember(preferenceValue: PreferenceValue, turnId: string): Promise<AgentPreferenceView> {
    const preference = parsePreferenceValue(preferenceValue)
    if (!TURN_ID.test(turnId)) throw new Error('That request reference is invalid.')
    return await this.serial(async () => {
      const file = await this.load()
      const view = {
        ...preference,
        provenance: { source: 'user_statement', turnId, recordedAt: new Date(this.now()).toISOString() }
      } as AgentPreferenceView
      file.preferences = [...file.preferences.filter((item) => item.key !== preference.key), view]
      await this.save(file)
      return view
    })
  }

  async forget(key: PreferenceKey): Promise<AgentPreferenceView[]> {
    if (!PREFERENCE_KEYS.includes(key)) throw new Error('That preference is invalid.')
    return await this.serial(async () => {
      const file = await this.load()
      file.preferences = file.preferences.filter((item) => item.key !== key)
      await this.save(file)
      return file.preferences
    })
  }

  recordEpisode(episode: EpisodeInput): Promise<void> {
    // Refused before anything is queued, sanitised, read or written.
    if (episode.classification === 'account_private') return Promise.resolve()
    return this.serial(async () => {
      const summary = sanitizeSummary(episode.summary)
      if (!summary || !UUID.test(episode.taskId)) return
      const file = await this.load()
      const view: EpisodeView = {
        taskId: episode.taskId,
        kind: episode.kind,
        summary,
        recordedAt: new Date(this.now()).toISOString(),
        provenance: { source: 'task_timeline', taskId: episode.taskId, sequence: episode.sequence }
      }
      // One summary per task and kind: the latest position replaces older ones.
      file.episodes = [...file.episodes.filter((item) => !(item.taskId === view.taskId && item.kind === view.kind)), view].slice(-MAX_EPISODES)
      await this.save(file)
    })
  }

  async episodes(): Promise<EpisodeView[]> {
    return (await this.load()).episodes
  }
}

/** In-memory implementation for tests and for builds without a profile. */
export class EphemeralMemory implements PreferenceMemory {
  private items: AgentPreferenceView[] = []
  private history: EpisodeView[] = []

  constructor(private readonly now: () => number = () => Date.now()) {}

  async preferences(): Promise<AgentPreferenceView[]> {
    return [...this.items]
  }

  async remember(preferenceValue: PreferenceValue, turnId: string): Promise<AgentPreferenceView> {
    const preference = parsePreferenceValue(preferenceValue)
    const view = { ...preference, provenance: { source: 'user_statement', turnId, recordedAt: new Date(this.now()).toISOString() } } as AgentPreferenceView
    this.items = [...this.items.filter((item) => item.key !== preference.key), view]
    return view
  }

  async forget(key: PreferenceKey): Promise<AgentPreferenceView[]> {
    this.items = this.items.filter((item) => item.key !== key)
    return [...this.items]
  }

  async recordEpisode(episode: EpisodeInput): Promise<void> {
    if (episode.classification === 'account_private') return
    const view: EpisodeView = {
      taskId: episode.taskId, kind: episode.kind, summary: sanitizeSummary(episode.summary),
      recordedAt: new Date(this.now()).toISOString(),
      provenance: { source: 'task_timeline', taskId: episode.taskId, sequence: episode.sequence }
    }
    this.history = [...this.history.filter((item) => !(item.taskId === view.taskId && item.kind === view.kind)), view].slice(-MAX_EPISODES)
  }

  async episodes(): Promise<EpisodeView[]> {
    return [...this.history]
  }
}
