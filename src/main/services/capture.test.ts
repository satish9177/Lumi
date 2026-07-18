import { describe, expect, it } from 'vitest'
import { encodeCaptureImage, isCompanionCaptureLabel, MAX_CAPTURE_BYTES, orderCaptureSources } from './capture'

class FakeCaptureImage {
  constructor(private readonly width: number, private readonly height: number, private readonly bytesAt42: number) {}

  toJPEG(quality: number): Buffer {
    const multiplier = quality / 42
    return Buffer.alloc(Math.ceil(this.bytesAt42 * multiplier * (this.width / 1_600)))
  }

  getSize(): { width: number; height: number } {
    return { width: this.width, height: this.height }
  }

  resize({ width }: { width: number }): FakeCaptureImage {
    return new FakeCaptureImage(width, Math.round(this.height * (width / this.width)), this.bytesAt42)
  }
}

describe('encodeCaptureImage', () => {
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
})
