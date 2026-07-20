import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { basename, dirname, extname, join } from 'node:path'
import { canonicalizeApprovedRoots } from '../../features/document-tools/search'
import type { PhotoSearchStatus } from '../../shared/contracts'
import { classifyFileKind, type NormalizedSearchQuery } from '../../shared/search-query'
import type { StoredDocumentRoot } from '../services/store'
import type { VisionEngine } from './engine'
import { computeImageId, PhotoIndexStore, type PhotoIndexRecord } from './index-store'
import { EXTRAS_PACK_TOTAL_BYTES } from './extras-manifest'
import { MODEL_PACK_TOTAL_BYTES, MODEL_PACK_VERSION } from './manifest'
import {
  clearModelPack,
  downloadExtrasPack,
  downloadModelPack,
  extrasLanguageDirectory,
  isExtrasPackInstalled,
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
import { countFaces } from './face-detect'
import { decodeForFaceDetection } from './face-image'
import { rankHybridPhotos, type HybridCoverage } from './hybrid-search'
import { LocalOcrEngine, OcrEngineError, type OcrWorkerHandle } from './ocr-engine'
import { decodeForOcr } from './ocr-image'
import { LocalQueryEmbedder } from './semantic-search'
import type { FaceFailureCode, OcrFailureCode } from './index-store'

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
  /** Phase 2, opt-in separately from semantic search. */
  textSearchEnabled: boolean
  faceCountEnabled: boolean
  extrasConsented: boolean
}

/**
 * OCR costs roughly an order of magnitude more than a face scan per image, so
 * it yields for longer between items. The goal is that enabling text search
 * never makes the machine feel worse than semantic indexing already did.
 */
const OCR_YIELD_MS = 600
const FACE_YIELD_MS = 150

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
  /** Phase-2 seams, so tests use fake OCR and face detectors rather than real models. */
  isExtrasInstalled?: () => Promise<boolean>
  downloadExtras?: (options: { signal?: AbortSignal; onProgress: (received: number, total: number) => void }) => Promise<void>
  clearExtras?: () => Promise<void>
  createOcrWorker?: (languageDirectory: string) => Promise<OcrWorkerHandle>
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
  private preferences: PhotoPreferences = {
    enabled: false,
    consented: false,
    paused: false,
    onlyWhilePluggedIn: true,
    textSearchEnabled: false,
    faceCountEnabled: false,
    extrasConsented: false
  }
  private modelInstalled = false
  private extrasInstalled = false
  private ocrEngine: LocalOcrEngine | undefined
  /** Aborted on pause, revocation, disable, and shutdown, so OCR stops now. */
  private phase2Abort: AbortController | undefined
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
    this.extrasInstalled = await (this.dependencies.isExtrasInstalled?.() ?? isExtrasPackInstalled(this.dependencies.userDataDir))
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
      message: this.message,
      ...this.phase2Status()
    }
  }

  /**
   * Phase-2 coverage, reported separately so the settings card can show which
   * kind of search is ready rather than one aggregate number that is never
   * quite true for any of them.
   */
  private phase2Status(): Pick<
    PhotoSearchStatus,
    | 'textSearchEnabled'
    | 'faceCountEnabled'
    | 'extrasInstalled'
    | 'extrasDownloadBytes'
    | 'textIndexed'
    | 'faceScanned'
  > {
    const phase2 = this.index.isLoaded()
      ? this.index.phase2Counts()
      : { total: 0, ocrDone: 0, ocrFailed: 0, ocrSkipped: 0, faceDone: 0, faceFailed: 0, faceSkipped: 0 }
    return {
      textSearchEnabled: this.preferences.textSearchEnabled,
      faceCountEnabled: this.preferences.faceCountEnabled,
      extrasInstalled: this.extrasInstalled,
      extrasDownloadBytes: EXTRAS_PACK_TOTAL_BYTES,
      // Skipped images are counted as covered: they have been looked at and
      // will not be looked at again, so leaving them out would leave the
      // progress line permanently short of its total.
      textIndexed: phase2.ocrDone + phase2.ocrFailed + phase2.ocrSkipped,
      faceScanned: phase2.faceDone + phase2.faceFailed + phase2.faceSkipped
    }
  }

  /** Turns on local text search. Downloads the extras pack if it is missing. */
  async setTextSearchEnabled(enabled: boolean): Promise<PhotoSearchStatus> {
    this.assertLive()
    this.preferences.textSearchEnabled = enabled
    await this.persistPreferences()
    if (enabled) await this.ensureExtras()
    else await this.ocrEngine?.release()
    if (this.canIndex()) void this.processQueue()
    this.emit(true)
    return this.status()
  }

  async setFaceCountEnabled(enabled: boolean): Promise<PhotoSearchStatus> {
    this.assertLive()
    this.preferences.faceCountEnabled = enabled
    await this.persistPreferences()
    if (enabled) await this.ensureExtras()
    else this.engine?.releaseFaceModel()
    if (this.canIndex()) void this.processQueue()
    this.emit(true)
    return this.status()
  }

  /**
   * Clears every stored text result so it is read again. Deliberately does not
   * touch the CLIP vectors: rebuilding the text index must not cost a full
   * re-embedding of the library.
   */
  async rebuildTextIndex(): Promise<PhotoSearchStatus> {
    this.generation += 1
    this.phase2Abort?.abort()
    await this.ocrEngine?.release()
    const now = this.now()
    for (const record of this.index.all()) {
      if (record.status !== 'deleted' && record.ocrStatus !== undefined) {
        await this.index.recordOcr(record.imageId, { status: 'pending', attempts: 0 }, now)
      }
    }
    this.emit(true)
    if (this.canIndex()) void this.processQueue()
    return this.status()
  }

  async rebuildFaceIndex(): Promise<PhotoSearchStatus> {
    this.generation += 1
    this.phase2Abort?.abort()
    this.engine?.releaseFaceModel()
    const now = this.now()
    for (const record of this.index.all()) {
      if (record.status !== 'deleted' && record.faceStatus !== undefined) {
        await this.index.recordFaces(record.imageId, { status: 'pending', attempts: 0 }, now)
      }
    }
    this.emit(true)
    if (this.canIndex()) void this.processQueue()
    return this.status()
  }

  private async ensureExtras(): Promise<void> {
    this.extrasInstalled = await (this.dependencies.isExtrasInstalled?.() ??
      isExtrasPackInstalled(this.dependencies.userDataDir))
    if (this.extrasInstalled) {
      return
    }

    this.preferences.extrasConsented = true
    await this.persistPreferences()
    try {
      if (this.dependencies.downloadExtras) {
        await this.dependencies.downloadExtras({ onProgress: () => undefined })
      } else {
        await downloadExtrasPack(this.dependencies.userDataDir, this.dependencies.modelRuntime, {
          consented: true
        })
      }
      this.extrasInstalled = true
    } catch {
      // Phase 1 is unaffected by this failing, so the feature simply stays off
      // rather than taking semantic search down with it.
      this.extrasInstalled = false
      this.message = 'The text and visible-face models could not be downloaded. Semantic photo search still works.'
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
    this.phase2Abort?.abort()
    this.preferences.paused = true
    await this.persistPreferences()
    this.setPausedState('Photo indexing is paused.')
    this.emit(true)
    return this.status()
  }

  /** Cancels the current run; pending journal rows resume on the next explicit resume. */
  async cancel(): Promise<PhotoSearchStatus> {
    this.phase2Abort?.abort()
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
    this.phase2Abort?.abort()
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
    this.phase2Abort?.abort()
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
    this.phase2Abort?.abort()
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
    // A Phase-2 request needs no visual concept at all: "photos containing the
    // number 1234" and "photos with two people" are answerable from the text
    // and face signals alone.
    const wantsPhase2 = query.containsTextTokens.length > 0 || query.people !== undefined
    if (!this.modelInstalled || (query.concepts.length === 0 && !wantsPhase2)) {
      return { ...base, candidates: [], available: false, message: 'The local photo model is unavailable. I searched filenames and dates only.' }
    }

    try {
      const queryVector = query.concepts.length > 0 ? await this.embedder.embed(query.concepts) : undefined
      const vectors = queryVector ? await this.index.readAllVectors() : new Map<string, Float32Array>()
      const { ranked, coverage } = rankHybridPhotos(
        this.index.indexed(),
        vectors,
        queryVector,
        query,
        this.now()
      )
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
      return { ...base, candidates, available: true, message: coverageMessage(coverage) }
    } catch {
      return { ...base, candidates: [], available: false, message: 'Intelligent photo search is temporarily unavailable. I searched filenames and dates only.' }
    }
  }

  async shutdown(): Promise<void> {
    this.phase2Abort?.abort()
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
    this.phase2Abort = new AbortController()
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
    // Only once every embedding is done, so Phase-1 search becomes usable as
    // early as possible and Phase-2 fills in behind it.
    await this.processPhase2(generation)
    if (generation !== this.generation) return

    if (this.index.shouldCompact()) await this.index.compact()
    this.state = 'ready'
    this.message = this.index.counts().failed > 0 ? 'Some photos could not be indexed; changed files will be retried.' : undefined
    this.emit(true)
  }

  /**
   * Runs Phase-2 work once the embedding queue is empty.
   *
   * The ordering is deliberate. Semantic embeddings come first because they are
   * what makes search work at all, and Phase-1 must stay usable while Phase-2
   * catches up. Face scanning comes next because it is cheap. OCR is last and
   * yields longest, because it is by far the most expensive per image.
   *
   * One image at a time, and the generation is rechecked at every step, so a
   * revoked root or a pause stops the loop rather than finishing the batch.
   */
  private async processPhase2(generation: number): Promise<void> {
    if (!this.canIndexPhase2()) {
      return
    }

    const signal = this.phase2Abort?.signal

    // Faces before text.
    while (this.canIndexPhase2() && generation === this.generation) {
      const record = this.nextFaceRecord()
      if (!record) break
      await this.scanFaces(record, generation, signal)
      this.emit()
      await this.wait(Math.max(FACE_YIELD_MS, this.realtimeActive ? REALTIME_YIELD_MS : 0))
    }

    while (this.canIndexPhase2() && generation === this.generation) {
      const record = this.nextOcrRecord()
      if (!record) break
      await this.readText(record, generation, signal)
      this.emit()
      await this.wait(Math.max(OCR_YIELD_MS, this.realtimeActive ? REALTIME_YIELD_MS : 0))
    }

    // Neither model needs to stay resident once its backlog is clear.
    if (!this.nextFaceRecord()) this.engine?.releaseFaceModel()
    if (!this.nextOcrRecord()) await this.ocrEngine?.release()
  }

  /** Prefers screenshots and document-like images, which is where text lives. */
  private nextOcrRecord(): PhotoIndexRecord | undefined {
    if (!this.preferences.textSearchEnabled || !this.extrasInstalled) {
      return undefined
    }
    const pending = this.index
      .indexed()
      .filter((record) => record.ocrStatus === undefined || record.ocrStatus === 'pending')
    if (pending.length === 0) {
      return undefined
    }
    return pending.find((record) => looksLikeText(record)) ?? pending[0]
  }

  private nextFaceRecord(): PhotoIndexRecord | undefined {
    if (!this.preferences.faceCountEnabled || !this.extrasInstalled) {
      return undefined
    }
    return this.index
      .indexed()
      .find((record) => record.faceStatus === undefined || record.faceStatus === 'pending')
  }

  private async scanFaces(record: PhotoIndexRecord, generation: number, signal?: AbortSignal): Promise<void> {
    const snapshot = await this.snapshotFor(record)
    if (!snapshot) {
      // The root is gone or the file moved; the reconcile pass owns that.
      return
    }

    try {
      const { bitmap } = await decodeForFaceDetection(snapshot, this.dependencies.decodeThumbnail)
      const scores = await this.getEngine().detectFaces(bitmap)
      // Re-checked after inference: authority may have been revoked while the
      // detector was running, and a result computed under it must not be kept.
      if (generation !== this.generation || signal?.aborted || !(await revalidateSnapshot(snapshot))) {
        return
      }
      const counts = countFaces(scores)
      await this.index.recordFaces(
        record.imageId,
        { status: 'done', visibleFaceCount: counts.visible, uncertainFaceCount: counts.uncertain },
        this.now()
      )
    } catch (error) {
      if (generation !== this.generation || signal?.aborted) {
        return
      }
      const code = faceFailureCode(error)
      const attempts = (record.faceAttempts ?? 0) + 1
      // A permanent failure is only retried after the file itself changes,
      // which rewrites the record and clears this status.
      const transient = code === 'file_locked' || code === 'detection_failed'
      await this.index.recordFaces(
        record.imageId,
        {
          status: transient && attempts < MAX_TRANSIENT_ATTEMPTS ? 'pending' : transient ? 'failed' : 'skipped',
          failureCode: code,
          attempts
        },
        this.now()
      )
    }
  }

  private async readText(record: PhotoIndexRecord, generation: number, signal?: AbortSignal): Promise<void> {
    const snapshot = await this.snapshotFor(record)
    if (!snapshot) {
      return
    }

    try {
      const image = await decodeForOcr(snapshot, this.dependencies.decodeThumbnail)
      const result = await this.getOcrEngine().recognize(image.png, signal)
      if (generation !== this.generation || signal?.aborted || !(await revalidateSnapshot(snapshot))) {
        return
      }
      await this.index.recordOcr(
        record.imageId,
        { status: 'done', text: result.text, tokens: result.tokens },
        this.now()
      )
    } catch (error) {
      if (generation !== this.generation || signal?.aborted) {
        return
      }
      const code = ocrFailureCode(error)
      const attempts = (record.ocrAttempts ?? 0) + 1
      const transient = code === 'file_locked' || code === 'ocr_failed' || code === 'ocr_timeout'
      await this.index.recordOcr(
        record.imageId,
        {
          status: transient && attempts < MAX_TRANSIENT_ATTEMPTS ? 'pending' : transient ? 'failed' : 'skipped',
          failureCode: code,
          attempts
        },
        this.now()
      )
    }
  }

  /** Rebuilds an absolute path from the live root store, never from the index. */
  private async snapshotFor(record: PhotoIndexRecord): Promise<PhotoFileSnapshot | undefined> {
    const root = (await this.liveRoots()).find((candidate) => candidate.id === record.rootId)
    if (!root) {
      return undefined
    }
    const absolutePath = join(root.canonicalPath, ...record.relativePath.split('/'))
    const snapshot = recordSnapshot(record, root.canonicalPath, absolutePath)
    return (await revalidateSnapshot(snapshot)) ? snapshot : undefined
  }

  private canIndexPhase2(): boolean {
    return this.canIndex() && this.extrasInstalled
  }

  private getOcrEngine(): LocalOcrEngine {
    this.ocrEngine ??= new LocalOcrEngine({
      languageDirectory: extrasLanguageDirectory(this.dependencies.userDataDir),
      ...(this.dependencies.createOcrWorker ? { createWorker: this.dependencies.createOcrWorker } : {})
    })
    return this.ocrEngine
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
    this.phase2Abort?.abort()
    void this.ocrEngine?.dispose()
    this.ocrEngine = undefined
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

/**
 * Tells the user what the answer could not cover, rather than presenting a
 * partial index as a complete one. Coverage is a count of images, never a
 * filename or a path.
 */
/**
 * Screenshots and photographed documents are where searchable text actually
 * lives, so they are read first. Everything else is still read, just later.
 */
function looksLikeText(record: PhotoIndexRecord): boolean {
  return classifyFileKind(record.name, extname(record.name), record.relativePath.split('/').slice(0, -1)) === 'screenshot'
}

/** Maps a decode or engine failure onto the index's bounded face codes. */
function faceFailureCode(error: unknown): FaceFailureCode {
  if (error instanceof PhotoDecodeError) {
    switch (error.code) {
      case 'file_locked':
        return 'file_locked'
      case 'too_many_pixels':
        return 'too_many_pixels'
      case 'unsupported_format':
        return 'unsupported_format'
      default:
        return 'decode_failed'
    }
  }
  return 'detection_failed'
}

function ocrFailureCode(error: unknown): OcrFailureCode {
  if (error instanceof OcrEngineError) {
    return error.code
  }
  if (error instanceof PhotoDecodeError) {
    switch (error.code) {
      case 'file_locked':
        return 'file_locked'
      case 'too_many_pixels':
        return 'too_many_pixels'
      case 'unsupported_format':
        return 'unsupported_format'
      default:
        return 'decode_failed'
    }
  }
  return 'ocr_failed'
}

function coverageMessage(coverage: HybridCoverage): string | undefined {
  const parts: string[] = []
  if (coverage.ocrUnchecked > 0) {
    parts.push(
      coverage.ocrUnchecked === 1
        ? '1 photo has not been checked for text yet'
        : `${coverage.ocrUnchecked} photos have not been checked for text yet`
    )
  }
  if (coverage.faceUnchecked > 0) {
    parts.push(
      coverage.faceUnchecked === 1
        ? '1 photo has not been checked for visible faces yet'
        : `${coverage.faceUnchecked} photos have not been checked for visible faces yet`
    )
  }
  return parts.length > 0 ? `${parts.join(', and ')}.` : undefined
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
      onlyWhilePluggedIn: value.onlyWhilePluggedIn !== false,
      textSearchEnabled: value.textSearchEnabled === true,
      faceCountEnabled: value.faceCountEnabled === true,
      extrasConsented: value.extrasConsented === true
    }
  } catch {
    return {
      enabled: false,
      consented: false,
      paused: false,
      onlyWhilePluggedIn: true,
      textSearchEnabled: false,
      faceCountEnabled: false,
      extrasConsented: false
    }
  }
}

async function writeJsonAtomic(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true })
  const temporary = `${path}.tmp`
  await writeFile(temporary, JSON.stringify(value, null, 2), 'utf8')
  await rename(temporary, path)
}
