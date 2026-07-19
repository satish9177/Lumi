import { describe, expect, it } from 'vitest'
import {
  CLIP_CONTEXT_LENGTH,
  CLIP_TOKEN_BYTES,
  parseVisionCommand,
  parseVisionEvent,
  VISION_BITMAP_BYTES,
  VISION_ERROR_CODES,
  VISION_ERROR_MESSAGES,
  VisionProtocolError
} from './protocol'

function bitmap(bytes = VISION_BITMAP_BYTES): ArrayBuffer {
  return new ArrayBuffer(bytes)
}

function embedImageCommand(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'embed_image',
    requestId: 'r1',
    width: 224,
    height: 224,
    format: 'bgra',
    bitmap: bitmap(),
    ...overrides
  }
}

function tokens(count = CLIP_CONTEXT_LENGTH): ArrayBuffer {
  return new Int32Array(count).buffer
}

/** Returns the bounded code so a failed expectation names the actual reason. */
function codeOf(run: () => unknown): string {
  try {
    run()
  } catch (error) {
    return error instanceof VisionProtocolError ? error.code : 'not-a-protocol-error'
  }
  return 'no-error'
}

describe('command parsing', () => {
  it('accepts a well-formed load, unload, embed, and shutdown command', () => {
    expect(parseVisionCommand({ type: 'load_model', kind: 'image', modelPath: 'C:\\models\\m.onnx' })).toEqual({
      type: 'load_model',
      kind: 'image',
      modelPath: 'C:\\models\\m.onnx'
    })
    expect(parseVisionCommand({ type: 'unload_model', kind: 'image' })).toEqual({
      type: 'unload_model',
      kind: 'image'
    })
    expect(parseVisionCommand({ type: 'shutdown' })).toEqual({ type: 'shutdown' })
    expect(parseVisionCommand(embedImageCommand()).type).toBe('embed_image')
  })

  it('rejects an unknown command', () => {
    expect(codeOf(() => parseVisionCommand({ type: 'run_arbitrary_model' }))).toBe('unknown_command')
    expect(codeOf(() => parseVisionCommand({ type: 'eval' }))).toBe('unknown_command')
  })

  it('rejects a non-object message', () => {
    for (const raw of [null, undefined, 42, 'load_model', [], true]) {
      expect(codeOf(() => parseVisionCommand(raw))).toBe('invalid_message')
    }
  })

  it('refuses extra properties that could smuggle a URL or a path', () => {
    expect(
      codeOf(() =>
        parseVisionCommand({ type: 'load_model', kind: 'image', modelPath: 'a', modelUrl: 'https://example.invalid' })
      )
    ).toBe('invalid_message')
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ sourcePath: 'C:\\photos\\a.jpg' })))).toBe(
      'invalid_message'
    )
    expect(codeOf(() => parseVisionCommand({ type: 'shutdown', andAlso: 1 }))).toBe('invalid_message')
  })

  it('rejects an unsupported model kind', () => {
    expect(codeOf(() => parseVisionCommand({ type: 'load_model', kind: 'audio', modelPath: 'a' }))).toBe(
      'invalid_message'
    )
    expect(codeOf(() => parseVisionCommand({ type: 'unload_model', kind: 'everything' }))).toBe('invalid_message')
  })

  it('rejects a missing or oversized model path', () => {
    expect(codeOf(() => parseVisionCommand({ type: 'load_model', kind: 'image', modelPath: '' }))).toBe(
      'invalid_message'
    )
    expect(codeOf(() => parseVisionCommand({ type: 'load_model', kind: 'image', modelPath: 'x'.repeat(1_025) }))).toBe(
      'invalid_message'
    )
  })

  it('rejects a bitmap of the wrong dimensions', () => {
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ width: 225 })))).toBe('invalid_bitmap')
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ height: 0 })))).toBe('invalid_bitmap')
  })

  it('rejects a bitmap of the wrong byte length', () => {
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ bitmap: bitmap(VISION_BITMAP_BYTES - 4) })))).toBe(
      'invalid_bitmap'
    )
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ bitmap: bitmap(VISION_BITMAP_BYTES + 4) })))).toBe(
      'invalid_bitmap'
    )
  })

  it('rejects a bitmap that is not an ArrayBuffer', () => {
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ bitmap: new Uint8Array(VISION_BITMAP_BYTES) })))).toBe(
      'invalid_bitmap'
    )
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ bitmap: 'AAAA' })))).toBe('invalid_bitmap')
  })

  it('rejects an unexpected bitmap format', () => {
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ format: 'rgba' })))).toBe('invalid_bitmap')
  })

  it('rejects a malformed request id', () => {
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ requestId: '' })))).toBe('invalid_message')
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ requestId: 'x'.repeat(65) })))).toBe('invalid_message')
    expect(codeOf(() => parseVisionCommand(embedImageCommand({ requestId: 7 })))).toBe('invalid_message')
  })

  it('accepts a full-width token buffer', () => {
    expect(
      parseVisionCommand({ type: 'embed_text', requestId: 'r1', tokenIds: tokens(), tokenCount: 3 }).type
    ).toBe('embed_text')
    expect(new Int32Array(CLIP_CONTEXT_LENGTH).byteLength).toBe(CLIP_TOKEN_BYTES)
  })

  it('rejects a token buffer of the wrong width', () => {
    expect(
      codeOf(() => parseVisionCommand({ type: 'embed_text', requestId: 'r1', tokenIds: tokens(76), tokenCount: 3 }))
    ).toBe('invalid_tokens')
    expect(
      codeOf(() => parseVisionCommand({ type: 'embed_text', requestId: 'r1', tokenIds: [1, 2], tokenCount: 2 }))
    ).toBe('invalid_tokens')
  })

  it('rejects an out-of-range token count', () => {
    for (const tokenCount of [0, 1, CLIP_CONTEXT_LENGTH + 1, 1.5, -3]) {
      expect(
        codeOf(() => parseVisionCommand({ type: 'embed_text', requestId: 'r1', tokenIds: tokens(), tokenCount }))
      ).toBe('invalid_tokens')
    }
  })
})

describe('event parsing', () => {
  function vector(length = 512): ArrayBuffer {
    return Float32Array.from({ length }, (_value, index) => (index % 7) + 1).buffer
  }

  it('accepts the lifecycle events', () => {
    expect(parseVisionEvent({ type: 'ready', runtimeVersion: '1.27.0' })).toEqual({
      type: 'ready',
      runtimeVersion: '1.27.0'
    })
    expect(parseVisionEvent({ type: 'model_loaded', kind: 'text', sessionLoadMs: 12 })).toEqual({
      type: 'model_loaded',
      kind: 'text',
      sessionLoadMs: 12
    })
    expect(parseVisionEvent({ type: 'model_unloaded', kind: 'image' })).toEqual({
      type: 'model_unloaded',
      kind: 'image'
    })
  })

  it('accepts an embedding result of the one allowed width', () => {
    expect(
      parseVisionEvent({
        type: 'embedding_result',
        requestId: 'r1',
        kind: 'image',
        vector: vector(512),
        elapsedMs: 5,
        workerRssBytes: 1
      }).type
    ).toBe('embedding_result')
  })

  it('rejects an embedding of an unexpected width, including the unprojected 768 pooler width', () => {
    for (const length of [0, 511, 513, 768, 1_024]) {
      expect(
        codeOf(() =>
          parseVisionEvent({
            type: 'embedding_result',
            requestId: 'r1',
            kind: 'image',
            vector: vector(length),
            elapsedMs: 5,
            workerRssBytes: 1
          })
        )
      ).toBe('unexpected_embedding_length')
    }
  })

  it('rejects an embedding containing NaN or Infinity', () => {
    for (const poison of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      const values = new Float32Array(512).fill(0.1)
      values[17] = poison
      expect(
        codeOf(() =>
          parseVisionEvent({
            type: 'embedding_result',
            requestId: 'r1',
            kind: 'image',
            vector: values.buffer,
            elapsedMs: 5,
            workerRssBytes: 1
          })
        )
      ).toBe('non_finite_embedding')
    }
  })

  it('rejects a vector that is not an ArrayBuffer', () => {
    expect(
      codeOf(() =>
        parseVisionEvent({
          type: 'embedding_result',
          requestId: 'r1',
          kind: 'image',
          vector: [1, 2, 3],
          elapsedMs: 5,
          workerRssBytes: 1
        })
      )
    ).toBe('invalid_output')
  })

  it('rejects an unrecognised error code', () => {
    expect(codeOf(() => parseVisionEvent({ type: 'bounded_error', code: 'stack_trace' }))).toBe('invalid_message')
  })

  it('rejects an unknown event type', () => {
    expect(codeOf(() => parseVisionEvent({ type: 'log', text: 'C:\\models\\clip.onnx' }))).toBe('invalid_message')
  })

  it('rejects negative or non-numeric durations', () => {
    expect(codeOf(() => parseVisionEvent({ type: 'model_loaded', kind: 'image', sessionLoadMs: -1 }))).toBe(
      'invalid_message'
    )
    expect(codeOf(() => parseVisionEvent({ type: 'model_loaded', kind: 'image', sessionLoadMs: 'fast' }))).toBe(
      'invalid_message'
    )
  })
})

describe('bounded error messages', () => {
  it('defines an app-authored message for every code', () => {
    for (const code of VISION_ERROR_CODES) {
      expect(typeof VISION_ERROR_MESSAGES[code]).toBe('string')
      expect(VISION_ERROR_MESSAGES[code].length).toBeGreaterThan(0)
    }
  })

  it('never exposes a path, a DLL name, or a stack frame', () => {
    for (const code of VISION_ERROR_CODES) {
      expect(VISION_ERROR_MESSAGES[code]).not.toMatch(/[\\/]|\.dll|\.onnx|\bat \b|Error:/i)
    }
  })
})
