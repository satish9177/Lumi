/**
 * Boundary tests for local vision.
 *
 * Local semantic search adds a settings surface the renderer can drive, so the
 * rule is no longer "no vision concept crosses the bridge" — it is that no
 * *path, pixel buffer, embedding, vector row, or model location* crosses it,
 * and that the Realtime model gains no way to ask for inference. These tests
 * fail if any of those three escape routes opens.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { IPC_CHANNELS, TOOL_NAMES } from '../../shared/contracts'
import { MODEL_ASSETS, MODEL_PACK_ID, MODEL_PACK_TOTAL_BYTES, isAllowlistedAssetUrl } from './manifest'
import { modelAssetPath, modelPackDirectory, photoIndexDirectory, visionModelDirectory } from './model-location'

const REPO_ROOT = fileURLToPath(new URL('../../..', import.meta.url))

/**
 * Implementation details that must never appear outside the main process. A
 * settings toggle may be named in the renderer; a model path, a tensor input
 * name, or a raw vector may not.
 */
const MAIN_ONLY_TERMS = [
  'onnxruntime',
  'vision-worker',
  'modelPath',
  'pixel_values',
  'image_embeds',
  'text_embeds',
  'bgraToClipTensor',
  'normalizedEmbedding',
  'vectorRow',
  'vectors.bin',
  'huggingface.co',
  'sha256'
]

function sourceFiles(relativeDirectory: string): string[] {
  const root = join(REPO_ROOT, relativeDirectory)
  const collected: string[] = []
  const walk = (directory: string): void => {
    for (const entry of readdirSync(directory)) {
      const path = join(directory, entry)
      if (statSync(path).isDirectory()) {
        walk(path)
      } else if (/\.(ts|tsx)$/.test(entry)) {
        collected.push(path)
      }
    }
  }
  walk(root)
  return collected
}

describe('local vision internals stay inside the main process', () => {
  it('exposes no model path, vector, or runtime detail through the preload bridge', () => {
    const preload = readFileSync(join(REPO_ROOT, 'src/preload/index.ts'), 'utf8')

    for (const term of MAIN_ONLY_TERMS) {
      expect(`preload contains ${term}: ${preload.includes(term)}`).toBe(`preload contains ${term}: false`)
    }
  })

  it('exposes no model path, vector, or runtime detail anywhere in the renderer', () => {
    const files = sourceFiles('src/renderer/src')
    expect(files.length).toBeGreaterThan(0)

    for (const file of files) {
      const contents = readFileSync(file, 'utf8')
      for (const term of MAIN_ONLY_TERMS) {
        expect(`${file}: ${contents.includes(term)}`).toBe(`${file}: false`)
      }
    }
  })

  it('keeps the shared contracts free of model paths, pixels, and vectors', () => {
    const contracts = readFileSync(join(REPO_ROOT, 'src/shared/contracts.ts'), 'utf8')

    for (const term of MAIN_ONLY_TERMS) {
      expect(`contracts contains ${term}: ${contracts.includes(term)}`).toBe(`contracts contains ${term}: false`)
    }
  })

  it('adds no model-callable tool, so no OpenAI-bound event can request inference', () => {
    // Semantic search rides on the existing search_documents tool rather than
    // adding a parallel one the model could aim somewhere else.
    expect([...TOOL_NAMES]).toEqual([
      'create_reminder',
      'search_documents',
      'open_file',
      'open_url',
      'save_context',
      'send_telegram_message',
      'send_telegram_attachment',
      'analyze_photo'
    ])
  })

  it('names no IPC channel after a path, a pixel buffer, or an embedding', () => {
    for (const [name, channel] of Object.entries(IPC_CHANNELS)) {
      expect(name).not.toMatch(/path|bitmap|pixel|embed|vector|onnx/i)
      expect(channel).not.toMatch(/path|bitmap|pixel|embed|vector|onnx/i)
    }
  })

  it('never registers an IPC handler that takes a model or image path', () => {
    const mainIndex = readFileSync(join(REPO_ROOT, 'src/main/index.ts'), 'utf8')
    expect(mainIndex).not.toMatch(/ipcMain\.[a-z]+\([^)]*modelPath/i)
  })
})

describe('model and index locations are application-authored', () => {
  const userData = 'C:\\Users\\dev\\AppData\\Roaming\\lifelens'

  it('derives every path from userData plus a compiled-in constant', () => {
    expect(visionModelDirectory(userData)).toBe(join(userData, 'vision-models'))
    expect(modelPackDirectory(userData)).toBe(join(userData, 'vision-models', MODEL_PACK_ID))
    expect(photoIndexDirectory(userData)).toBe(join(userData, 'photo-index'))
  })

  it('resolves every manifest asset inside the pack directory', () => {
    const packDirectory = modelPackDirectory(userData)

    for (const asset of MODEL_ASSETS) {
      const resolved = modelAssetPath(userData, asset.fileName)
      expect(resolved).toBe(join(packDirectory, asset.fileName))
      expect(resolved.startsWith(`${packDirectory}\\`) || resolved.startsWith(`${packDirectory}/`)).toBe(true)
    }
  })
})

describe('the download allowlist cannot be widened', () => {
  it('accepts only the compiled-in manifest URLs', () => {
    for (const asset of MODEL_ASSETS) {
      expect(isAllowlistedAssetUrl(asset.url)).toBe(true)
    }
  })

  it('rejects any other URL, including look-alike hosts and downgraded schemes', () => {
    const rejected = [
      'https://huggingface.co/other/model.onnx',
      'https://huggingface.co.evil.example/model.onnx',
      'http://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx',
      'file:///C:/Windows/System32/calc.exe',
      'https://example.invalid/model.onnx',
      '',
      'not a url'
    ]

    for (const candidate of rejected) {
      expect(`${candidate}: ${isAllowlistedAssetUrl(candidate)}`).toBe(`${candidate}: false`)
    }
  })

  it('states a real download size the user can consent to', () => {
    // The settings card discloses this figure before anything is fetched.
    expect(MODEL_PACK_TOTAL_BYTES).toBe(MODEL_ASSETS.reduce((total, asset) => total + asset.sizeBytes, 0))
    expect(MODEL_PACK_TOTAL_BYTES).toBeGreaterThan(100 * 1024 * 1024)
    expect(MODEL_PACK_TOTAL_BYTES).toBeLessThan(300 * 1024 * 1024)
  })

  it('is frozen, so no code path can retarget an asset at runtime', () => {
    expect(Object.isFrozen(MODEL_ASSETS)).toBe(true)
    for (const asset of MODEL_ASSETS) {
      expect(Object.isFrozen(asset)).toBe(true)
    }
  })

  it('pins every asset to an immutable commit rather than a moving branch', () => {
    for (const asset of MODEL_ASSETS) {
      expect(asset.url).toMatch(/\/resolve\/[0-9a-f]{40}\//)
      expect(asset.url).not.toContain('/resolve/main/')
      expect(asset.sha256).toMatch(/^[0-9a-f]{64}$/)
      expect(asset.sizeBytes).toBeGreaterThan(0)
    }
  })
})
