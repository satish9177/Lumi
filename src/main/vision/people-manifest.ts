/**
 * The one allowlisted Phase-3 people pack.
 *
 * A *third* pack, separate from both the Phase-1 CLIP manifest and the Phase-2
 * extras manifest, for the same reason the extras pack was split off: installing
 * a face-embedding model must not change `MODEL_PACK_ID` or `EXTRAS_PACK_ID`,
 * because that would invalidate stored CLIP vectors or stored OCR and face-count
 * results and force a re-index to gain a feature unrelated to either.
 *
 * The rules from `manifest.ts` and `extras-manifest.ts` apply unchanged: every
 * URL, filename, size, and digest here is a compile-time application-authored
 * constant. Nothing may ever be derived from the renderer, an IPC payload, the
 * Realtime session, model output, or conversational text. There is no parameter
 * anywhere for a caller-supplied URL, hash, filename, or pack id.
 *
 * ## Why this model, and what its licence actually covers
 *
 * SFace, as distributed by the OpenCV Zoo. The distinction that decided it:
 * opencv_zoo licenses *per model* — its root README says "Please refer to
 * licenses of different models" — and the SFace directory carries its own
 * Apache 2.0 LICENSE together with the sentence "All files in this directory are
 * licensed under Apache 2.0 License". That sentence is what grants the `.onnx`
 * weights, not merely the surrounding Python.
 *
 * The obvious alternative, InsightFace ArcFace, was rejected on its own words:
 * its model zoo states "ALL models are available for non-commercial research
 * purposes only" despite MIT-licensed code. Permissive code does not imply
 * permissive weights, and here the two genuinely diverge.
 *
 * Recorded honestly because it is not visible from the licence: the SFace
 * weights descend from models trained on CASIA-WebFace, VGGFace2 and
 * MS-Celeb-1M, and the model card does not say which produced this export.
 * MS-Celeb-1M was later withdrawn by Microsoft over the provenance of its
 * images. Apache 2.0 grants use and redistribution of the artefact; it says
 * nothing about how those training faces were collected. See
 * THIRD_PARTY_NOTICES.md, where the same fact is stated for users.
 */

export const PEOPLE_PACK_ID = 'photo-search-people'

/** Bumped only when the pack contents change. Independent of the other packs. */
export const PEOPLE_PACK_VERSION = 1

/**
 * Stamped onto every stored reference embedding and every per-photo match
 * record. Bumping it invalidates Phase-3 data *only*: CLIP vectors, OCR text,
 * and visible-face counts are all keyed by their own versions and survive
 * untouched. Enrolled profiles survive too, but are marked as needing
 * re-enrolment, because an embedding from one model is meaningless to another.
 */
export const FACE_EMBED_MODEL_VERSION = 1

/**
 * Bumped when the *matching* rules change — thresholds, aggregation, alignment —
 * without the model itself changing. Invalidates stored match outcomes but not
 * the reference embeddings, so a rescan is enough and re-enrolment is not.
 */
export const PEOPLE_INDEX_VERSION = 1

const SFACE_REPOSITORY = 'opencv/opencv_zoo'
/**
 * The same commit Phase 2 already pins for YuNet. Reusing it is deliberate: the
 * detector that produces the landmarks and the recognizer that consumes them
 * then come from one immutable snapshot of one repository.
 */
const SFACE_REVISION = 'f12e12798e8314f7c074a6656816c048dcc95b7a'

/**
 * The weights are a git-LFS object, so `raw` would answer with a ~133-byte
 * pointer rather than the model; `media` is GitHub's LFS content endpoint. Both
 * are pinned to a commit and therefore immutable.
 *
 * As with every other pack, integrity does not rest on the host: the file is
 * checked against the pinned SHA-256 below before installation, so even a
 * redirect to a hostile host cannot install altered bytes.
 */
export const ALLOWED_PEOPLE_DOWNLOAD_HOSTS: readonly string[] = Object.freeze([
  'media.githubusercontent.com'
])

export type PeopleAssetRole = 'faceEmbedModel'

export interface PeopleAsset {
  role: PeopleAssetRole
  /** Filename inside the pack directory. Never taken from a server response. */
  fileName: string
  url: string
  sizeBytes: number
  sha256: string
}

export const PEOPLE_ASSETS: readonly PeopleAsset[] = Object.freeze([
  Object.freeze({
    role: 'faceEmbedModel',
    fileName: 'face_recognition_sface_2021dec.onnx',
    url: `https://media.githubusercontent.com/media/${SFACE_REPOSITORY}/${SFACE_REVISION}/models/face_recognition_sface/face_recognition_sface_2021dec.onnx`,
    sizeBytes: 38_696_353,
    // Verified twice over: hashed from the downloaded bytes, and independently
    // equal to the `oid` recorded in the repository's own LFS pointer file.
    sha256: '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'
  })
]) as readonly PeopleAsset[]

export const PEOPLE_PACK_TOTAL_BYTES = PEOPLE_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0)

/**
 * The exact input the pinned SFace export accepts, and the width of what it
 * returns. Both were measured against this revision rather than assumed:
 * input `data` [1,3,112,112], output `fc1` [1,128].
 */
export const FACE_EMBED_INPUT_SIZE = 112
export const FACE_EMBED_DIMENSIONS = 128

/**
 * SFace does not return a unit vector — a measured sample had an L2 norm of
 * about 4.8 — so every embedding is normalized by us before it is compared or
 * stored. Cosine similarity on un-normalized vectors would be quietly wrong.
 */
export const FACE_EMBED_NORMALIZED = true

/**
 * Shown verbatim before the user consents to the download. It names the
 * capability limits in the same breath as the licence, because "face
 * recognition" invites people to assume far more than this does.
 */
export const PEOPLE_PACK_LICENSE_NOTICE =
  'SFace face recognition by Yaoyao Zhong, distributed by the OpenCV Zoo under the Apache 2.0 licence. ' +
  'Lumi uses it only to compare faces against people you have labelled yourself on this device. ' +
  'It is downloaded once, stays on this device, and is never used to identify anyone you have not labelled. ' +
  'Face matching can be wrong, so review matches before relying on them.'

export const PEOPLE_PACK_DISPLAY_NAME = 'Labelled-person matching'

export function peopleAssetFor(role: PeopleAssetRole): PeopleAsset {
  const asset = PEOPLE_ASSETS.find((candidate) => candidate.role === role)
  if (!asset) {
    throw new Error(`The people manifest is missing its ${role} entry.`)
  }
  return asset
}

/**
 * Rejects anything that is not one of the compiled-in URLs, so a mutated
 * manifest entry cannot point the fetch at a new origin. Checks the *initial*
 * URL only; see the note above on why integrity rests on the digest instead.
 */
export function isAllowlistedPeopleUrl(candidate: string): boolean {
  if (!PEOPLE_ASSETS.some((asset) => asset.url === candidate)) {
    return false
  }

  try {
    const url = new URL(candidate)
    return url.protocol === 'https:' && ALLOWED_PEOPLE_DOWNLOAD_HOSTS.includes(url.hostname)
  } catch {
    return false
  }
}
