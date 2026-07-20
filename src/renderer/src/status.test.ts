import { describe, expect, it } from 'vitest'
import { deriveStatus, type StatusInputs } from './status'

const IDLE: StatusInputs = {
  companionState: 'idle',
  isConnecting: false,
  hasConnectedBefore: false,
  online: true,
  isSending: false,
  isSearching: false
}

const indexing = { state: 'indexing', indexed: 1240, total: 3000 } as const

describe('status precedence', () => {
  it('reports Ready when nothing is happening', () => {
    expect(deriveStatus(IDLE)).toEqual({ tone: 'idle', label: 'Ready', suffix: undefined })
  })

  it('puts needs-attention above every other signal', () => {
    const status = deriveStatus({
      ...IDLE,
      companionState: 'error',
      online: false,
      isSending: true,
      isSearching: true,
      isConnecting: true,
      photoSearchStatus: indexing
    })

    expect(status.label).toBe('Needs attention')
  })

  it('puts offline above sending, searching and connecting', () => {
    const status = deriveStatus({
      ...IDLE,
      online: false,
      isSending: true,
      isSearching: true,
      isConnecting: true,
      photoSearchStatus: indexing
    })

    expect(status.label).toBe('Offline')
  })

  it('puts sending above searching', () => {
    expect(deriveStatus({ ...IDLE, isSending: true, isSearching: true }).label).toBe('Sending')
  })

  it('puts searching above connecting and the live states', () => {
    expect(deriveStatus({ ...IDLE, isSearching: true, isConnecting: true, companionState: 'thinking' }).label).toBe('Searching')
  })

  it('puts thinking above listening and speaking', () => {
    expect(deriveStatus({ ...IDLE, companionState: 'thinking' }).label).toBe('Thinking')
  })

  it('reports listening and speaking', () => {
    expect(deriveStatus({ ...IDLE, companionState: 'listening' }).label).toBe('Listening')
    expect(deriveStatus({ ...IDLE, companionState: 'speaking' }).label).toBe('Speaking')
  })

  it('distinguishes a first connection from a reconnection', () => {
    expect(deriveStatus({ ...IDLE, isConnecting: true }).label).toBe('Connecting')
    expect(deriveStatus({ ...IDLE, isConnecting: true, hasConnectedBefore: true }).label).toBe('Reconnecting')
  })
})

describe('background indexing', () => {
  it('reports indexing progress only when Lumi is otherwise idle', () => {
    const status = deriveStatus({ ...IDLE, photoSearchStatus: indexing })

    expect(status.tone).toBe('indexing')
    expect(status.label).toBe('Indexing photos 1,240 of 3,000')
  })

  it('never lets indexing mask a live state', () => {
    for (const companionState of ['listening', 'thinking', 'speaking'] as const) {
      expect(deriveStatus({ ...IDLE, companionState, photoSearchStatus: indexing }).tone).not.toBe('indexing')
    }
    expect(deriveStatus({ ...IDLE, isSearching: true, photoSearchStatus: indexing }).label).toBe('Searching')
  })

  it('omits the count until the total is known', () => {
    expect(deriveStatus({ ...IDLE, photoSearchStatus: { state: 'indexing', indexed: 0, total: 0 } }).label).toBe('Indexing photos')
  })

  it('describes the model download and verification in plain words', () => {
    expect(deriveStatus({ ...IDLE, photoSearchStatus: { state: 'downloading', indexed: 0, total: 0 } }).label).toBe('Getting the photo model')
    expect(deriveStatus({ ...IDLE, photoSearchStatus: { state: 'verifying', indexed: 0, total: 0 } }).label).toBe('Checking the download')
  })

  it('stays Ready when indexing has finished', () => {
    expect(deriveStatus({ ...IDLE, photoSearchStatus: { state: 'ready', indexed: 10, total: 10 } }).label).toBe('Ready')
  })
})

describe('demo mode', () => {
  it('rides along as a suffix rather than replacing the state', () => {
    expect(deriveStatus({ ...IDLE, mode: 'mock' })).toEqual({ tone: 'idle', label: 'Ready', suffix: 'Demo mode' })
    expect(deriveStatus({ ...IDLE, mode: 'mock', companionState: 'listening' }).label).toBe('Listening')
  })

  it('is absent for a live session', () => {
    expect(deriveStatus({ ...IDLE, mode: 'live' }).suffix).toBeUndefined()
  })
})
