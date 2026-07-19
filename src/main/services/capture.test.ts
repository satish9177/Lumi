import { describe, expect, it, vi } from 'vitest'
import { captureScreen, encodeCaptureImage, isCompanionCaptureLabel, MAX_CAPTURE_BYTES, orderCaptureSources } from './capture'

class FakeCaptureImage {
  constructor(
    private readonly width: number,
    private readonly height: number,
    private readonly bytesAt42: number,
    private readonly empty = false
  ) {}

  toJPEG(quality: number): Buffer {
    const multiplier = quality / 42
    return Buffer.alloc(Math.ceil(this.bytesAt42 * multiplier * (this.width / 1_600)))
  }

  getSize(): { width: number; height: number } {
    return { width: this.width, height: this.height }
  }

  resize({ width }: { width: number }): FakeCaptureImage {
    return new FakeCaptureImage(width, Math.round(this.height * (width / this.width)), this.bytesAt42, this.empty)
  }

  isEmpty(): boolean {
    return this.empty
  }
}

describe('encodeCaptureImage', () => {
  it('applies an optional maximum width before the quality ladder and preserves aspect ratio', () => {
    const encoded = encodeCaptureImage(new FakeCaptureImage(1_600, 900, 80_000), { maxWidth: 1_024 })

    expect(encoded.width).toBeLessThanOrEqual(1_024)
    expect(encoded.height / encoded.width).toBeCloseTo(900 / 1_600, 2)
  })

  it('keeps the original dimensions when no maximum width is supplied and the image fits', () => {
    const encoded = encodeCaptureImage(new FakeCaptureImage(1_600, 900, 80_000))

    expect(encoded.width).toBe(1_600)
    expect(encoded.height).toBe(900)
  })

  it('keeps a 4K-sized capture within the configured data-channel-safe byte cap', () => {
    const encoded = encodeCaptureImage(new FakeCaptureImage(1_600, 900, 300_000))
    const encodedBytes = Buffer.from(encoded.dataUrl.split(',')[1] ?? '', 'base64').byteLength

    expect(encodedBytes).toBeLessThanOrEqual(MAX_CAPTURE_BYTES)
    expect(encoded.width).toBeGreaterThanOrEqual(560)
    expect(encoded.height / encoded.width).toBeCloseTo(900 / 1_600, 2)
  })

  it('retains the final safety error when minimum-size JPEG output is still too large', () => {
    expect(() => encodeCaptureImage(new FakeCaptureImage(1_600, 900, 1_000_000))).toThrow('too large to send safely')
  })
})

describe('capture source selection', () => {
  it('puts application windows before displays and excludes the companion label', () => {
    const ordered = orderCaptureSources([
      { id: 'screen:1:0', label: 'Display 1', kind: 'screen', thumbnailDataUrl: 'data:image/png;base64,AA==' },
      { id: 'window:2:0', label: 'Resume.pdf', kind: 'window', thumbnailDataUrl: 'data:image/png;base64,AA==' },
      { id: 'window:3:0', label: 'Mail', kind: 'window', thumbnailDataUrl: 'data:image/png;base64,AA==' }
    ])

    expect(ordered.map((source) => source.id)).toEqual(['window:3:0', 'window:2:0', 'screen:1:0'])
    expect(isCompanionCaptureLabel('LifeLens')).toBe(true)
    expect(isCompanionCaptureLabel('Lumi')).toBe(true)
    expect(isCompanionCaptureLabel('Mail')).toBe(false)
  })

  it('quietly retries an initial empty native frame and returns the recovered capture', async () => {
    const getSources = vi.fn()
      .mockResolvedValueOnce([{ id: 'screen:1:0', display_id: '1', name: 'Primary screen', thumbnail: new FakeCaptureImage(1_600, 900, 80_000, true) }])
      .mockResolvedValueOnce([{ id: 'screen:1:0', display_id: '1', name: 'Primary screen', thumbnail: new FakeCaptureImage(1_600, 900, 80_000) }])

    const capture = await captureScreen(undefined, {
      getPrimaryDisplay: () => ({ id: 1, size: { width: 1_600, height: 900 }, scaleFactor: 1 }),
      getSources
    })

    expect(getSources).toHaveBeenCalledTimes(2)
    expect(capture.sourceId).toBe('screen:1:0')
    expect(capture.width).toBe(1_600)
  })

  it('reports a capture error only after the recovery retry also has no usable frame', async () => {
    const getSources = vi.fn().mockResolvedValue([{ id: 'screen:1:0', display_id: '1', name: 'Primary screen', thumbnail: new FakeCaptureImage(1_600, 900, 80_000, true) }])

    await expect(captureScreen(undefined, {
      getPrimaryDisplay: () => ({ id: 1, size: { width: 1_600, height: 900 }, scaleFactor: 1 }),
      getSources
    })).rejects.toThrow('could not capture the primary display')
    expect(getSources).toHaveBeenCalledTimes(2)
  })
})
