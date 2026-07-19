/**
 * The single place that decides where local vision assets live.
 *
 * Every path is the application's own userData directory joined with a
 * compile-time constant. No caller — renderer, IPC, Realtime, model output, or
 * conversational text — can influence any of these, because none of these
 * functions accepts a path or a filename from outside this module's own
 * manifest.
 */

import { join } from 'node:path'
import { MODEL_PACK_ID } from './manifest'

export const VISION_MODEL_DIRECTORY = 'vision-models'
export const PHOTO_INDEX_DIRECTORY = 'photo-index'
const DOWNLOAD_STAGING_DIRECTORY = 'downloads'

export function visionModelDirectory(userDataDir: string): string {
  return join(userDataDir, VISION_MODEL_DIRECTORY)
}

/** Where a verified pack lives: %APPDATA%\lifelens\vision-models\<pack-id>\ */
export function modelPackDirectory(userDataDir: string): string {
  return join(visionModelDirectory(userDataDir), MODEL_PACK_ID)
}

/**
 * Partial downloads are staged here and moved into the pack directory only
 * after their digest verifies, so an unverified byte is never loadable.
 */
export function downloadStagingDirectory(userDataDir: string): string {
  return join(visionModelDirectory(userDataDir), DOWNLOAD_STAGING_DIRECTORY, MODEL_PACK_ID)
}

/** Resolves one manifest filename inside the verified pack directory. */
export function modelAssetPath(userDataDir: string, fileName: string): string {
  return join(modelPackDirectory(userDataDir), fileName)
}

export function photoIndexDirectory(userDataDir: string): string {
  return join(userDataDir, PHOTO_INDEX_DIRECTORY)
}

export const INDEX_JOURNAL_FILE = 'records.jsonl'
export const INDEX_VECTOR_FILE = 'vectors.bin'
export const INDEX_META_FILE = 'index-meta.json'
/**
 * The single pointer file naming the active generation directory. Its atomic
 * replace is what makes a compaction switch generations without ever exposing a
 * half-written one.
 */
export const INDEX_POINTER_FILE = 'CURRENT'
