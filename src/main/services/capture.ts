import { desktopCapturer, screen } from 'electron'
import { randomUUID } from 'node:crypto'
import type { CaptureResult } from '../../shared/contracts'

export async function capturePrimaryScreen(): Promise<CaptureResult> {
  const display = screen.getPrimaryDisplay()
  const width = Math.min(Math.round(display.size.width * display.scaleFactor), 1_600)
  const height = Math.min(Math.round(display.size.height * display.scaleFactor), 1_000)
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width, height }
  })
  const source = sources.find((candidate) => candidate.display_id === String(display.id)) ?? sources[0]

  if (!source || source.thumbnail.isEmpty()) {
    throw new Error('LifeLens could not capture the primary display.')
  }

  const imageSize = source.thumbnail.getSize()
  return {
    id: randomUUID(),
    label: source.name || 'Primary screen',
    dataUrl: source.thumbnail.toDataURL(),
    mimeType: 'image/png',
    width: imageSize.width,
    height: imageSize.height,
    capturedAt: new Date().toISOString()
  }
}
