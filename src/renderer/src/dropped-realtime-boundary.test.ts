import { afterEach, describe, expect, it, vi } from 'vitest'
import { RealtimeClient } from './realtime'
import type { ApprovedImagePayload } from '../../shared/contracts'

/**
 * The Realtime boundary for a dropped file.
 *
 * A confirmed image still reaches OpenAI — that is what the user approved. What
 * must not happen is the dropped file's temporary identifier becoming a handle
 * the model can later address as "the selected file".
 */

const originalWindow = globalThis.window
const clients: RealtimeClient[] = []

afterEach(() => {
  for (const client of clients.splice(0)) {
    client.disconnect()
  }
  vi.restoreAllMocks()
  globalThis.window = originalWindow
})

const IMAGE: ApprovedImagePayload = {
  resultId: 'dropped-abc',
  name: 'whiteboard.png',
  dataUrl: 'data:image/jpeg;base64,AAAA',
  mimeType: 'image/jpeg',
  width: 800,
  height: 600
}

async function connectedClient(): Promise<RealtimeClient> {
  // Demo mode speaks its reply; this stack has no speech synthesis.
  globalThis.window = {
    setTimeout,
    clearTimeout,
    speechSynthesis: { cancel: () => undefined, speak: () => undefined }
  } as unknown as Window & typeof globalThis
  vi.stubGlobal('SpeechSynthesisUtterance', class {})
  const client = new RealtimeClient({
    onState: () => undefined,
    onTranscript: () => undefined,
    onExplanation: () => undefined,
    onCaptureContextRequest: () => undefined,
    onFileSearchRequest: () => undefined,
    onToolProposal: () => undefined,
    onError: () => undefined
  })
  clients.push(client)
  await client.connect({ mode: 'mock', model: 'gpt-realtime-2.1' })
  return client
}

describe('dropped-file Realtime boundary', () => {
  it('retains an approved-folder photo as the selected file', async () => {
    const client = await connectedClient()

    await client.analyzeSelectedPhoto(IMAGE, 'What is this?')

    expect(client.hasSelectedPhoto()).toBe(true)
  })

  it('does not retain a dropped photo as a model-addressable selection', async () => {
    const client = await connectedClient()

    await client.analyzeSelectedPhoto(IMAGE, 'What is this?', false)

    // The image was still sent; only the reusable handle is withheld.
    expect(client.hasSelectedPhoto()).toBe(false)
  })

  it('clears a previous approved selection when a dropped photo is analysed', async () => {
    const client = await connectedClient()

    await client.analyzeSelectedPhoto({ ...IMAGE, resultId: 'approved-1' }, 'first')
    expect(client.hasSelectedPhoto()).toBe(true)

    await client.analyzeSelectedPhoto(IMAGE, 'second', false)

    // A stale approved id must not survive to answer "the selected file".
    expect(client.hasSelectedPhoto()).toBe(false)
  })
})
