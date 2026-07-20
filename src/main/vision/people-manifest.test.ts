import { describe, expect, it } from 'vitest'
import { EXTRAS_ASSETS, EXTRAS_PACK_ID, isAllowlistedExtrasUrl } from './extras-manifest'
import { MODEL_ASSETS, MODEL_PACK_ID, isAllowlistedAssetUrl } from './manifest'
import {
  ALLOWED_PEOPLE_DOWNLOAD_HOSTS,
  FACE_EMBED_DIMENSIONS,
  FACE_EMBED_INPUT_SIZE,
  FACE_EMBED_MODEL_VERSION,
  PEOPLE_ASSETS,
  PEOPLE_INDEX_VERSION,
  PEOPLE_PACK_ID,
  PEOPLE_PACK_LICENSE_NOTICE,
  PEOPLE_PACK_TOTAL_BYTES,
  PEOPLE_PACK_VERSION,
  isAllowlistedPeopleUrl,
  peopleAssetFor
} from './people-manifest'

describe('the people pack is its own identity', () => {
  it('does not share an id with the CLIP or extras packs', () => {
    expect(PEOPLE_PACK_ID).not.toBe(MODEL_PACK_ID)
    expect(PEOPLE_PACK_ID).not.toBe(EXTRAS_PACK_ID)
  })

  it('does not reuse a filename from another pack', () => {
    // Two packs writing the same filename would collide only if they also shared
    // a directory, but a shared name is the first step toward that mistake.
    const others = [...MODEL_ASSETS, ...EXTRAS_ASSETS].map((asset) => asset.fileName)
    for (const asset of PEOPLE_ASSETS) {
      expect(others).not.toContain(asset.fileName)
    }
  })

  it('versions Phase-3 data independently of every other signal', () => {
    // The point of these being separate numbers is that bumping one cannot
    // invalidate CLIP vectors, OCR text, or visible-face counts.
    expect(FACE_EMBED_MODEL_VERSION).toBeGreaterThan(0)
    expect(PEOPLE_INDEX_VERSION).toBeGreaterThan(0)
    expect(PEOPLE_PACK_VERSION).toBeGreaterThan(0)
  })
})

describe('every asset is pinned and verifiable', () => {
  it('pins each url to an immutable commit rather than a branch', () => {
    for (const asset of PEOPLE_ASSETS) {
      // A 40-character hex revision, never a branch name that could move.
      expect(asset.url).toMatch(/\/[0-9a-f]{40}\//)
      expect(asset.url).not.toMatch(/\/(main|master|HEAD)\//)
    }
  })

  it('carries a full sha-256 and an exact byte count for each asset', () => {
    for (const asset of PEOPLE_ASSETS) {
      expect(asset.sha256).toMatch(/^[0-9a-f]{64}$/)
      expect(asset.sizeBytes).toBeGreaterThan(0)
      expect(Number.isInteger(asset.sizeBytes)).toBe(true)
    }
  })

  it('uses https for every asset', () => {
    for (const asset of PEOPLE_ASSETS) {
      expect(new URL(asset.url).protocol).toBe('https:')
    }
  })

  it('reports a total that matches the sum of its assets', () => {
    const summed = PEOPLE_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0)
    expect(PEOPLE_PACK_TOTAL_BYTES).toBe(summed)
  })

  it('is frozen against mutation at runtime', () => {
    expect(Object.isFrozen(PEOPLE_ASSETS)).toBe(true)
    for (const asset of PEOPLE_ASSETS) {
      expect(Object.isFrozen(asset)).toBe(true)
    }
  })
})

describe('the allowlist is bound to this pack alone', () => {
  it('accepts exactly the compiled-in urls', () => {
    for (const asset of PEOPLE_ASSETS) {
      expect(isAllowlistedPeopleUrl(asset.url)).toBe(true)
    }
  })

  it('rejects a url from another pack', () => {
    // Each pack's allowlist is an identity check against its own manifest, not a
    // general "is this GitHub" test, so packs cannot fetch each other's assets.
    for (const asset of [...MODEL_ASSETS, ...EXTRAS_ASSETS]) {
      expect(isAllowlistedPeopleUrl(asset.url)).toBe(false)
    }
    for (const asset of PEOPLE_ASSETS) {
      expect(isAllowlistedAssetUrl(asset.url)).toBe(false)
      expect(isAllowlistedExtrasUrl(asset.url)).toBe(false)
    }
  })

  it('rejects a same-host url that is not in the manifest', () => {
    expect(
      isAllowlistedPeopleUrl('https://media.githubusercontent.com/media/opencv/opencv_zoo/main/evil.onnx')
    ).toBe(false)
  })

  it('rejects an allowlisted path served from another host', () => {
    const asset = peopleAssetFor('faceEmbedModel')
    const moved = asset.url.replace('media.githubusercontent.com', 'example.invalid')
    expect(isAllowlistedPeopleUrl(moved)).toBe(false)
  })

  it('rejects plain http and unparseable input', () => {
    const asset = peopleAssetFor('faceEmbedModel')
    expect(isAllowlistedPeopleUrl(asset.url.replace('https:', 'http:'))).toBe(false)
    expect(isAllowlistedPeopleUrl('not a url')).toBe(false)
    expect(isAllowlistedPeopleUrl('')).toBe(false)
  })

  it('names only hosts it actually uses', () => {
    const hosts = new Set(PEOPLE_ASSETS.map((asset) => new URL(asset.url).hostname))
    for (const allowed of ALLOWED_PEOPLE_DOWNLOAD_HOSTS) {
      expect(hosts.has(allowed)).toBe(true)
    }
  })
})

describe('the measured model shape is recorded, not guessed', () => {
  it('states the input size and embedding width the pinned export actually uses', () => {
    expect(FACE_EMBED_INPUT_SIZE).toBe(112)
    expect(FACE_EMBED_DIMENSIONS).toBe(128)
  })

  it('resolves its one role and refuses an unknown one', () => {
    expect(peopleAssetFor('faceEmbedModel').fileName).toMatch(/\.onnx$/)
    expect(() => peopleAssetFor('nope' as never)).toThrow()
  })
})

describe('the consent notice is honest about the limits', () => {
  it('names the licence and the attribution', () => {
    expect(PEOPLE_PACK_LICENSE_NOTICE).toContain('Apache 2.0')
    expect(PEOPLE_PACK_LICENSE_NOTICE).toContain('OpenCV Zoo')
  })

  it('says matching is limited to people the user labelled themselves', () => {
    expect(PEOPLE_PACK_LICENSE_NOTICE).toMatch(/labelled yourself/i)
    expect(PEOPLE_PACK_LICENSE_NOTICE).toMatch(/never used to identify anyone you have not labelled/i)
  })

  it('warns that matching can be wrong rather than implying certainty', () => {
    expect(PEOPLE_PACK_LICENSE_NOTICE).toMatch(/can be wrong/i)
  })

  it('says the model stays on the device', () => {
    expect(PEOPLE_PACK_LICENSE_NOTICE).toMatch(/stays on this device/i)
  })
})
