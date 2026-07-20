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
import { EXTRAS_PACK_ID } from './extras-manifest'
import { MODEL_PACK_ID } from './manifest'
import { PEOPLE_PACK_ID } from './people-manifest'

export const VISION_MODEL_DIRECTORY = 'vision-models'
export const PHOTO_INDEX_DIRECTORY = 'photo-index'
const DOWNLOAD_STAGING_DIRECTORY = 'downloads'

export function visionModelDirectory(userDataDir: string): string {
  return join(userDataDir, VISION_MODEL_DIRECTORY)
}

/**
 * Where a verified pack lives: %APPDATA%\lifelens\vision-models\<pack-id>\
 *
 * Each pack gets its own directory keyed by its own id, so installing or
 * clearing the Phase-2 extras cannot touch a byte of the Phase-1 CLIP pack.
 */
export function packDirectory(userDataDir: string, packId: string): string {
  return join(visionModelDirectory(userDataDir), packId)
}

/**
 * Partial downloads are staged here and moved into the pack directory only
 * after their digest verifies, so an unverified byte is never loadable.
 */
export function packStagingDirectory(userDataDir: string, packId: string): string {
  return join(visionModelDirectory(userDataDir), DOWNLOAD_STAGING_DIRECTORY, packId)
}

/** Resolves one manifest filename inside a verified pack directory. */
export function packAssetPath(userDataDir: string, packId: string, fileName: string): string {
  return join(packDirectory(userDataDir, packId), fileName)
}

export function modelPackDirectory(userDataDir: string): string {
  return packDirectory(userDataDir, MODEL_PACK_ID)
}

export function downloadStagingDirectory(userDataDir: string): string {
  return packStagingDirectory(userDataDir, MODEL_PACK_ID)
}

export function modelAssetPath(userDataDir: string, fileName: string): string {
  return packAssetPath(userDataDir, MODEL_PACK_ID, fileName)
}

export function extrasPackDirectory(userDataDir: string): string {
  return packDirectory(userDataDir, EXTRAS_PACK_ID)
}

export function extrasStagingDirectory(userDataDir: string): string {
  return packStagingDirectory(userDataDir, EXTRAS_PACK_ID)
}

export function extrasAssetPath(userDataDir: string, fileName: string): string {
  return packAssetPath(userDataDir, EXTRAS_PACK_ID, fileName)
}

export function peoplePackDirectory(userDataDir: string): string {
  return packDirectory(userDataDir, PEOPLE_PACK_ID)
}

export function peopleStagingDirectory(userDataDir: string): string {
  return packStagingDirectory(userDataDir, PEOPLE_PACK_ID)
}

export function peopleAssetPath(userDataDir: string, fileName: string): string {
  return packAssetPath(userDataDir, PEOPLE_PACK_ID, fileName)
}

export function photoIndexDirectory(userDataDir: string): string {
  return join(userDataDir, PHOTO_INDEX_DIRECTORY)
}

/**
 * Where enrolled person profiles live. A directory of its own, entirely outside
 * `photo-index`, so "delete all people data" is a single recursive removal that
 * cannot take a CLIP vector or an OCR result with it — and so the biometric data
 * is never interleaved into a file that other features rewrite.
 */
export const PEOPLE_DIRECTORY = 'people'

export function peopleDirectory(userDataDir: string): string {
  return join(userDataDir, PEOPLE_DIRECTORY)
}

export const PEOPLE_PROFILE_FILE = 'profiles.json'

export const INDEX_JOURNAL_FILE = 'records.jsonl'
export const INDEX_VECTOR_FILE = 'vectors.bin'
export const INDEX_META_FILE = 'index-meta.json'
/**
 * The single pointer file naming the active generation directory. Its atomic
 * replace is what makes a compaction switch generations without ever exposing a
 * half-written one.
 */
export const INDEX_POINTER_FILE = 'CURRENT'
