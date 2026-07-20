import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CaptureResult } from '../../shared/contracts'
import { RetainedCaptureStore } from './retained-captures'

const capture = (id: string): CaptureResult => ({
  id,
  sourceId: 'screen:1:0',
  sourceKind: 'screen',
  label: 'Primary screen',
  dataUrl: `data:image/jpeg;base64,${id}`,
  mimeType: 'image/jpeg',
  width: 560,
  height: 315,
  capturedAt: '2026-07-20T12:00:00.000Z'
})

afterEach(() => vi.useRealTimers())

describe('RetainedCaptureStore', () => {
  it('keeps only the newest capture and rejects unknown ids', () => {
    const store = new RetainedCaptureStore()
    store.replace(capture('capture-one'))
    store.replace(capture('capture-two'))

    expect(store.get('capture-one')).toBeUndefined()
    expect(store.get('unknown')).toBeUndefined()
    expect(store.get('capture-two')?.dataUrl).toBe('data:image/jpeg;base64,capture-two')
    store.clear()
  })

  it('expires and clears the retained image', () => {
    vi.useFakeTimers()
    const store = new RetainedCaptureStore(1_000)
    store.replace(capture('capture-one'))
    vi.advanceTimersByTime(1_000)
    expect(store.get('capture-one')).toBeUndefined()
  })

  it('clears a capture explicitly when the session closes', () => {
    const store = new RetainedCaptureStore()
    store.replace(capture('capture-one'))
    store.clear()
    expect(store.get('capture-one')).toBeUndefined()
  })
})
