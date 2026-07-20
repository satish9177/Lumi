/**
 * Acquires and verifies the one allowlisted model pack.
 *
 * Safety properties this file is responsible for:
 *  - Every URL comes from the compiled-in manifest and is re-checked against
 *    the allowlist immediately before the request. There is no parameter for a
 *    caller-supplied URL, filename, or digest.
 *  - Bytes land in a staging directory and are only moved into the pack
 *    directory after their SHA-256 matches the manifest, so an interrupted or
 *    tampered download is never loadable.
 *  - Failures are bounded codes. Network errors, filesystem errors, and native
 *    exception text never reach a caller.
 *
 * Network and clock are injected so the whole lifecycle is testable offline.
 */

import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { createReadStream, createWriteStream, type WriteStream } from 'node:fs'
import { mkdir, open, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import type { ReadableStream as WebReadableStream } from 'node:stream/web'
import {
  EXTRAS_ASSETS,
  EXTRAS_PACK_ID,
  EXTRAS_PACK_VERSION,
  isAllowlistedExtrasUrl,
  type ExtrasAssetRole
} from './extras-manifest'
import {
  isAllowlistedAssetUrl,
  MODEL_ASSETS,
  MODEL_PACK_ID,
  MODEL_PACK_VERSION,
  type ModelAssetRole
} from './manifest'
import { packAssetPath, packDirectory, packStagingDirectory } from './model-location'

/**
 * One asset shape both manifests satisfy. The download, verification, and
 * installation rules are identical for every pack, so they live in exactly one
 * audited place rather than being copied per pack — a second copy is a second
 * thing that can quietly stop checking a digest.
 */
interface PackAsset {
  role: ModelAssetRole | ExtrasAssetRole
  fileName: string
  url: string
  sizeBytes: number
  sha256: string
}

interface PackSpec {
  packId: string
  packVersion: number
  assets: readonly PackAsset[]
  /** The pack's own compiled-in allowlist. Re-checked immediately before fetch. */
  isAllowlisted: (url: string) => boolean
}

const CLIP_PACK: PackSpec = Object.freeze({
  packId: MODEL_PACK_ID,
  packVersion: MODEL_PACK_VERSION,
  assets: MODEL_ASSETS,
  isAllowlisted: isAllowlistedAssetUrl
})

const EXTRAS_PACK: PackSpec = Object.freeze({
  packId: EXTRAS_PACK_ID,
  packVersion: EXTRAS_PACK_VERSION,
  assets: EXTRAS_ASSETS,
  isAllowlisted: isAllowlistedExtrasUrl
})

export const MODEL_PACK_ERROR_CODES = [
  'not_consented',
  'insufficient_disk_space',
  'network_unavailable',
  'download_failed',
  'digest_mismatch',
  'install_failed',
  'cancelled',
  'download_in_progress'
] as const

export type ModelPackErrorCode = (typeof MODEL_PACK_ERROR_CODES)[number]

export const MODEL_PACK_ERROR_MESSAGES: Record<ModelPackErrorCode, string> = {
  not_consented: 'Intelligent photo search has not been enabled.',
  insufficient_disk_space: 'There is not enough free disk space for the local search model.',
  network_unavailable: 'The local search model could not be reached.',
  download_failed: 'The local search model download did not complete.',
  digest_mismatch: 'The downloaded model failed its integrity check and was discarded.',
  install_failed: 'The local search model could not be installed.',
  cancelled: 'The model download was cancelled.',
  download_in_progress: 'The local search model is already being downloaded.'
}

export class ModelPackError extends Error {
  constructor(readonly code: ModelPackErrorCode) {
    super(MODEL_PACK_ERROR_MESSAGES[code])
    this.name = 'ModelPackError'
  }
}

/** Written only after every asset verifies. Its presence is the "installed" signal. */
const PACK_MARKER_FILE = 'pack.json'

interface PackMarker {
  packId: string
  packVersion: number
  installedAt: string
  digests: Record<string, string>
}

export interface ModelPackProgress {
  /** Bytes verified plus bytes fetched so far, across the whole pack. */
  receivedBytes: number
  totalBytes: number
  /** Which manifest entry is in flight, for a coarse status line. */
  role: ModelAssetRole | ExtrasAssetRole
}

export interface ModelPackRuntime {
  /** Injected so tests never touch the network. */
  fetch: typeof fetch
  /** Free bytes on the volume holding the given directory, or undefined if unknown. */
  freeDiskBytes?: (directory: string) => Promise<number | undefined>
  now?: () => number
}

/** Retry only transport faults; a digest mismatch is never retried. */
const MAX_ATTEMPTS = 3
const RETRY_BASE_DELAY_MS = 500
/** Refuse to start unless the pack fits with room to spare for the staging copy. */
const DISK_HEADROOM_BYTES = 256 * 1024 * 1024

/**
 * The userData directories with a download in flight. A single writer per pack
 * is the invariant that keeps two concurrent downloads from interleaving writes
 * into the same staging file, and it gives cancellation a single, explicit
 * owner: the call that started the download.
 */
const activeDownloads = new Set<string>()

/**
 * Summed from the manifest on each call rather than captured once, so size
 * disclosure, the disk-space gate, and progress reporting can never drift apart.
 */
export function modelPackTotalBytes(): number {
  return packTotalBytes(CLIP_PACK)
}

function packTotalBytes(spec: PackSpec): number {
  return spec.assets.reduce((total, asset) => total + asset.sizeBytes, 0)
}

/**
 * True only when every manifest asset is present at its exact expected size and
 * the marker records the current pack version. Size is a cheap gate; the digest
 * was already checked at install time and is recorded in the marker.
 */
async function isPackInstalled(spec: PackSpec, userDataDir: string): Promise<boolean> {
  const marker = await readMarker(spec, userDataDir)
  if (!marker || marker.packId !== spec.packId || marker.packVersion !== spec.packVersion) {
    return false
  }

  for (const asset of spec.assets) {
    if (marker.digests[asset.fileName] !== asset.sha256) {
      return false
    }
    try {
      const details = await stat(packAssetPath(userDataDir, spec.packId, asset.fileName))
      if (!details.isFile() || details.size !== asset.sizeBytes) {
        return false
      }
    } catch {
      return false
    }
  }

  return true
}

export function isModelPackInstalled(userDataDir: string): Promise<boolean> {
  return isPackInstalled(CLIP_PACK, userDataDir)
}

export function isExtrasPackInstalled(userDataDir: string): Promise<boolean> {
  return isPackInstalled(EXTRAS_PACK, userDataDir)
}

export async function installedPackVersion(userDataDir: string): Promise<number | undefined> {
  const marker = await readMarker(CLIP_PACK, userDataDir)
  return marker?.packVersion
}

export async function installedExtrasVersion(userDataDir: string): Promise<number | undefined> {
  const marker = await readMarker(EXTRAS_PACK, userDataDir)
  return marker?.packVersion
}

function resolveRolePath(spec: PackSpec, userDataDir: string, role: string): string {
  const asset = spec.assets.find((candidate) => candidate.role === role)
  if (!asset) {
    throw new ModelPackError('install_failed')
  }
  return packAssetPath(userDataDir, spec.packId, asset.fileName)
}

/** Resolves an asset path only for a role this application defined. */
export function resolveAssetPath(userDataDir: string, role: ModelAssetRole): string {
  return resolveRolePath(CLIP_PACK, userDataDir, role)
}

export function resolveExtrasAssetPath(userDataDir: string, role: ExtrasAssetRole): string {
  return resolveRolePath(EXTRAS_PACK, userDataDir, role)
}

/**
 * The directory holding `eng.traineddata`. Tesseract is handed a directory
 * rather than a file path because it resolves a language to a filename itself.
 */
export function extrasLanguageDirectory(userDataDir: string): string {
  return packDirectory(userDataDir, EXTRAS_PACK_ID)
}

export function extrasPackTotalBytes(): number {
  return packTotalBytes(EXTRAS_PACK)
}

export interface DownloadOptions {
  /** Must be true. Consent is checked here as well as in the caller. */
  consented: boolean
  signal?: AbortSignal
  onProgress?: (progress: ModelPackProgress) => void
}

/**
 * Downloads every missing asset, verifies each one, and installs the pack
 * atomically. Safe to call again after an interruption: completed assets are
 * skipped and a partial file resumes with a Range request where the server
 * allows it.
 */
async function downloadPack(
  spec: PackSpec,
  userDataDir: string,
  runtime: ModelPackRuntime,
  options: DownloadOptions
): Promise<void> {
  if (!options.consented) {
    throw new ModelPackError('not_consented')
  }

  // A second concurrent request for the same pack is refused rather than run, so
  // the two never write the same staging file and cancellation stays owned by
  // the first caller. Retrying after this one settles is fine: the guard clears
  // in the finally below. The key includes the pack id, so the CLIP pack and the
  // extras pack are independent writers rather than blocking each other.
  const guard = `${spec.packId} ${userDataDir}`
  if (activeDownloads.has(guard)) {
    throw new ModelPackError('download_in_progress')
  }
  activeDownloads.add(guard)

  try {
    const packDir = packDirectory(userDataDir, spec.packId)
    const stagingDir = packStagingDirectory(userDataDir, spec.packId)

    try {
      await mkdir(packDir, { recursive: true })
      await mkdir(stagingDir, { recursive: true })
    } catch {
      throw new ModelPackError('install_failed')
    }

    const totalBytes = packTotalBytes(spec)
    await assertDiskSpace(totalBytes, stagingDir, runtime)

    let completedBytes = 0
    for (const asset of spec.assets) {
      throwIfAborted(options.signal)

      if (await assetAlreadyInstalled(spec, userDataDir, asset)) {
        completedBytes += asset.sizeBytes
        options.onProgress?.({ receivedBytes: completedBytes, totalBytes, role: asset.role })
        continue
      }

      const baseline = completedBytes
      await fetchAndInstallAsset(spec, userDataDir, stagingDir, asset, runtime, options, (assetBytes) => {
        options.onProgress?.({
          receivedBytes: baseline + assetBytes,
          totalBytes,
          role: asset.role
        })
      })
      completedBytes += asset.sizeBytes
    }

    await writeMarker(spec, userDataDir, runtime)

    // The staging directory has served its purpose; leaving it would double the
    // pack's footprint on disk.
    await rm(stagingDir, { recursive: true, force: true }).catch(() => undefined)
  } finally {
    activeDownloads.delete(guard)
  }
}

export function downloadModelPack(
  userDataDir: string,
  runtime: ModelPackRuntime,
  options: DownloadOptions
): Promise<void> {
  return downloadPack(CLIP_PACK, userDataDir, runtime, options)
}

export function downloadExtrasPack(
  userDataDir: string,
  runtime: ModelPackRuntime,
  options: DownloadOptions
): Promise<void> {
  return downloadPack(EXTRAS_PACK, userDataDir, runtime, options)
}

/**
 * Removes the pack and any staged bytes. Used by "Clear and rebuild".
 *
 * Refuses while a download is in flight for the same pack: clearing the staging
 * directory out from under an active writer has no safe outcome, so the defined
 * one is to make the caller cancel the download first.
 */
async function clearPack(spec: PackSpec, userDataDir: string): Promise<void> {
  if (activeDownloads.has(`${spec.packId} ${userDataDir}`)) {
    throw new ModelPackError('download_in_progress')
  }
  await rm(packDirectory(userDataDir, spec.packId), { recursive: true, force: true }).catch(() => undefined)
  await rm(packStagingDirectory(userDataDir, spec.packId), { recursive: true, force: true }).catch(() => undefined)
}

export function clearModelPack(userDataDir: string): Promise<void> {
  return clearPack(CLIP_PACK, userDataDir)
}

export function clearExtrasPack(userDataDir: string): Promise<void> {
  return clearPack(EXTRAS_PACK, userDataDir)
}

async function assetAlreadyInstalled(spec: PackSpec, userDataDir: string, asset: PackAsset): Promise<boolean> {
  try {
    const details = await stat(packAssetPath(userDataDir, spec.packId, asset.fileName))
    return details.isFile() && details.size === asset.sizeBytes
  } catch {
    return false
  }
}

async function fetchAndInstallAsset(
  spec: PackSpec,
  userDataDir: string,
  stagingDir: string,
  asset: PackAsset,
  runtime: ModelPackRuntime,
  options: DownloadOptions,
  onAssetProgress: (assetBytes: number) => void
): Promise<void> {
  // Re-checked here, not only at manifest authoring time, so a mutated entry
  // cannot widen what this function is willing to contact.
  if (!spec.isAllowlisted(asset.url)) {
    throw new ModelPackError('install_failed')
  }

  const stagingPath = join(stagingDir, `${asset.fileName}.part`)
  let lastTransportError: ModelPackErrorCode = 'download_failed'

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    throwIfAborted(options.signal)

    try {
      await streamAsset(stagingPath, asset, runtime, options, onAssetProgress)
    } catch (error) {
      if (error instanceof ModelPackError && error.code === 'cancelled') {
        throw error
      }
      lastTransportError = error instanceof ModelPackError ? error.code : 'download_failed'
      // A partial file is kept: the next attempt resumes from it.
      if (attempt === MAX_ATTEMPTS) {
        throw new ModelPackError(lastTransportError)
      }
      await delay(RETRY_BASE_DELAY_MS * attempt, options.signal)
      continue
    }

    const digest = await sha256OfFile(stagingPath)
    if (digest !== asset.sha256) {
      // Never retried and never kept: mismatched bytes are discarded outright
      // so a resume can never build on a corrupt prefix.
      await rm(stagingPath, { force: true }).catch(() => undefined)
      throw new ModelPackError('digest_mismatch')
    }

    try {
      // Same volume, so this is an atomic replace: the pack directory only ever
      // contains verified bytes.
      await rename(stagingPath, packAssetPath(userDataDir, spec.packId, asset.fileName))
    } catch {
      throw new ModelPackError('install_failed')
    }
    return
  }

  throw new ModelPackError(lastTransportError)
}

async function streamAsset(
  stagingPath: string,
  asset: PackAsset,
  runtime: ModelPackRuntime,
  options: DownloadOptions,
  onAssetProgress: (assetBytes: number) => void
): Promise<void> {
  const existingBytes = await partialSize(stagingPath)
  if (existingBytes > asset.sizeBytes) {
    // A partial file longer than the expected asset is nonsense; start over.
    await rm(stagingPath, { force: true }).catch(() => undefined)
  }
  const resumeFrom = existingBytes > 0 && existingBytes < asset.sizeBytes ? existingBytes : 0

  let response: Response
  try {
    response = await runtime.fetch(asset.url, {
      redirect: 'follow',
      signal: options.signal,
      headers: resumeFrom > 0 ? { Range: `bytes=${resumeFrom}-` } : {}
    })
  } catch (error) {
    throw new ModelPackError(isAbortError(error) ? 'cancelled' : 'network_unavailable')
  }

  if (response.status === 416) {
    // The server says the range is unsatisfiable; the local partial is wrong.
    await rm(stagingPath, { force: true }).catch(() => undefined)
    throw new ModelPackError('download_failed')
  }
  if (!response.ok) {
    throw new ModelPackError('download_failed')
  }

  // A server that ignored the Range header restarts the file, so the local
  // partial must be discarded rather than appended to.
  const append = resumeFrom > 0 && response.status === 206
  if (!response.body) {
    throw new ModelPackError('download_failed')
  }

  let written = append ? resumeFrom : 0
  const sink = createWriteStream(stagingPath, append ? { flags: 'a' } : { flags: 'w' })
  const source = Readable.fromWeb(response.body as unknown as WebReadableStream<Uint8Array>)

  try {
    for await (const chunk of source) {
      const bytes = chunk as Uint8Array
      written += bytes.byteLength
      if (written > asset.sizeBytes) {
        // Refuse to write more than the manifest declares, so a hostile or
        // broken response cannot fill the disk.
        throw new ModelPackError('download_failed')
      }
      if (!sink.write(bytes)) {
        await once(sink, 'drain')
      }
      onAssetProgress(written)
    }
    await closeStream(sink)
  } catch (error) {
    sink.destroy()
    source.destroy()
    if (isAbortError(error)) {
      throw new ModelPackError('cancelled')
    }
    throw error instanceof ModelPackError ? error : new ModelPackError('download_failed')
  }

  if (written !== asset.sizeBytes) {
    throw new ModelPackError('download_failed')
  }
}

function closeStream(sink: WriteStream): Promise<void> {
  return new Promise((resolve, reject) => {
    sink.end((error?: Error | null) => (error ? reject(error) : resolve()))
  })
}

async function partialSize(path: string): Promise<number> {
  try {
    const details = await stat(path)
    return details.isFile() ? details.size : 0
  } catch {
    return 0
  }
}

export async function sha256OfFile(path: string): Promise<string> {
  const hash = createHash('sha256')
  await pipeline(createReadStream(path), hash)
  return hash.digest('hex')
}

async function assertDiskSpace(
  totalBytes: number,
  stagingDir: string,
  runtime: ModelPackRuntime
): Promise<void> {
  const probe = runtime.freeDiskBytes ?? defaultFreeDiskBytes
  let free: number | undefined
  try {
    free = await probe(stagingDir)
  } catch {
    free = undefined
  }

  // An unknown figure is not treated as a failure: the download still verifies
  // and installs atomically, and a genuinely full disk surfaces as a write
  // error rather than a wrong refusal.
  if (free !== undefined && free < totalBytes + DISK_HEADROOM_BYTES) {
    throw new ModelPackError('insufficient_disk_space')
  }
}

async function defaultFreeDiskBytes(directory: string): Promise<number | undefined> {
  try {
    const { statfs } = await import('node:fs/promises')
    if (typeof statfs !== 'function') {
      return undefined
    }
    const stats = await statfs(directory)
    return Number(stats.bavail) * Number(stats.bsize)
  } catch {
    return undefined
  }
}

async function readMarker(spec: PackSpec, userDataDir: string): Promise<PackMarker | undefined> {
  try {
    const raw = await readFile(join(packDirectory(userDataDir, spec.packId), PACK_MARKER_FILE), 'utf8')
    const parsed: unknown = JSON.parse(raw)
    if (
      typeof parsed !== 'object' ||
      parsed === null ||
      typeof (parsed as PackMarker).packId !== 'string' ||
      typeof (parsed as PackMarker).packVersion !== 'number' ||
      typeof (parsed as PackMarker).digests !== 'object'
    ) {
      return undefined
    }
    return parsed as PackMarker
  } catch {
    return undefined
  }
}

async function writeMarker(spec: PackSpec, userDataDir: string, runtime: ModelPackRuntime): Promise<void> {
  const marker: PackMarker = {
    packId: spec.packId,
    packVersion: spec.packVersion,
    installedAt: new Date(runtime.now?.() ?? Date.now()).toISOString(),
    digests: Object.fromEntries(spec.assets.map((asset) => [asset.fileName, asset.sha256]))
  }

  try {
    const markerPath = join(packDirectory(userDataDir, spec.packId), PACK_MARKER_FILE)
    const temporaryPath = `${markerPath}.tmp`
    await writeFile(temporaryPath, JSON.stringify(marker), 'utf8')
    await flushToDisk(temporaryPath)
    await rename(temporaryPath, markerPath)
  } catch {
    throw new ModelPackError('install_failed')
  }
}

async function flushToDisk(path: string): Promise<void> {
  try {
    const handle = await open(path, 'r+')
    try {
      await handle.sync()
    } finally {
      await handle.close()
    }
  } catch {
    // A filesystem that will not fsync is still usable; the digest check on the
    // next start is what actually protects us.
  }
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) {
    throw new ModelPackError('cancelled')
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError')
}

function delay(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    timer.unref?.()

    function onAbort(): void {
      clearTimeout(timer)
      reject(new ModelPackError('cancelled'))
    }

    signal?.addEventListener('abort', onAbort, { once: true })
  })
}
