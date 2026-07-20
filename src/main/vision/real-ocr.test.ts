/**
 * Real Tesseract recognition against the actual installed extras pack.
 *
 * Everything else about OCR in this directory is unit-tested with a fake
 * worker. This file is the one place that proves the real engine, the real
 * verified training data, and this application's own PNG encoder actually
 * agree — and, just as importantly, that the engine reads a local file rather
 * than reaching for a CDN.
 *
 * Shape checks cannot catch a wrongly encoded image: a malformed PNG simply
 * yields empty text, which looks identical to a photo with nothing written on
 * it. Only recognizing known content can distinguish those.
 *
 * Skipped when the extras pack is not installed, so the suite stays runnable
 * offline and on a clean checkout.
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { EXTRAS_ASSETS, EXTRAS_PACK_ID } from './extras-manifest'
import { LocalOcrEngine } from './ocr-engine'
import { encodeGreyscalePng } from './ocr-image'

const PACK_DIR = join(
  process.env.APPDATA ?? join(process.env.USERPROFILE ?? '', 'AppData', 'Roaming'),
  'lifelens',
  'vision-models',
  EXTRAS_PACK_ID
)

const packInstalled = EXTRAS_ASSETS.every((asset) => existsSync(join(PACK_DIR, asset.fileName)))

/**
 * A 5x7 bitmap font, so the fixture image depends on no external asset and no
 * font installed on the host. Deliberately crude: if OCR can read this, it can
 * comfortably read real document text.
 */
const FONT: Record<string, readonly string[]> = {
  I: ['11111', '00100', '00100', '00100', '00100', '00100', '11111'],
  N: ['10001', '11001', '11001', '10101', '10011', '10011', '10001'],
  V: ['10001', '10001', '10001', '10001', '10001', '01010', '00100'],
  O: ['01110', '10001', '10001', '10001', '10001', '10001', '01110'],
  C: ['01110', '10001', '10000', '10000', '10000', '10001', '01110'],
  E: ['11111', '10000', '10000', '11110', '10000', '10000', '11111'],
  '1': ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
  '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'],
  '3': ['11111', '00010', '00100', '00010', '00001', '10001', '01110'],
  '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'],
  ' ': ['00000', '00000', '00000', '00000', '00000', '00000', '00000']
}

function renderText(text: string, scale = 14, pad = 60): { pixels: Uint8Array; width: number; height: number } {
  const cellWidth = 6 * scale
  const width = pad * 2 + text.length * cellWidth
  const height = pad * 2 + 7 * scale
  const pixels = new Uint8Array(width * height).fill(0xff)

  text.split('').forEach((character, index) => {
    const glyph = FONT[character]
    if (!glyph) return
    for (let row = 0; row < 7; row += 1) {
      for (let column = 0; column < 5; column += 1) {
        if (glyph[row]![column] !== '1') continue
        for (let dy = 0; dy < scale; dy += 1) {
          for (let dx = 0; dx < scale; dx += 1) {
            const x = pad + index * cellWidth + column * scale + dx
            const y = pad + row * scale + dy
            pixels[y * width + x] = 0x00
          }
        }
      }
    }
  })

  return { pixels, width, height }
}

function pngOf(text: string): Buffer {
  const { pixels, width, height } = renderText(text)
  return encodeGreyscalePng(pixels, width, height)
}

describe.skipIf(!packInstalled)('real local OCR', () => {
  it('reads digits from an image this application encoded itself', async () => {
    const engine = new LocalOcrEngine({ languageDirectory: PACK_DIR })
    try {
      const result = await engine.recognize(pngOf('1234'))
      // Digits are the load-bearing case: reference and ID queries match them
      // exactly, with no fuzzy budget to fall back on.
      expect(result.tokens).toContain('1234')
    } finally {
      await engine.dispose()
    }
  }, 120_000)

  it('produces normalized, tokenized text rather than raw engine output', async () => {
    const engine = new LocalOcrEngine({ languageDirectory: PACK_DIR })
    try {
      const result = await engine.recognize(pngOf('INVOICE 1234'))
      expect(result.text).toBe(result.text.toLocaleLowerCase('en-US'))
      expect(result.text).not.toMatch(/\n|\r|\t/)
      expect(result.tokens.every((token) => token.length >= 2)).toBe(true)
    } finally {
      await engine.dispose()
    }
  }, 120_000)

  it('reads a blank image as no text rather than failing', async () => {
    const engine = new LocalOcrEngine({ languageDirectory: PACK_DIR })
    try {
      const blank = encodeGreyscalePng(new Uint8Array(400 * 200).fill(0xff), 400, 200)
      const result = await engine.recognize(blank)
      expect(result.tokens.length).toBe(0)
    } finally {
      await engine.dispose()
    }
  }, 120_000)

  it('runs with no network access whatsoever', async () => {
    // If the engine were resolving training data over the network, removing
    // fetch would surface as a failure rather than a clean recognition.
    const originalFetch = globalThis.fetch
    let attemptedFetch = false
    globalThis.fetch = (() => {
      attemptedFetch = true
      throw new Error('the local OCR path must not perform network requests')
    }) as typeof fetch

    const engine = new LocalOcrEngine({ languageDirectory: PACK_DIR })
    try {
      const result = await engine.recognize(pngOf('1234'))
      expect(attemptedFetch).toBe(false)
      expect(result.tokens).toContain('1234')
    } finally {
      globalThis.fetch = originalFetch
      await engine.dispose()
    }
  }, 120_000)

  it('releases the worker heap when disposed', async () => {
    const engine = new LocalOcrEngine({ languageDirectory: PACK_DIR })
    await engine.recognize(pngOf('1234'))
    expect(engine.isRunning()).toBe(true)
    await engine.dispose()
    expect(engine.isRunning()).toBe(false)
  }, 120_000)
})
