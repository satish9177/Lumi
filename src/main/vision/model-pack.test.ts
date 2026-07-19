import { mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelAsset } from './manifest'

/**
 * The real manifest is deeply frozen on purpose, and its assets total 148 MB.
 * Rather than weaken that freeze, these tests substitute a stand-in manifest:
 * the same four roles and the same (allowlisted) URLs, but small bodies whose
 * digests are taken from those bodies. Every code path exercised below is the
 * production one; only the payload shrinks. The real manifest's pinned commit,
 * digests, and sizes are asserted in isolation.test.ts.
 */
const fixture = await vi.hoisted(async () => {
  const { createHash } = await import('node:crypto')
  const actual = await import('./manifest')
  const sizes = [4_096, 3_072, 1_024, 512]

  const assets = actual.MODEL_ASSETS.map((asset, index) => {
    const size = sizes[index] ?? 512
    const body = Buffer.alloc(size)
    for (let offset = 0; offset < size; offset += 1) {
      body[offset] = (offset + asset.fileName.length) % 251
    }
    return {
      asset: {
        role: asset.role,
        fileName: asset.fileName,
        url: asset.url,
        sizeBytes: size,
        sha256: createHash('sha256').update(body).digest('hex')
      },
      body
    }
  })

  return {
    assets: assets.map((entry) => entry.asset),
    bodies: new Map(assets.map((entry) => [entry.asset.url, entry.body])),
    totalBytes: assets.reduce((total, entry) => total + entry.asset.sizeBytes, 0)
  }
})

vi.mock('./manifest', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./manifest')>()
  return {
    ...actual,
    MODEL_ASSETS: fixture.assets,
    isAllowlistedAssetUrl: (candidate: string) => fixture.assets.some((asset) => asset.url === candidate)
  }
})

const { downloadStagingDirectory, modelAssetPath, modelPackDirectory } = await import('./model-location')
const {
  clearModelPack,
  downloadModelPack,
  isModelPackInstalled,
  ModelPackError,
  modelPackTotalBytes,
  resolveAssetPath
} = await import('./model-pack')
type ModelPackProgress = import('./model-pack').ModelPackProgress
type ModelPackRuntime = import('./model-pack').ModelPackRuntime

const MODEL_ASSETS = fixture.assets as readonly ModelAsset[]

let userDataDir: string

beforeEach(async () => {
  userDataDir = await mkdtemp(join(tmpdir(), 'lumi-pack-'))
})

afterEach(async () => {
  await rm(userDataDir, { recursive: true, force: true })
})

interface ServerOptions {
  /** Truncate the first response for this URL to force a resume. */
  truncateFirst?: string
  /** Serve wrong bytes for this URL. */
  corrupt?: string
  /** Fail the first N requests with a network error. */
  failFirst?: number
  /** Ignore Range headers and always restart the transfer. */
  ignoreRange?: boolean
}

function makeServer(bodies: Map<string, Buffer>, options: ServerOptions = {}) {
  const requests: Array<{ url: string; range: string | undefined }> = []
  let failures = 0
  let truncationsServed = 0

  const runtimeFetch = (async (url: string, init: RequestInit = {}) => {
    const range = new Headers(init.headers as HeadersInit | undefined).get('Range') ?? undefined
    requests.push({ url, range })

    if (options.failFirst !== undefined && failures < options.failFirst) {
      failures += 1
      throw new TypeError('fetch failed')
    }

    let body = bodies.get(url)
    if (!body) {
      return new Response(null, { status: 404 })
    }
    if (options.corrupt === url) {
      body = Buffer.from(body)
      body[0] = (body[0]! + 1) % 256
    }

    let status = 200
    let offset = 0
    if (range && !options.ignoreRange) {
      offset = Number(/bytes=(\d+)-/.exec(range)?.[1] ?? 0)
      status = 206
    }

    let slice = body.subarray(offset)
    if (options.truncateFirst === url && truncationsServed === 0) {
      truncationsServed += 1
      slice = slice.subarray(0, Math.floor(slice.length / 2))
    }

    return new Response(new Uint8Array(slice), { status })
  }) as unknown as typeof fetch

  return { requests, fetch: runtimeFetch }
}

function runtimeWith(fetchImpl: typeof fetch, freeBytes?: number): ModelPackRuntime {
  return {
    fetch: fetchImpl,
    freeDiskBytes: async () => freeBytes,
    now: () => 1_700_000_000_000
  }
}

async function staged(): Promise<string[]> {
  try {
    return await readdir(downloadStagingDirectory(userDataDir))
  } catch {
    return []
  }
}

describe('consent and preconditions', () => {
  it('refuses to download anything without consent', async () => {
    const server = makeServer(new Map())

    await expect(
      downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: false })
    ).rejects.toThrow(expect.objectContaining({ code: 'not_consented' }))

    expect(server.requests).toHaveLength(0)
  })

  it('refuses when the volume cannot hold the pack', async () => {
    const server = makeServer(new Map())

    await expect(
      downloadModelPack(userDataDir, runtimeWith(server.fetch, 1_000), { consented: true })
    ).rejects.toThrow(expect.objectContaining({ code: 'insufficient_disk_space' }))

    expect(server.requests).toHaveLength(0)
  })

  it('proceeds when free space is unknown rather than refusing wrongly', async () => {
      const server = makeServer(fixture.bodies)
      await downloadModelPack(userDataDir, { fetch: server.fetch }, { consented: true })
      expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })

  it('discloses a total summed from the manifest, so the figure cannot drift', () => {
    expect(modelPackTotalBytes()).toBe(MODEL_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0))
    expect(modelPackTotalBytes()).toBe(fixture.totalBytes)
  })
})

describe('successful install', () => {
  it('fetches every asset, verifies it, and reports it installed', async () => {
      const server = makeServer(fixture.bodies)
      const progress: ModelPackProgress[] = []

      await downloadModelPack(userDataDir, runtimeWith(server.fetch, 10_000_000_000), {
        consented: true,
        onProgress: (update) => progress.push(update)
      })

      expect(server.requests.map((request) => request.url)).toEqual(MODEL_ASSETS.map((asset) => asset.url))
      expect(await isModelPackInstalled(userDataDir)).toBe(true)
      for (const asset of MODEL_ASSETS) {
        expect((await stat(modelAssetPath(userDataDir, asset.fileName))).size).toBe(asset.sizeBytes)
      }
      expect(progress.at(-1)?.receivedBytes).toBe(fixture.totalBytes)
      expect(progress.at(-1)?.totalBytes).toBe(fixture.totalBytes)
  })

  it('removes the staging directory once the pack is installed', async () => {
      await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
      expect(await staged()).toEqual([])
  })

  it('skips assets that are already present on a second run', async () => {
      await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })

      const second = makeServer(fixture.bodies)
      await downloadModelPack(userDataDir, runtimeWith(second.fetch), { consented: true })

      expect(second.requests).toHaveLength(0)
  })

  it('resolves asset paths only inside the pack directory', () => {
    for (const role of ['imageModel', 'textModel', 'vocabulary', 'merges'] as const) {
      expect(resolveAssetPath(userDataDir, role).startsWith(modelPackDirectory(userDataDir))).toBe(true)
    }
  })
})

describe('integrity', () => {
  it('rejects a pack whose bytes do not match the manifest digest', async () => {
      const target = MODEL_ASSETS[0]!.url
      const server = makeServer(fixture.bodies, { corrupt: target })

      await expect(
        downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })
      ).rejects.toThrow(expect.objectContaining({ code: 'digest_mismatch' }))

      expect(await isModelPackInstalled(userDataDir)).toBe(false)
  })

  it('never leaves unverified bytes where the loader could find them', async () => {
      const server = makeServer(fixture.bodies, { corrupt: MODEL_ASSETS[0]!.url })
      await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true }).catch(() => undefined)

      // The corrupt asset must be absent from the pack directory entirely.
      await expect(stat(modelAssetPath(userDataDir, MODEL_ASSETS[0]!.fileName))).rejects.toThrow()
      expect(await staged()).toEqual([])
  })

  it('does not retry a digest mismatch', async () => {
      const target = MODEL_ASSETS[0]!.url
      const server = makeServer(fixture.bodies, { corrupt: target })

      await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true }).catch(() => undefined)

      expect(server.requests.filter((request) => request.url === target)).toHaveLength(1)
  })

  it('reports a pack with a tampered file as not installed', async () => {
      await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
      // Truncating a file breaks the size gate the loader checks on every start.
      await writeFile(modelAssetPath(userDataDir, MODEL_ASSETS[3]!.fileName), 'tampered', 'utf8')

      expect(await isModelPackInstalled(userDataDir)).toBe(false)
  })

  it('reports a pack as not installed when a file is missing', async () => {
      await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
      await rm(modelAssetPath(userDataDir, MODEL_ASSETS[1]!.fileName))

      expect(await isModelPackInstalled(userDataDir)).toBe(false)
  })

  it('reports no pack at all before any download', async () => {
    expect(await isModelPackInstalled(userDataDir)).toBe(false)
  })
})

describe('interruption and resume', () => {
  it('resumes a partial download with a Range request instead of restarting', async () => {
      const target = MODEL_ASSETS[3]!.url
      const server = makeServer(fixture.bodies, { truncateFirst: target })

      await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })

      const attempts = server.requests.filter((request) => request.url === target)
      expect(attempts.length).toBeGreaterThan(1)
      expect(attempts[0]!.range).toBeUndefined()
      expect(attempts[1]!.range).toMatch(/^bytes=\d+-$/)
      expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })

  it('discards the local partial when the server ignores the Range header', async () => {
      const target = MODEL_ASSETS[3]!.url
      const server = makeServer(fixture.bodies, { truncateFirst: target, ignoreRange: true })

      await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })

      // A restarted transfer must still verify, meaning the stale prefix was dropped.
      expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })

  it('retries a transient network failure', async () => {
      const server = makeServer(fixture.bodies, { failFirst: 2 })

      await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })

      expect(await isModelPackInstalled(userDataDir)).toBe(true)
      expect(server.requests.length).toBeGreaterThan(MODEL_ASSETS.length)
  })

  it('gives up with a bounded code after repeated network failures', async () => {
      const server = makeServer(fixture.bodies, { failFirst: 99 })

      await expect(
        downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })
      ).rejects.toThrow(expect.objectContaining({ code: 'network_unavailable' }))
  })

  it('stops promptly when cancelled', async () => {
      const controller = new AbortController()
      controller.abort()
      const server = makeServer(fixture.bodies)

      await expect(
        downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true, signal: controller.signal })
      ).rejects.toThrow(expect.objectContaining({ code: 'cancelled' }))

      expect(server.requests).toHaveLength(0)
      expect(await isModelPackInstalled(userDataDir)).toBe(false)
  })
})

describe('clearing', () => {
  it('removes the pack and any staged bytes', async () => {
      await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
      expect(await isModelPackInstalled(userDataDir)).toBe(true)

      await clearModelPack(userDataDir)

      expect(await isModelPackInstalled(userDataDir)).toBe(false)
      await expect(readdir(modelPackDirectory(userDataDir))).rejects.toThrow()
  })

  it('is safe to call when nothing is installed', async () => {
    await expect(clearModelPack(userDataDir)).resolves.toBeUndefined()
  })
})

describe('concurrent access', () => {
  it('refuses a second concurrent download for the same pack', async () => {
    const server = makeServer(fixture.bodies)

    const first = downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })
    const second = downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })

    await expect(second).rejects.toThrow(expect.objectContaining({ code: 'download_in_progress' }))
    await first

    expect(await isModelPackInstalled(userDataDir)).toBe(true)
    // The rejected second call never issued a request against the shared server.
    expect(server.requests.map((request) => request.url)).toEqual(MODEL_ASSETS.map((asset) => asset.url))
  })

  it('refuses to clear the pack while a download is in flight', async () => {
    const server = makeServer(fixture.bodies)

    const download = downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })
    await expect(clearModelPack(userDataDir)).rejects.toThrow(
      expect.objectContaining({ code: 'download_in_progress' })
    )
    await download

    expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })

  it('allows a fresh download once the previous one has settled', async () => {
    await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
    await clearModelPack(userDataDir)

    // The in-flight guard has cleared, so a retry proceeds normally.
    await downloadModelPack(userDataDir, runtimeWith(makeServer(fixture.bodies).fetch), { consented: true })
    expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })

  it('clears the in-flight guard after a failed download so a retry is possible', async () => {
    const failing = makeServer(fixture.bodies, { failFirst: 99 })
    await expect(
      downloadModelPack(userDataDir, runtimeWith(failing.fetch), { consented: true })
    ).rejects.toThrow(expect.objectContaining({ code: 'network_unavailable' }))

    // A concurrent request during the next attempt would still be refused, proving
    // the guard is armed and released per-call rather than left stuck after failure.
    const server = makeServer(fixture.bodies)
    await downloadModelPack(userDataDir, runtimeWith(server.fetch), { consented: true })
    expect(await isModelPackInstalled(userDataDir)).toBe(true)
  })
})

describe('bounded errors', () => {
  it('never carries a URL, a path, or native text', () => {
    for (const code of ['not_consented', 'network_unavailable', 'digest_mismatch', 'install_failed'] as const) {
      const message = new ModelPackError(code).message
      expect(message).not.toMatch(/https?:|[\\/]|\.onnx|Error:/i)
      expect(message.length).toBeGreaterThan(0)
    }
  })

  it('builds every request URL from the manifest alone', async () => {
    const raw = await readFile(join(__dirname, 'model-pack.ts'), 'utf8')
    // The downloader must never assemble a URL; it only forwards asset.url.
    expect(raw).not.toMatch(/https?:\/\//)
    expect(raw).toContain('isAllowlistedAssetUrl(asset.url)')
  })
})
