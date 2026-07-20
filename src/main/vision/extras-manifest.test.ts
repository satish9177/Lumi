import { describe, expect, it } from 'vitest'
import {
  ALLOWED_EXTRAS_DOWNLOAD_HOSTS,
  EXTRAS_ASSETS,
  EXTRAS_PACK_ID,
  EXTRAS_PACK_LICENSE_NOTICE,
  EXTRAS_PACK_TOTAL_BYTES,
  EXTRAS_PACK_VERSION,
  extrasAssetFor,
  FACE_MODEL_VERSION,
  isAllowlistedExtrasUrl,
  OCR_MODEL_VERSION
} from './extras-manifest'
import { MODEL_ASSETS, MODEL_PACK_ID, MODEL_PACK_VERSION } from './manifest'

describe('the extras manifest is frozen application-authored data', () => {
  it('cannot be extended or rewritten at runtime', () => {
    expect(Object.isFrozen(EXTRAS_ASSETS)).toBe(true)
    for (const asset of EXTRAS_ASSETS) {
      expect(Object.isFrozen(asset)).toBe(true)
    }
    expect(() => {
      ;(EXTRAS_ASSETS as unknown as unknown[]).push({ role: 'faceModel' })
    }).toThrow()
  })

  it('pins every asset to an immutable revision with a digest and an exact size', () => {
    expect(EXTRAS_ASSETS.length).toBeGreaterThan(0)
    for (const asset of EXTRAS_ASSETS) {
      expect(asset.sha256).toMatch(/^[0-9a-f]{64}$/)
      expect(asset.sizeBytes).toBeGreaterThan(0)
      expect(asset.url.startsWith('https://')).toBe(true)
      // A 40-character hex commit, never a branch or tag that could be moved.
      expect(asset.url).toMatch(/\/[0-9a-f]{40}\//)
      expect(asset.url).not.toMatch(/\/(main|master|HEAD|latest)\//)
    }
  })

  it('declares a total that matches the sum of its assets', () => {
    expect(EXTRAS_PACK_TOTAL_BYTES).toBe(
      EXTRAS_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0)
    )
  })

  it('names both licences and does not imply identity recognition', () => {
    expect(EXTRAS_PACK_LICENSE_NOTICE).toMatch(/Apache 2\.0/)
    expect(EXTRAS_PACK_LICENSE_NOTICE).toMatch(/MIT/)
    expect(EXTRAS_PACK_LICENSE_NOTICE).toMatch(/only on this device/)
    expect(EXTRAS_PACK_LICENSE_NOTICE).toMatch(/cannot recognise who anyone is/)
  })
})

describe('the extras pack is independent of the CLIP pack', () => {
  it('has its own identity, so installing it cannot invalidate stored vectors', () => {
    expect(EXTRAS_PACK_ID).not.toBe(MODEL_PACK_ID)
    // The Phase-1 pack identity is what the vector index is keyed on. If this
    // ever changes to accommodate Phase 2, every user re-embeds their library.
    expect(MODEL_PACK_ID).toBe('clip-vit-base-patch32-q8')
    expect(MODEL_PACK_VERSION).toBe(1)
  })

  it('shares no filename with the CLIP pack', () => {
    const clipNames = new Set(MODEL_ASSETS.map((asset) => asset.fileName))
    for (const asset of EXTRAS_ASSETS) {
      expect(clipNames.has(asset.fileName)).toBe(false)
    }
  })

  it('versions the two Phase-2 signals separately from the pack and from each other', () => {
    expect(Number.isInteger(OCR_MODEL_VERSION)).toBe(true)
    expect(Number.isInteger(FACE_MODEL_VERSION)).toBe(true)
    expect(Number.isInteger(EXTRAS_PACK_VERSION)).toBe(true)
  })
})

describe('the download allowlist', () => {
  it('accepts exactly the compiled-in URLs', () => {
    for (const asset of EXTRAS_ASSETS) {
      expect(isAllowlistedExtrasUrl(asset.url)).toBe(true)
    }
  })

  it.each([
    ['a plausible look-alike on an allowed host', 'https://raw.githubusercontent.com/evil/repo/0000000000000000000000000000000000000000/eng.traineddata'],
    ['the same path on another host', 'https://example.com/eng.traineddata'],
    ['plain http', 'http://raw.githubusercontent.com/x/y/z/eng.traineddata'],
    ['an empty string', ''],
    ['a local file URL', 'file:///C:/Windows/System32/drivers/etc/hosts'],
    ['a data URL', 'data:application/octet-stream;base64,AAAA'],
    ['nonsense', 'not a url at all']
  ])('rejects %s', (_label, candidate) => {
    expect(isAllowlistedExtrasUrl(candidate)).toBe(false)
  })

  it('rejects a URL that differs from a real asset by a single character', () => {
    const real = EXTRAS_ASSETS[0]!.url
    expect(isAllowlistedExtrasUrl(`${real}x`)).toBe(false)
    expect(isAllowlistedExtrasUrl(real.replace('https', 'http'))).toBe(false)
  })

  it('only ever contacts hosts named in the allowlist', () => {
    for (const asset of EXTRAS_ASSETS) {
      expect(ALLOWED_EXTRAS_DOWNLOAD_HOSTS).toContain(new URL(asset.url).hostname)
    }
  })
})

describe('role lookup', () => {
  it('resolves both application-defined roles', () => {
    expect(extrasAssetFor('ocrTrainedData').fileName).toBe('eng.traineddata')
    expect(extrasAssetFor('faceModel').fileName).toMatch(/\.onnx$/)
  })

  it('throws for a role this application never defined', () => {
    expect(() => extrasAssetFor('faceIdentity' as never)).toThrow(/missing/)
  })
})
