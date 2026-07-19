import { desktopCapturer, screen, type NativeImage } from 'electron'
import { randomUUID } from 'node:crypto'
import type { CaptureResult, CaptureSource, CaptureSourceKind } from '../../shared/contracts'

const SOURCE_PREVIEW_SIZE = { width: 320, height: 180 }
const MAX_CAPTURE_WIDTH = 1_600
const MAX_CAPTURE_HEIGHT = 1_000
// This bounds data-channel transport size; JPEG bytes do not determine image tokens.
export const MAX_CAPTURE_BYTES = 150_000
const MIN_CAPTURE_WIDTH = 560

/** The minimal image surface shared by native images and test doubles. */
export interface CaptureImage {
  toJPEG: (quality: number) => Buffer
  getSize: () => { width: number; height: number }
  resize: (size: { width: number }) => CaptureImage
  isEmpty?: () => boolean
}

interface CaptureDisplay {
  id: number
  size: { width: number; height: number }
  scaleFactor: number
}

interface CaptureSourceImage {
  id: string
  display_id: string
  name: string
  thumbnail: CaptureImage
}

/** Narrow injection seam for the recoverable native-frame path. */
export interface CaptureRuntime {
  getPrimaryDisplay: () => CaptureDisplay
  getSources: (options: { types: Array<'screen' | 'window'>; thumbnailSize: { width: number; height: number }; fetchWindowIcons: boolean }) => Promise<CaptureSourceImage[]>
}

const ELECTRON_CAPTURE_RUNTIME: CaptureRuntime = {
  getPrimaryDisplay: () => screen.getPrimaryDisplay(),
  getSources: (options) => desktopCapturer.getSources(options)
}

export async function listCaptureSources(): Promise<CaptureSource[]> {
  const sources = await desktopCapturer.getSources({
    types: ['screen', 'window'],
    thumbnailSize: SOURCE_PREVIEW_SIZE,
    fetchWindowIcons: false
  })

  return orderCaptureSources(sources
    .filter((source) => !source.thumbnail.isEmpty())
    .map((source) => ({
      id: source.id,
      label: source.name || (sourceKindFor(source.id) === 'screen' ? 'Screen' : 'Window'),
      kind: sourceKindFor(source.id),
      thumbnailDataUrl: source.thumbnail.toDataURL()
    }))
    .filter((source) => !isCompanionCaptureLabel(source.label))
  ).slice(0, 16)
}

export function orderCaptureSources(sources: readonly CaptureSource[]): CaptureSource[] {
  return [...sources].sort((left, right) => {
    const kindOrder = (left.kind === 'window' ? 0 : 1) - (right.kind === 'window' ? 0 : 1)
    return kindOrder || left.label.localeCompare(right.label)
  })
}

export function isCompanionCaptureLabel(label: string): boolean {
  return /\b(?:lifelens|lumi)\b/i.test(label)
}

export async function captureScreen(sourceId?: string, runtime: CaptureRuntime = ELECTRON_CAPTURE_RUNTIME): Promise<CaptureResult> {
  const display = runtime.getPrimaryDisplay()
  const width = Math.min(Math.round(display.size.width * display.scaleFactor), MAX_CAPTURE_WIDTH)
  const height = Math.min(Math.round(display.size.height * display.scaleFactor), MAX_CAPTURE_HEIGHT)
  const sourceOptions: Parameters<CaptureRuntime['getSources']>[0] = {
    types: ['screen', 'window'],
    thumbnailSize: { width, height },
    fetchWindowIcons: false
  }
  let sources = await runtime.getSources(sourceOptions)
  let source = selectCaptureSource(sources, sourceId, display.id)

  // Windows Graphics Capture can transiently return an empty thumbnail while
  // its native backend recovers. Do one quiet retry; an app-level error is only
  // surfaced when neither attempt produces a usable selected source.
  if (!isUsableCaptureSource(source)) {
    sources = await runtime.getSources(sourceOptions)
    source = selectCaptureSource(sources, sourceId, display.id)
  }

  if (!isUsableCaptureSource(source)) {
    throw new Error(sourceId ? 'The selected capture source is no longer available. Choose it again.' : 'LifeLens could not capture the primary display.')
  }

  const image = encodeCaptureImage(source.thumbnail)
  return {
    id: randomUUID(),
    sourceId: source.id,
    sourceKind: sourceKindFor(source.id),
    label: source.name || (sourceKindFor(source.id) === 'screen' ? 'Primary screen' : 'Selected window'),
    dataUrl: image.dataUrl,
    mimeType: 'image/jpeg',
    width: image.width,
    height: image.height,
    capturedAt: new Date().toISOString()
  }
}

function selectCaptureSource(sources: readonly CaptureSourceImage[], sourceId: string | undefined, displayId: number): CaptureSourceImage | undefined {
  return sourceId
    ? sources.find((candidate) => candidate.id === sourceId)
    : sources.find((candidate) => candidate.display_id === String(displayId)) ?? sources.find((candidate) => sourceKindFor(candidate.id) === 'screen')
}

function isUsableCaptureSource(source: CaptureSourceImage | undefined): source is CaptureSourceImage {
  return source !== undefined && source.thumbnail.isEmpty?.() !== true
}

function sourceKindFor(sourceId: string): CaptureSourceKind {
  return sourceId.startsWith('screen:') ? 'screen' : 'window'
}

export function encodeCaptureImage(
  sourceImage: NativeImage | CaptureImage,
  options?: { maxWidth?: number }
): { dataUrl: string; width: number; height: number } {
  let image: CaptureImage = sourceImage
  const maxWidth = options?.maxWidth
  if (maxWidth !== undefined && Number.isFinite(maxWidth) && maxWidth > 0 && image.getSize().width > maxWidth) {
    // Supplying only width keeps the source image aspect ratio intact.
    image = image.resize({ width: Math.round(maxWidth) })
  }

  for (;;) {
    for (const quality of [72, 62, 52, 42]) {
      const jpeg = image.toJPEG(quality)
      if (jpeg.byteLength <= MAX_CAPTURE_BYTES) {
        const size = image.getSize()
        return {
          dataUrl: `data:image/jpeg;base64,${jpeg.toString('base64')}`,
          width: size.width,
          height: size.height
        }
      }
    }

    const size = image.getSize()
    if (size.width <= MIN_CAPTURE_WIDTH) {
      break
    }

    // Supplying only width keeps the source image aspect ratio intact.
    image = image.resize({ width: Math.max(MIN_CAPTURE_WIDTH, Math.round(size.width * 0.72)) })
  }

  throw new Error('The selected screen is too large to send safely. Choose a smaller window and try again.')
}
