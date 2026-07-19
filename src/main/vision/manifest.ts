/**
 * The one allowlisted model pack.
 *
 * Every value here is compile-time application-authored. Nothing in this file
 * may ever be derived from the renderer, an IPC payload, the Realtime session,
 * model output, or conversational text — a downloader that accepts a caller's
 * URL or hash is a remote-code-execution primitive, so it simply has no
 * parameter for one.
 *
 * The revision is pinned to a specific commit rather than a branch, so the
 * bytes behind these URLs cannot change under us. The digests were taken from
 * that commit and are re-verified locally after every download.
 */

export const MODEL_PACK_ID = 'clip-vit-base-patch32-q8'

/**
 * Bumped whenever the embedding space changes in a way that invalidates stored
 * vectors. The index records this and rebuilds itself when it no longer matches.
 */
export const MODEL_PACK_VERSION = 1

const REPOSITORY = 'Xenova/clip-vit-base-patch32'
const REVISION = 'd15189d7028b43f1d3e65039190477f6af591c2a'
const RESOLVE_BASE = `https://huggingface.co/${REPOSITORY}/resolve/${REVISION}`

/** The host the downloader is permitted to talk to. Checked per request. */
export const ALLOWED_DOWNLOAD_HOST = 'huggingface.co'

export type ModelAssetRole = 'imageModel' | 'textModel' | 'vocabulary' | 'merges'

export interface ModelAsset {
  role: ModelAssetRole
  /** Filename inside the pack directory. Never taken from a server response. */
  fileName: string
  url: string
  sizeBytes: number
  sha256: string
}

export const MODEL_ASSETS: readonly ModelAsset[] = Object.freeze([
  Object.freeze({
    role: 'imageModel',
    fileName: 'vision_model_quantized.onnx',
    url: `${RESOLVE_BASE}/onnx/vision_model_quantized.onnx`,
    sizeBytes: 89_117_001,
    sha256: '583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299'
  }),
  Object.freeze({
    role: 'textModel',
    fileName: 'text_model_quantized.onnx',
    url: `${RESOLVE_BASE}/onnx/text_model_quantized.onnx`,
    sizeBytes: 64_504_507,
    sha256: '73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a'
  }),
  Object.freeze({
    role: 'vocabulary',
    fileName: 'vocab.json',
    url: `${RESOLVE_BASE}/vocab.json`,
    sizeBytes: 862_328,
    sha256: '5047b556ce86ccaf6aa22b3ffccfc52d391ea4accdab9c2f2407da5b742d4363'
  }),
  Object.freeze({
    role: 'merges',
    fileName: 'merges.txt',
    url: `${RESOLVE_BASE}/merges.txt`,
    sizeBytes: 524_619,
    sha256: '9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a'
  })
]) as readonly ModelAsset[]

export const MODEL_PACK_TOTAL_BYTES = MODEL_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0)

/**
 * Shown verbatim in the settings card before the user consents to a download.
 * CLIP's weights are MIT; the wrapper repository is MIT as well.
 */
export const MODEL_PACK_LICENSE_NOTICE =
  'CLIP ViT-B/32 by OpenAI, ONNX export by Xenova. MIT licensed. Downloaded once and stored only on this device.'

export const MODEL_PACK_DISPLAY_NAME = 'CLIP ViT-B/32 (quantized)'

export function assetFor(role: ModelAssetRole): ModelAsset {
  const asset = MODEL_ASSETS.find((candidate) => candidate.role === role)
  if (!asset) {
    throw new Error(`The model manifest is missing its ${role} entry.`)
  }
  return asset
}

/**
 * Rejects anything that is not one of the compiled-in URLs. The downloader calls
 * this on the initial request URL, so a mutated manifest entry cannot point the
 * fetch at a new origin.
 *
 * This checks the *initial* URL only. Hugging Face answers these URLs with a 302
 * to its CDN (a different host), and the fetch follows that redirect, so this is
 * not host pinning across the whole chain. Integrity does not rest on the host:
 * every downloaded file is checked against its pinned SHA-256 before it is
 * installed, so a redirect to a hostile host still cannot install altered bytes.
 */
export function isAllowlistedAssetUrl(candidate: string): boolean {
  if (!MODEL_ASSETS.some((asset) => asset.url === candidate)) {
    return false
  }

  try {
    const url = new URL(candidate)
    return url.protocol === 'https:' && url.hostname === ALLOWED_DOWNLOAD_HOST
  } catch {
    return false
  }
}

/** A digest is 64 lowercase hex characters; anything else is not comparable. */
export function isSha256Digest(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
}
