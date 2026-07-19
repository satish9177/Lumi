import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import { canonicalizeApprovedRoots } from '../../features/document-tools/search'
import type { PhotoSearchStatus } from '../../shared/contracts'
import type { NormalizedSearchQuery } from '../../shared/search-query'
import type { StoredDocumentRoot } from '../services/store'
import type { VisionEngine } from './engine'
import { computeImageId, PhotoIndexStore, type PhotoIndexRecord } from './index-store'
import { MODEL_PACK_TOTAL_BYTES, MODEL_PACK_VERSION } from './manifest'
import {
  clearModelPack,
  downloadModelPack,
  isModelPackInstalled,
  ModelPackError,
  type ModelPackRuntime
} from './model-pack'
import {
  decodePhotoSnapshot,
  PhotoDecodeError,
  revalidateSnapshot,
  scanApprovedPhotos,
  type PhotoFileSnapshot,
  type PhotoScanRoot,
  type ThumbnailDecoder
} from './scanner'
import { LocalQueryEmbedder, rankSemanticPhotos } from './semantic-search'

const MAX_QUEUE = 512
const BASE_YIELD_MS = 75
const REALTIME_YIELD_MS = 900
const SLOW_INFERENCE_MS = 2_000
const MAX_TRANSIENT_ATTEMPTS = 3
const STATUS_THROTTLE_MS = 1_000

interface PhotoPreferences {
  enabled: boolean
  consented: boolean
  paused: boolean
  onlyWhilePluggedIn: boolean
}

export interface PhotoIndexCoordinatorDependencies {
  userDataDir: string
  listRoots: () => Promise<StoredDocumentRoot[]>
  createEngine: () => VisionEngine
  decodeThumbnail: ThumbnailDecoder
  modelRuntime: ModelPackRuntime
  emitStatus?: (status: PhotoSearchStatus) => void
  isOnBattery?: () => boolean
  now?: () => number
  delay?: (milliseconds: number) => Promise<void>
  indexStore?: PhotoIndexStore
  /** Narrow lifecycle seams for deterministic tests; production uses the frozen pack implementation. */
  isModelInstalled?: () => Promise<boolean>
  downloadModel?: (options: { signal?: AbortSignal; onProgress: (received: number, total: number) => void }) => Promise<void>
  clearModel?: () => Promise<void>
  scan?: (roots: readonly PhotoScanRoot[]) => ReturnType<typeof scanApprovedPhotos>
}

export interface TrustedSemanticCandidate {
  rootId: string
  absolutePath: string
  relativePath: string
  name: string
  modifiedAtMs: number
  sizeBytes: number
  reason: string
}

export interface SemanticSearchResult {
  candidates: TrustedSemanticCandidate[]
  indexed: number
  total: number
  available: boolean
  incomplete: boolean
  message?: string
}

export class PhotoIndexCoordinator {
  private readonly index: PhotoIndexStore
  private readonly preferencesPath: string
  private readonly now: () => number
  private readonly wait: (milliseconds: number) => Promise<void>
  private preferences: PhotoPreferences = { enabled: false, consented: false, paused: false, onlyWhilePluggedIn: true }
  private modelInstalled = false
  private downloadedBytes = 0
  private state: PhotoSearchStatus['state'] = 'off'
  private message: string | undefined
  private lastIndexedAt: string | undefined
  private engine: VisionEngine | undefined
  private readonly embedder: LocalQueryEmbedder
  private downloadAbort: AbortController | undefined
  private downloadPromise: Promise<void> | undefined
  private queue: PhotoFileSnapshot[] = []
  private queued = new Set<string>()
  private processing = false
  private reconciling = false
  private reconcileAgain = false
  private generation = 0
  private knownRootIds = new Set<string>()
  private realtimeActive = false
  private disposed = false
  private lastStatusEmit = 0

  constructor(private readonly dependencies: PhotoIndexCoordinatorDependencies) {
    this.index = dependencies.indexStore ?? new PhotoIndexStore(dependencies.userDataDir)
    this.preferencesPath = join(dependencies.userDataDir, 'photo-search-preferences.json')
    this.now = dependencies.now ?? (() => Date.now())
    this.wait = dependencies.delay ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)))
    this.embedder = new LocalQueryEmbedder(dependencies.userDataDir, () => this.getEngine())
  }

  async initialize(): Promise<void> {
    this.preferences = await readPreferences(this.preferencesPath)
    await this.index.load(MODEL_PACK_VERSION)
    this.modelInstalled = await this.checkModelInstalled()
    if (!this.preferences.enabled) {
      this.state = 'off'
    } else if (!this.modelInstalled) {
      this.state = 'consent_required'
    } else if (this.preferences.paused || this.shouldPauseForPower()) {
      this.setPausedState()
    } else {
      this.state = 'indexing'
      void this.reconcile()
    }
    this.emit(true)
  }

  status(): PhotoSearchStatus {
    const counts = this.index.isLoaded() ? this.index.counts() : { total: 0, indexed: 0, pending: 0, failed: 0, skipped: 0 }
    return {
      state: this.state,
      enabled: this.preferences.enabled,
      modelInstalled: this.modelInstalled,
      modelDownloadBytes: MODEL_PACK_TOTAL_BYTES,
      downloadedBytes: this.downloadedBytes,
      indexed: counts.indexed,
      total: counts.total,
      failed: counts.failed,
      skipped: counts.skipped,
      lastIndexedAt: this.lastIndexedAt,
      onlyWhilePluggedIn: this.preferences.onlyWhilePluggedIn,
      powerStateKnown: this.dependencies.isOnBattery !== undefined,
      onBattery: this.onBattery(),
      message: this.message
    }
  }

  async enable(): Promise<PhotoSearchStatus> {
    this.assertLive()
    this.preferences.enabled = true
    this.preferences.paused = false
    await this.persistPreferences()
    this.modelInstalled = await this.checkModelInstalled()
    if (this.modelInstalled) {
      this.state = this.shouldPauseForPower() ? 'paused' : 'indexing'
      this.message = this.shouldPauseForPower() ? 'Photo indexing is paused while this device is on battery.' : undefined
      void this.reconcile()
    } else {
      this.state = 'consent_required'
      this.message = 'Download the 148 MB local model to continue.'
    }
    this.emit(true)
    return this.status()
  }

  async downloadModel(): Promise<PhotoSearchStatus> {
    this.assertLive()
    if (!this.preferences.enabled) await this.enable()
    if (this.downloadPromise) return this.status()
    this.preferences.consented = true
    await this.persistPreferences()
    this.downloadAbort = new AbortController()
    this.state = 'downloading'
    this.message = undefined
    this.emit(true)

    this.downloadPromise = (async () => {
      try {
        const report = (receivedBytes: number, totalBytes: number): void => {
            this.downloadedBytes = receivedBytes
            this.state = receivedBytes >= totalBytes ? 'verifying' : 'downloading'
            this.emit()
        }
        if (this.dependencies.downloadModel) {
          await this.dependencies.downloadModel({ signal: this.downloadAbort?.signal, onProgress: report })
        } else {
          await downloadModelPack(this.dependencies.userDataDir, this.dependencies.modelRuntime, {
            consented: true,
            signal: this.downloadAbort?.signal,
            onProgress: (progress) => report(progress.receivedBytes, progress.totalBytes)
          })
        }
        this.modelInstalled = true
        this.downloadedBytes = MODEL_PACK_TOTAL_BYTES
        this.state = this.shouldPauseForPower() ? 'paused' : 'indexing'
        this.message = this.shouldPauseForPower() ? 'Photo indexing is paused while this device is on battery.' : undefined
        void this.reconcile()
      } catch (error) {
        this.modelInstalled = false
        if (error instanceof ModelPackError && error.code === 'cancelled') {
          this.state = 'consent_required'
          this.message = 'The local model download was cancelled.'
        } else {
          this.state = 'error'
          this.message = error instanceof ModelPackError ? error.message : 'The local model download did not complete.'
        }
      } finally {
        this.downloadAbort = undefined
        this.downloadPromise = undefined
        this.emit(true)
      }
    })()
    await this.downloadPromise
    return this.status()
  }

  async cancelDownload(): Promise<PhotoSearchStatus> {
    this.downloadAbort?.abort()
    await this.downloadPromise?.catch(() => undefined)
    return this.status()
  }

  async pause(): Promise<PhotoSearchStatus> {
    this.preferences.paused = true
    await this.persistPreferences()
    this.setPausedState('Photo indexing is paused.')
    this.emit(true)
    return this.status()
  }

  /** Cancels the current run; pending journal rows resume on the next explicit resume. */
  async cancel(): Promise<PhotoSearchStatus> {
    this.generation += 1
    this.queue = []
    this.queued.clear()
    this.preferences.paused = true
    await this.persistPreferences()
    this.setPausedState('Photo indexing was cancelled. Resume to continue from the local journal.')
    this.emit(true)
    return this.status()
  }

  async resume(): Promise<PhotoSearchStatus> {
    this.preferences.paused = false
    await this.persistPreferences()
    if (!this.preferences.enabled || !this.modelInstalled) return this.status()
    if (this.shouldPauseForPower()) this.setPausedState()
    else {
      this.state = 'indexing'
      this.message = undefined
      void this.reconcile()
      void this.processQueue()
    }
    this.emit(true)
    return this.status()
  }

  async setOnlyWhilePluggedIn(enabled: boolean): Promise<PhotoSearchStatus> {
    this.preferences.onlyWhilePluggedIn = enabled
    await this.persistPreferences()
    if (!this.preferences.enabled || !this.modelInstalled) return this.status()
    if (this.preferences.paused || this.shouldPauseForPower()) this.setPausedState(this.preferences.paused ? 'Photo indexing is paused.' : undefined)
    else {
      this.state = 'indexing'
      this.message = undefined
      void this.reconcile()
      void this.processQueue()
    }
    this.emit(true)
    return this.status()
  }

  setRealtimeActive(active: boolean): void {
    this.realtimeActive = active
  }

  async revokeRoot(rootId: string): Promise<void> {
    this.generation += 1
    this.queue = this.queue.filter((snapshot) => snapshot.rootId !== rootId)
    this.queued = new Set(this.queue.map((snapshot) => computeImageId(snapshot.rootId, snapshot.relativePath)))
    this.knownRootIds.delete(rootId)
    await this.index.purgeRoot(rootId, this.now())
    this.emit(true)
    if (this.canIndex()) void this.reconcile()
  }

  powerChanged(): void {
    if (!this.preferences.enabled || !this.modelInstalled || this.preferences.paused) return
    if (this.shouldPauseForPower()) this.setPausedState()
    else {
      this.state = 'indexing'
      this.message = undefined
      void this.processQueue()
      void this.reconcile()
    }
    this.emit(true)
  }

  async rebuild(): Promise<PhotoSearchStatus> {
    this.generation += 1
    this.queue = []
    this.queued.clear()
    this.downloadAbort?.abort()
    await this.downloadPromise?.catch(() => undefined)
    this.disposeEngine()
    // Engine disposal must precede deleting any model file.
    if (this.dependencies.clearModel) await this.dependencies.clearModel()
    else await clearModelPack(this.dependencies.userDataDir)
    await this.index.reset(MODEL_PACK_VERSION)
    this.modelInstalled = false
    this.downloadedBytes = 0
    this.lastIndexedAt = undefined
    this.state = 'consent_required'
    this.message = 'The local model and photo index were cleared. Download the model to rebuild.'
    this.emit(true)
    return this.status()
  }

  async disable(): Promise<PhotoSearchStatus> {
    this.generation += 1
    this.queue = []
    this.queued.clear()
    this.downloadAbort?.abort()
    await this.downloadPromise?.catch(() => undefined)
    this.disposeEngine()
    this.preferences.enabled = false
    this.preferences.paused = false
    await this.persistPreferences()
    this.state = 'off'
    this.message = undefined
    this.emit(true)
    return this.status()
  }

  async reconcile(): Promise<void> {
    if (this.reconciling) {
      this.reconcileAgain = true
      return
    }
    this.reconciling = true
    try {
      do {
        this.reconcileAgain = false
        await this.reconcileOnce()
      } while (this.reconcileAgain && !this.disposed)
    } finally {
      this.reconciling = false
    }
    void this.processQueue()
  }

  async search(query: NormalizedSearchQuery): Promise<SemanticSearchResult> {
    const counts = this.index.counts()
    const base = { indexed: counts.indexed, total: counts.total, incomplete: counts.indexed < counts.total }
    if (!this.preferences.enabled) {
      return { ...base, candidates: [], available: false, message: 'Intelligent photo search is not enabled. I searched filenames and dates only.' }
    }
    if (!this.modelInstalled || query.concepts.length === 0) {
      return { ...base, candidates: [], available: false, message: 'The local photo model is unavailable. I searched filenames and dates only.' }
    }

    try {
      const queryVector = await this.embedder.embed(query.concepts)
      const vectors = await this.index.readAllVectors()
      const ranked = rankSemanticPhotos(this.index.indexed(), vectors, queryVector, query, this.now())
      const roots = await this.liveRoots()
      const byId = new Map(roots.map((root) => [root.id, root]))
      const candidates: TrustedSemanticCandidate[] = []
      for (const match of ranked) {
        if (candidates.length >= 50) break
        const root = byId.get(match.record.rootId)
        if (!root) continue
        const absolutePath = join(root.canonicalPath, ...match.record.relativePath.split('/'))
        const snapshot = recordSnapshot(match.record, root.canonicalPath, absolutePath)
        if (!await revalidateSnapshot(snapshot)) continue
        candidates.push({
          rootId: match.record.rootId,
          absolutePath,
          relativePath: match.record.relativePath,
          name: match.record.name,
          modifiedAtMs: match.record.mtimeMs,
          sizeBytes: match.record.sizeBytes,
          reason: match.reason
        })
      }
      return { ...base, candidates, available: true }
    } catch {
      return { ...base, candidates: [], available: false, message: 'Intelligent photo search is temporarily unavailable. I searched filenames and dates only.' }
    }
  }

  async shutdown(): Promise<void> {
    this.disposed = true
    this.generation += 1
    this.queue = []
    this.queued.clear()
    this.downloadAbort?.abort()
    await this.downloadPromise?.catch(() => undefined)
    this.disposeEngine()
    await this.index.flush()
  }

  private async reconcileOnce(): Promise<void> {
    const roots = await this.liveRoots()
    const rootIds = new Set(roots.map((root) => root.id))
    const revoked = [...this.knownRootIds].some((rootId) => !rootIds.has(rootId))
    if (revoked) {
      this.generation += 1
      this.queue = []
      this.queued.clear()
    }
    this.knownRootIds = rootIds
    await this.index.retainRoots([...rootIds], this.now())

    if (!this.canIndex()) {
      this.emit(true)
      return
    }

    const scan = await (this.dependencies.scan ?? scanApprovedPhotos)(roots)
    const seen = new Set<string>()
    for (const failure of scan.failures) {
      const imageId = computeImageId(failure.rootId, failure.relativePath)
      seen.add(imageId)
      const existing = this.index.get(imageId)
      if (existing && existing.mtimeMs === failure.mtimeMs && existing.sizeBytes === failure.sizeBytes && existing.status === 'skipped') continue
      await this.index.put({
        imageId,
        rootId: failure.rootId,
        relativePath: failure.relativePath,
        name: failure.name,
        mtimeMs: failure.mtimeMs,
        sizeBytes: failure.sizeBytes,
        modelVersion: MODEL_PACK_VERSION,
        status: 'skipped',
        failureCode: failure.code,
        attempts: 1,
        updatedAtMs: this.now()
      })
    }

    for (const snapshot of scan.files) {
      const imageId = computeImageId(snapshot.rootId, snapshot.relativePath)
      seen.add(imageId)
      const existing = this.index.get(imageId)
      const unchanged = existing?.mtimeMs === snapshot.mtimeMs && existing.sizeBytes === snapshot.sizeBytes
      if (unchanged && (existing.status === 'indexed' || existing.status === 'skipped')) continue
      if (!unchanged || !existing) {
        await this.index.put(recordForSnapshot(snapshot, 'pending', 0, this.now()))
      }
      if (this.queue.length < MAX_QUEUE && !this.queued.has(imageId)) {
        this.queue.push(snapshot)
        this.queued.add(imageId)
      }
    }

    for (const record of this.index.all()) {
      if (record.status !== 'deleted' && rootIds.has(record.rootId) && !seen.has(record.imageId)) {
        await this.index.markDeleted(record.imageId, this.now())
      }
    }
    this.state = this.queue.length > 0 || this.index.counts().pending > 0 ? 'indexing' : 'ready'
    this.message = scan.truncated ? 'Photo scanning reached its safe traversal limit; coverage may be incomplete.' : undefined
    this.emit(true)
  }

  private async processQueue(): Promise<void> {
    if (this.processing || !this.canIndex()) return
    this.processing = true
    const generation = this.generation
    try {
      while (this.queue.length > 0 && this.canIndex() && generation === this.generation) {
        const snapshot = this.queue.shift()!
        const imageId = computeImageId(snapshot.rootId, snapshot.relativePath)
        this.queued.delete(imageId)
        const started = this.now()
        await this.processOne(snapshot, generation)
        const elapsed = this.now() - started
        this.emit()
        const adaptive = elapsed > SLOW_INFERENCE_MS ? Math.min(1_500, Math.round(elapsed / 3)) : BASE_YIELD_MS
        await this.wait(Math.max(adaptive, this.realtimeActive ? REALTIME_YIELD_MS : 0))
      }
    } finally {
      this.processing = false
    }

    if (generation !== this.generation) {
      if (this.canIndex()) {
        void this.reconcile()
        void this.processQueue()
      }
      return
    }
    if (!this.canIndex()) return
    if (this.index.counts().pending > 0) {
      void this.reconcile()
      return
    }
    if (this.index.shouldCompact()) await this.index.compact()
    this.state = 'ready'
    this.message = this.index.counts().failed > 0 ? 'Some photos could not be indexed; changed files will be retried.' : undefined
    this.emit(true)
  }

  private async processOne(snapshot: PhotoFileSnapshot, generation: number): Promise<void> {
    const imageId = computeImageId(snapshot.rootId, snapshot.relativePath)
    const currentRoots = await this.liveRoots()
    if (!currentRoots.some((root) => root.id === snapshot.rootId && root.canonicalPath === snapshot.rootPath)) {
      await this.index.purgeRoot(snapshot.rootId, this.now())
      return
    }
    try {
      const decoded = await decodePhotoSnapshot(snapshot, this.dependencies.decodeThumbnail)
      const vector = await this.getEngine().embedImage(decoded.bitmap, decoded.width, decoded.height)
      if (generation !== this.generation || !await revalidateSnapshot(snapshot)) return
      const row = await this.index.appendVector(vector)
      if (generation !== this.generation || !await revalidateSnapshot(snapshot)) return
      await this.index.put({ ...recordForSnapshot(snapshot, 'indexed', 1, this.now()), vectorRow: row })
      this.lastIndexedAt = new Date(this.now()).toISOString()
    } catch (error) {
      if (generation !== this.generation) return
      const code = error instanceof PhotoDecodeError ? error.code : 'inference_failed'
      const existing = this.index.get(imageId)
      const attempts = (existing?.attempts ?? 0) + 1
      const transient = code === 'file_locked' || code === 'inference_failed'
      await this.index.put({
        ...recordForSnapshot(snapshot, transient ? 'failed' : 'skipped', attempts, this.now()),
        failureCode: code
      })
      if (transient && attempts < MAX_TRANSIENT_ATTEMPTS && this.queue.length < MAX_QUEUE && await revalidateSnapshot(snapshot)) {
        this.queue.push(snapshot)
        this.queued.add(imageId)
      }
    }
  }

  private async liveRoots(): Promise<PhotoScanRoot[]> {
    const stored = await this.dependencies.listRoots()
    const roots: PhotoScanRoot[] = []
    for (const root of stored) {
      try {
        const [canonical] = await canonicalizeApprovedRoots([root.path])
        if (canonical) roots.push({ id: root.id, canonicalPath: canonical.canonicalPath, label: root.label })
      } catch {
        // Temporarily unavailable roots are not filesystem authority and are skipped.
      }
    }
    return roots
  }

  private canIndex(): boolean {
    return !this.disposed && this.preferences.enabled && this.modelInstalled && !this.preferences.paused && !this.shouldPauseForPower()
  }

  private shouldPauseForPower(): boolean {
    return this.preferences.onlyWhilePluggedIn && this.dependencies.isOnBattery !== undefined && this.onBattery()
  }

  private onBattery(): boolean {
    try { return this.dependencies.isOnBattery?.() ?? false } catch { return false }
  }

  private setPausedState(message = 'Photo indexing is paused while this device is on battery.'): void {
    this.state = 'paused'
    this.message = message
  }

  private getEngine(): VisionEngine {
    this.engine ??= this.dependencies.createEngine()
    return this.engine
  }

  private disposeEngine(): void {
    this.engine?.dispose()
    this.engine = undefined
    this.embedder.clear()
  }

  private async persistPreferences(): Promise<void> {
    await writeJsonAtomic(this.preferencesPath, this.preferences)
  }

  private emit(force = false): void {
    const now = this.now()
    if (!force && now - this.lastStatusEmit < STATUS_THROTTLE_MS) return
    this.lastStatusEmit = now
    this.dependencies.emitStatus?.(this.status())
  }

  private assertLive(): void {
    if (this.disposed) throw new Error('Photo indexing has shut down.')
  }

  private checkModelInstalled(): Promise<boolean> {
    return this.dependencies.isModelInstalled?.() ?? isModelPackInstalled(this.dependencies.userDataDir)
  }
}

function recordForSnapshot(snapshot: PhotoFileSnapshot, status: PhotoIndexRecord['status'], attempts: number, nowMs: number): PhotoIndexRecord {
  return {
    imageId: computeImageId(snapshot.rootId, snapshot.relativePath),
    rootId: snapshot.rootId,
    relativePath: snapshot.relativePath,
    name: snapshot.name,
    mtimeMs: snapshot.mtimeMs,
    sizeBytes: snapshot.sizeBytes,
    width: snapshot.width,
    height: snapshot.height,
    modelVersion: MODEL_PACK_VERSION,
    status,
    attempts,
    updatedAtMs: nowMs
  }
}

function recordSnapshot(record: PhotoIndexRecord, rootPath: string, absolutePath: string): PhotoFileSnapshot {
  return {
    rootId: record.rootId,
    rootPath,
    absolutePath,
    relativePath: record.relativePath,
    name: record.name,
    mtimeMs: record.mtimeMs,
    sizeBytes: record.sizeBytes,
    width: record.width ?? 1,
    height: record.height ?? 1
  }
}

async function readPreferences(path: string): Promise<PhotoPreferences> {
  try {
    const value = JSON.parse(await readFile(path, 'utf8')) as Partial<PhotoPreferences>
    return {
      enabled: value.enabled === true,
      consented: value.consented === true,
      paused: value.paused === true,
      onlyWhilePluggedIn: value.onlyWhilePluggedIn !== false
    }
  } catch {
    return { enabled: false, consented: false, paused: false, onlyWhilePluggedIn: true }
  }
}

async function writeJsonAtomic(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true })
  const temporary = `${path}.tmp`
  await writeFile(temporary, JSON.stringify(value, null, 2), 'utf8')
  await rename(temporary, path)
}
