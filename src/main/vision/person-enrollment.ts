/**
 * The enrolment flow for user-labelled people.
 *
 * Every profile in Lumi exists because a person typed a name, picked specific
 * photos, pointed at a specific face in each, and then confirmed. This module
 * is what enforces that. There is no path through it that creates a profile as
 * a side effect of anything else — not dropping a file, not analysing an image,
 * not a Telegram contact, not a search.
 *
 * Design decisions worth stating plainly:
 *
 *  - **The largest face is never assumed to be the intended one.** When a
 *    reference contains more than one face, enrolment stops and asks. A parent
 *    standing behind a child is the ordinary case, and guessing would enrol the
 *    wrong person under a name the user trusts.
 *  - **Candidate previews and ids are memory-only and expire.** They exist for
 *    one selection, in one draft, and are never written anywhere.
 *  - **Source files are revalidated twice**: once when a reference is added, and
 *    again at final confirmation. A file that changed in between is refused,
 *    because the bytes the user reviewed are the only bytes they consented to.
 *  - **Nothing about the source survives the draft.** Once a profile is created,
 *    the paths, the pixels, the crops and the landmarks are gone; only the
 *    embeddings and their quality metadata persist.
 */

import { randomUUID } from 'node:crypto'
import { alignFaceToTensor, type SourceImage } from './face-align'
import { CONFIDENT_FACE_SCORE } from './face-detect'
import { findInconsistentReference } from './face-matching'
import type { Point } from './face-landmarks'
import { FACE_EMBED_TENSOR_FLOATS, MAX_EMBED_FACES } from './protocol'
import {
  MAX_REFERENCES,
  MIN_REFERENCES,
  PersonProfileError,
  cleanLabel,
  normalizeEmbedding,
  type PersonProfileStore,
  type PersonProfileSummary,
  type ReferenceQuality,
  type StoredReference
} from './person-profiles'

/** A face smaller than this yields an embedding too noisy to enrol on. */
export const MIN_REFERENCE_FACE_PX = 80

/** References must come from confident detections; matching may be laxer. */
export const MIN_REFERENCE_DETECTION_SCORE = CONFIDENT_FACE_SCORE

/** How long a draft and its previews live without activity. */
export const DRAFT_TTL_MS = 15 * 60_000

/** A ceiling on concurrent drafts, so abandoned ones cannot accumulate. */
export const MAX_DRAFTS = 4

/** Bounded preview edge, in pixels. Large enough to recognise, small enough to cap. */
export const PREVIEW_EDGE_PX = 96

export const ENROLMENT_REJECTIONS = [
  'no_face',
  'face_too_small',
  'detection_uncertain',
  'alignment_failed',
  'embedding_failed',
  'inconsistent_reference',
  'file_unavailable',
  'file_changed',
  'already_added',
  'unknown_draft',
  'unknown_candidate',
  'selection_required',
  'too_many_references',
  'too_few_references',
  'too_many_drafts'
] as const
export type EnrolmentRejection = (typeof ENROLMENT_REJECTIONS)[number]

/**
 * App-authored, path-free explanations. These are the only words the renderer
 * shows for a rejection, so none of them quotes a filename or a measurement.
 */
export const ENROLMENT_REJECTION_MESSAGES: Record<EnrolmentRejection, string> = {
  no_face: 'Lumi couldn’t find a face in that photo. Try one where the face is clearer.',
  face_too_small: 'That face is too small for Lumi to learn from. Try a closer photo.',
  detection_uncertain: 'Lumi isn’t sure that’s a face. Try a clearer photo.',
  alignment_failed: 'Lumi couldn’t line that face up. Try a photo facing the camera more.',
  embedding_failed: 'Lumi couldn’t read that face. Try another photo.',
  inconsistent_reference: 'That photo looks like a different person from the others.',
  file_unavailable: 'Lumi can’t open that photo anymore.',
  file_changed: 'That photo changed after you chose it, so Lumi left it out.',
  already_added: 'That photo is already one of the references.',
  unknown_draft: 'That setup is no longer open. Start again.',
  unknown_candidate: 'That choice is no longer available. Pick a face again.',
  selection_required: 'Choose which face belongs to this person.',
  too_many_references: `Lumi keeps up to ${MAX_REFERENCES} reference photos per person.`,
  too_few_references: `Choose at least ${MIN_REFERENCES} photos of this person.`,
  too_many_drafts: 'Finish or cancel the person you’re already adding first.'
}

export class EnrolmentError extends Error {
  constructor(readonly code: EnrolmentRejection) {
    super(ENROLMENT_REJECTION_MESSAGES[code])
    this.name = 'EnrolmentError'
  }
}

/** The minimal image surface enrolment needs. Matches Electron's nativeImage. */
export interface EnrolmentImage {
  getSize: () => { width: number; height: number }
  crop: (rect: { x: number; y: number; width: number; height: number }) => EnrolmentImage
  resize: (options: { width: number; height: number }) => EnrolmentImage
  toBitmap: () => Uint8Array
  toDataURL: () => string
}

export interface FileFingerprint {
  sizeBytes: number
  mtimeMs: number
}

export interface DetectedGeometry {
  count: number
  /** `count * 5`: x, y, width, height, score — in 640x640 detector space. */
  boxes: Float32Array
  /** `count * 10`: five landmark x/y pairs — in 640x640 detector space. */
  landmarks: Float32Array
}

export interface PersonEnrollmentDependencies {
  profiles: PersonProfileStore
  /**
   * Resolves a renderer-supplied identifier to a path main trusts, or undefined.
   * This is the *only* way a file enters enrolment: an arbitrary path cannot be
   * passed in, because this function takes an opaque id and not a path.
   */
  resolveTrustedPath: (trustedId: string) => Promise<string | undefined>
  fingerprint: (path: string) => Promise<FileFingerprint | undefined>
  decodeImage: (path: string) => Promise<EnrolmentImage | undefined>
  /** Letterboxes to 640x640 and returns the scale applied. */
  prepareDetectionBitmap: (image: EnrolmentImage) => { bitmap: ArrayBuffer; scale: number }
  detectFaces: (bitmap: ArrayBuffer) => Promise<DetectedGeometry>
  embedFaces: (tensors: Float32Array, count: number) => Promise<Float32Array>
  now?: () => number
}

/** What the renderer may see about one detected face. */
export interface CandidateView {
  candidateId: string
  /** A bounded crop, for pointing at. Never stored. */
  previewDataUrl: string
  /** App-authored, e.g. "Too small to learn from". Never a raw number. */
  note?: string
  /** False when a quality gate already ruled this face out. */
  selectable: boolean
}

export interface ReferenceView {
  referenceId: string
  addedAt: string
}

export interface DraftView {
  draftId: string
  label: string
  references: ReferenceView[]
  /** Present when the last added photo needs a face chosen. */
  candidates?: CandidateView[]
  /** True once enough references are accepted for confirmation to be offered. */
  readyToConfirm: boolean
}

interface Candidate {
  candidateId: string
  detectionScore: number
  faceSizePx: number
  /** Source-image coordinates. Memory-only; never persisted or sent anywhere. */
  landmarks: Point[]
  previewDataUrl: string
  selectable: boolean
  note?: string
}

interface DraftReference {
  referenceId: string
  embedding: number[]
  quality: ReferenceQuality
  addedAt: string
  /** Retained only until confirmation, purely to revalidate the source. */
  trustedId: string
  fingerprint: FileFingerprint
}

interface Draft {
  draftId: string
  label: string
  references: DraftReference[]
  pending?: { trustedId: string; source: SourceImage; candidates: Candidate[] }
  expiresAt: number
}

export class PersonEnrollmentService {
  private drafts = new Map<string, Draft>()

  constructor(private readonly dependencies: PersonEnrollmentDependencies) {}

  private now(): number {
    return this.dependencies.now?.() ?? Date.now()
  }

  private sweep(): void {
    const now = this.now()
    for (const [draftId, draft] of this.drafts) {
      if (draft.expiresAt <= now) {
        this.drafts.delete(draftId)
      }
    }
  }

  private draftOrThrow(draftId: string): Draft {
    this.sweep()
    const draft = this.drafts.get(draftId)
    if (!draft) {
      throw new EnrolmentError('unknown_draft')
    }
    draft.expiresAt = this.now() + DRAFT_TTL_MS
    return draft
  }

  /**
   * Opens a draft. Creates nothing persistent — a draft that is never confirmed
   * leaves no trace beyond an entry in this map that expires on its own.
   */
  begin(rawLabel: string): DraftView {
    this.sweep()
    if (this.drafts.size >= MAX_DRAFTS) {
      throw new EnrolmentError('too_many_drafts')
    }
    // Validated here so a bad label fails before any photo is processed.
    const label = cleanLabel(rawLabel)
    const draft: Draft = {
      draftId: randomUUID(),
      label,
      references: [],
      expiresAt: this.now() + DRAFT_TTL_MS
    }
    this.drafts.set(draft.draftId, draft)
    return view(draft)
  }

  list(draftId: string): DraftView {
    return view(this.draftOrThrow(draftId))
  }

  /**
   * Adds one reference photo, identified by a trusted id the renderer already
   * legitimately holds — an approved-root search result or a live dropped file.
   *
   * Returns candidates for selection whenever the photo contains more than one
   * usable face. A single usable face is accepted directly; that is not the
   * same as assuming the largest, because a lone face is unambiguous.
   */
  async addReference(draftId: string, trustedId: string): Promise<DraftView> {
    const draft = this.draftOrThrow(draftId)
    if (draft.references.length >= MAX_REFERENCES) {
      throw new EnrolmentError('too_many_references')
    }
    if (draft.references.some((reference) => reference.trustedId === trustedId)) {
      throw new EnrolmentError('already_added')
    }

    const { source, image, fingerprint } = await this.loadTrusted(trustedId)
    const prepared = this.dependencies.prepareDetectionBitmap(image)
    const geometry = await this.dependencies.detectFaces(prepared.bitmap)

    const candidates = this.buildCandidates(geometry, prepared.scale, image)
    if (candidates.length === 0) {
      throw new EnrolmentError('no_face')
    }

    const usable = candidates.filter((candidate) => candidate.selectable)
    if (usable.length === 0) {
      // Every face failed a gate. Report the first reason rather than a generic
      // one, so the user knows whether to move closer or find a clearer photo.
      throw new EnrolmentError(candidates[0]!.note === PREVIEW_TOO_SMALL ? 'face_too_small' : 'detection_uncertain')
    }

    if (usable.length > 1) {
      // Stop and ask. This is the branch that keeps Lumi from enrolling the
      // wrong family member from a group photo.
      draft.pending = { trustedId, source, candidates }
      return view(draft)
    }

    await this.acceptCandidate(draft, trustedId, source, fingerprint, usable[0]!)
    return view(draft)
  }

  /** Resolves the pending selection to one explicitly chosen face. */
  async selectFace(draftId: string, candidateId: string): Promise<DraftView> {
    const draft = this.draftOrThrow(draftId)
    const pending = draft.pending
    if (!pending) {
      throw new EnrolmentError('unknown_candidate')
    }
    const candidate = pending.candidates.find((entry) => entry.candidateId === candidateId)
    if (!candidate || !candidate.selectable) {
      throw new EnrolmentError('unknown_candidate')
    }

    // Revalidated again: the user may have spent minutes choosing.
    const fingerprint = await this.fingerprintOf(pending.trustedId)
    await this.acceptCandidate(draft, pending.trustedId, pending.source, fingerprint, candidate)
    draft.pending = undefined
    return view(draft)
  }

  /** Abandons the draft and everything held in memory for it. */
  cancel(draftId: string): void {
    this.drafts.delete(draftId)
  }

  /**
   * Creates the profile. The explicit act that this whole module exists to
   * gate — nothing before this point has written anything.
   */
  async confirm(draftId: string): Promise<PersonProfileSummary> {
    const draft = this.draftOrThrow(draftId)
    if (draft.pending) {
      throw new EnrolmentError('selection_required')
    }
    if (draft.references.length < MIN_REFERENCES) {
      throw new EnrolmentError('too_few_references')
    }

    // Second revalidation. Between adding a reference and confirming, a source
    // photo may have been edited, replaced, or had its folder revoked; the
    // bytes the user reviewed are the only ones they agreed to enrol.
    for (const reference of draft.references) {
      const current = await this.fingerprintOf(reference.trustedId)
      if (
        current.sizeBytes !== reference.fingerprint.sizeBytes ||
        current.mtimeMs !== reference.fingerprint.mtimeMs
      ) {
        throw new EnrolmentError('file_changed')
      }
    }

    const inconsistent = findInconsistentReference(draft.references.map((reference) => reference.embedding))
    if (inconsistent !== undefined) {
      throw new EnrolmentError('inconsistent_reference')
    }

    const stored: StoredReference[] = draft.references.map((reference) => ({
      id: reference.referenceId,
      embedding: reference.embedding,
      quality: reference.quality,
      addedAt: reference.addedAt
    }))

    const summary = await this.dependencies.profiles.create(draft.label, stored)
    // The draft held the only copy of the source paths and pixels. Dropping it
    // is what makes "no reference paths are stored" true.
    this.drafts.delete(draftId)
    return summary
  }

  private async loadTrusted(
    trustedId: string
  ): Promise<{ source: SourceImage; image: EnrolmentImage; fingerprint: FileFingerprint }> {
    const path = await this.dependencies.resolveTrustedPath(trustedId)
    if (!path) {
      // Covers an unknown id, an expired dropped record, and a revoked root.
      throw new EnrolmentError('file_unavailable')
    }
    const fingerprint = await this.dependencies.fingerprint(path)
    if (!fingerprint) {
      throw new EnrolmentError('file_unavailable')
    }
    const image = await this.dependencies.decodeImage(path)
    if (!image) {
      throw new EnrolmentError('file_unavailable')
    }
    const size = image.getSize()
    if (size.width <= 0 || size.height <= 0) {
      throw new EnrolmentError('file_unavailable')
    }
    return {
      image,
      fingerprint,
      source: { data: image.toBitmap(), width: size.width, height: size.height }
    }
  }

  private async fingerprintOf(trustedId: string): Promise<FileFingerprint> {
    const path = await this.dependencies.resolveTrustedPath(trustedId)
    if (!path) {
      throw new EnrolmentError('file_unavailable')
    }
    const fingerprint = await this.dependencies.fingerprint(path)
    if (!fingerprint) {
      throw new EnrolmentError('file_unavailable')
    }
    return fingerprint
  }

  /**
   * Maps detector-space geometry back to source coordinates and applies the
   * quality gates. Faces that fail a gate are still returned, marked
   * unselectable with a reason, so the user can see that Lumi looked at them
   * rather than silently ignoring half a photo.
   */
  private buildCandidates(
    geometry: DetectedGeometry,
    scale: number,
    image: EnrolmentImage
  ): Candidate[] {
    const candidates: Candidate[] = []
    const size = image.getSize()
    const count = Math.min(geometry.count, MAX_EMBED_FACES)

    for (let index = 0; index < count; index += 1) {
      const boxOffset = index * 5
      const x = geometry.boxes[boxOffset]! / scale
      const y = geometry.boxes[boxOffset + 1]! / scale
      const width = geometry.boxes[boxOffset + 2]! / scale
      const height = geometry.boxes[boxOffset + 3]! / scale
      const score = geometry.boxes[boxOffset + 4]!

      const landmarks: Point[] = []
      for (let point = 0; point < 5; point += 1) {
        landmarks.push({
          x: geometry.landmarks[index * 10 + point * 2]! / scale,
          y: geometry.landmarks[index * 10 + point * 2 + 1]! / scale
        })
      }

      const faceSizePx = Math.max(width, height)
      let selectable = true
      let note: string | undefined
      if (faceSizePx < MIN_REFERENCE_FACE_PX) {
        selectable = false
        note = PREVIEW_TOO_SMALL
      } else if (score < MIN_REFERENCE_DETECTION_SCORE) {
        selectable = false
        note = PREVIEW_UNSURE
      }

      candidates.push({
        candidateId: randomUUID(),
        detectionScore: score,
        faceSizePx,
        landmarks,
        previewDataUrl: cropPreview(image, size, { x, y, width, height }),
        selectable,
        note
      })
    }

    return candidates
  }

  private async acceptCandidate(
    draft: Draft,
    trustedId: string,
    source: SourceImage,
    fingerprint: FileFingerprint,
    candidate: Candidate
  ): Promise<void> {
    const aligned = alignFaceToTensor(source, candidate.landmarks)
    if (!aligned || aligned.length !== FACE_EMBED_TENSOR_FLOATS) {
      throw new EnrolmentError('alignment_failed')
    }

    let embeddings: Float32Array
    try {
      embeddings = await this.dependencies.embedFaces(aligned, 1)
    } catch {
      throw new EnrolmentError('embedding_failed')
    }

    let embedding: number[]
    try {
      embedding = normalizeEmbedding(embeddings)
    } catch (error) {
      throw error instanceof PersonProfileError ? new EnrolmentError('embedding_failed') : error
    }

    draft.references.push({
      referenceId: randomUUID(),
      embedding,
      quality: { detectionScore: candidate.detectionScore, faceSizePx: candidate.faceSizePx },
      addedAt: new Date(this.now()).toISOString(),
      trustedId,
      fingerprint
    })
  }
}

const PREVIEW_TOO_SMALL = 'Too small to learn from'
const PREVIEW_UNSURE = 'Lumi isn’t sure this is a face'

/**
 * A bounded square crop around the face, for the selection UI.
 *
 * Padded outward so the preview shows a recognisable head rather than a tight
 * rectangle of features, and clamped to the image so a face at the edge does
 * not produce an out-of-bounds crop.
 */
function cropPreview(
  image: EnrolmentImage,
  size: { width: number; height: number },
  box: { x: number; y: number; width: number; height: number }
): string {
  const padding = Math.max(box.width, box.height) * 0.25
  const x = Math.max(0, Math.round(box.x - padding))
  const y = Math.max(0, Math.round(box.y - padding))
  const width = Math.max(1, Math.min(Math.round(box.width + padding * 2), size.width - x))
  const height = Math.max(1, Math.min(Math.round(box.height + padding * 2), size.height - y))

  try {
    return image
      .crop({ x, y, width, height })
      .resize({ width: PREVIEW_EDGE_PX, height: PREVIEW_EDGE_PX })
      .toDataURL()
  } catch {
    return ''
  }
}

/** The renderer's view. Note what it cannot contain: paths, vectors, geometry. */
function view(draft: Draft): DraftView {
  return {
    draftId: draft.draftId,
    label: draft.label,
    references: draft.references.map((reference) => ({
      referenceId: reference.referenceId,
      addedAt: reference.addedAt
    })),
    candidates: draft.pending?.candidates.map((candidate) => ({
      candidateId: candidate.candidateId,
      previewDataUrl: candidate.previewDataUrl,
      note: candidate.note,
      selectable: candidate.selectable
    })),
    readyToConfirm: draft.pending === undefined && draft.references.length >= MIN_REFERENCES
  }
}
