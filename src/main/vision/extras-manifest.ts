/**
 * The one allowlisted Phase-2 extras pack.
 *
 * This is a *separate* pack from the Phase-1 CLIP manifest on purpose. Adding
 * OCR and face-detection assets must not change `MODEL_PACK_ID` or
 * `MODEL_PACK_VERSION`, because that would invalidate every stored CLIP vector
 * and force a full re-index of the user's photo library to gain a feature that
 * has nothing to do with the embedding space.
 *
 * The same rules as `manifest.ts` apply, and for the same reason: every URL,
 * filename, size, and digest here is a compile-time application-authored
 * constant. Nothing in this file may ever be derived from the renderer, an IPC
 * payload, the Realtime session, model output, or conversational text. There is
 * no parameter anywhere for a caller-supplied URL, hash, filename, or pack id.
 *
 * Both revisions are pinned to specific commits rather than branches, so the
 * bytes behind these URLs cannot change under us. Each digest below was taken
 * by fetching that exact pinned revision and hashing the result locally.
 */

export const EXTRAS_PACK_ID = 'photo-search-extras'

/**
 * Bumped only when the *pack contents* change. Kept independent of
 * MODEL_PACK_VERSION so the two can move without disturbing each other.
 */
export const EXTRAS_PACK_VERSION = 1

/**
 * These two versions are what the index records against OCR and face results
 * respectively. Bumping one invalidates only that Phase-2 signal: the stored
 * CLIP vectors, and the other Phase-2 signal, are untouched. See
 * `index-store.ts`, which drops the stale fields but keeps the record and its
 * vector row.
 */
export const OCR_MODEL_VERSION = 1
export const FACE_MODEL_VERSION = 1

const TESSDATA_REPOSITORY = 'tesseract-ocr/tessdata_fast'
const TESSDATA_REVISION = '923915d4ced2a7235221788285785a29c4a42d4a'

const YUNET_REPOSITORY = 'opencv/opencv_zoo'
const YUNET_REVISION = 'f12e12798e8314f7c074a6656816c048dcc95b7a'

/**
 * Two hosts, because the two assets are served differently. `raw` serves
 * ordinary git blobs; the YuNet weights are a git-LFS object, which `raw` would
 * answer with a 130-byte pointer file rather than the model. `media` is
 * GitHub's LFS content endpoint. Both are pinned to a commit, so both are
 * immutable.
 *
 * As with the CLIP pack, integrity does not rest on the host: every file is
 * checked against its pinned SHA-256 before it is installed, so even a redirect
 * to a hostile host cannot install altered bytes.
 */
export const ALLOWED_EXTRAS_DOWNLOAD_HOSTS: readonly string[] = Object.freeze([
  'raw.githubusercontent.com',
  'media.githubusercontent.com'
])

export type ExtrasAssetRole = 'ocrTrainedData' | 'faceModel'

export interface ExtrasAsset {
  role: ExtrasAssetRole
  /** Filename inside the pack directory. Never taken from a server response. */
  fileName: string
  url: string
  sizeBytes: number
  sha256: string
}

export const EXTRAS_ASSETS: readonly ExtrasAsset[] = Object.freeze([
  Object.freeze({
    role: 'ocrTrainedData',
    // Tesseract resolves a language by filename, so this name is fixed by the
    // OCR engine's own convention rather than chosen here.
    fileName: 'eng.traineddata',
    url: `https://raw.githubusercontent.com/${TESSDATA_REPOSITORY}/${TESSDATA_REVISION}/eng.traineddata`,
    sizeBytes: 4_113_088,
    sha256: '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
  }),
  Object.freeze({
    role: 'faceModel',
    fileName: 'face_detection_yunet_2023mar.onnx',
    url: `https://media.githubusercontent.com/media/${YUNET_REPOSITORY}/${YUNET_REVISION}/models/face_detection_yunet/face_detection_yunet_2023mar.onnx`,
    sizeBytes: 232_589,
    sha256: '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
  })
]) as readonly ExtrasAsset[]

export const EXTRAS_PACK_TOTAL_BYTES = EXTRAS_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0)

/**
 * Shown verbatim in the settings card before the user consents to a download.
 * Deliberately states what the face model does and does not do, because "face
 * model" reads as identity recognition to most people and this one cannot do
 * that.
 */
export const EXTRAS_PACK_LICENSE_NOTICE =
  'Tesseract English training data by the Tesseract OCR project, Apache 2.0 licensed. ' +
  'YuNet face detection by Shiqi Yu and Yuantao Feng, MIT licensed. ' +
  'YuNet finds where faces are so Lumi can count them; it cannot recognise who anyone is. ' +
  'Both are downloaded once and stored only on this device.'

export const EXTRAS_PACK_DISPLAY_NAME = 'Text and visible-face detection'

export function extrasAssetFor(role: ExtrasAssetRole): ExtrasAsset {
  const asset = EXTRAS_ASSETS.find((candidate) => candidate.role === role)
  if (!asset) {
    throw new Error(`The extras manifest is missing its ${role} entry.`)
  }
  return asset
}

/**
 * Rejects anything that is not one of the compiled-in URLs, so a mutated
 * manifest entry cannot point the fetch at a new origin. Checks the *initial*
 * URL only; see the note above on why integrity rests on the digest instead.
 */
export function isAllowlistedExtrasUrl(candidate: string): boolean {
  if (!EXTRAS_ASSETS.some((asset) => asset.url === candidate)) {
    return false
  }

  try {
    const url = new URL(candidate)
    return url.protocol === 'https:' && ALLOWED_EXTRAS_DOWNLOAD_HOSTS.includes(url.hostname)
  } catch {
    return false
  }
}
