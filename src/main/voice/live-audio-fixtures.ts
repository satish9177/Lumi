import { existsSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Synthetic utterances for the opt-in Gemini Live validation
 * (providers.live.test.ts), and the checks that keep a missing or malformed
 * fixture from surfacing as a bare ENOENT or as garbage audio.
 *
 * Format is the one Lumi streams to Gemini Live (voice-relay-contracts.ts,
 * pcm-audio.ts, gemini-live-relay.ts `audio/pcm;rate=16000`): raw PCM,
 * signed 16-bit little-endian, mono, 16 kHz. A fixture is either that raw
 * PCM (`<name>.pcm`) or a canonical PCM WAV (`<name>.wav`) whose data chunk
 * is exactly that. Only the PCM samples are ever sent, never a WAV header.
 *
 * Generate them with `npm run live:audio` (scripts/live/make-test-audio.ts).
 */

export const LIVE_AUDIO_RATE = 16_000
export const LIVE_AUDIO_CHANNELS = 1
export const LIVE_AUDIO_BITS = 16
export const DEFAULT_LIVE_AUDIO_DIR = join('dist', 'live-audio')

export interface LiveAudioFixture {
  name: string
  /** BCP-47 language the phrase is spoken in. */
  language: string
  phrase: string
}

/** Phrases match the assertions in providers.live.test.ts. */
export const LIVE_AUDIO_FIXTURES: readonly LiveAudioFixture[] = [
  // Main scenario: one compound request → Dermatology, evening, ≤ 1000 and "dermatolog" in the transcript.
  { name: 'en_compound', language: 'en-IN', phrase: 'Find me a dermatologist on Saturday evening under 1000 rupees and prepare the cheapest one.' },
  // Spoken over the model's answer to trigger barge-in.
  { name: 'en_stop', language: 'en-IN', phrase: 'Stop, stop. Wait a moment, please stop talking.' },
  // Multilingual scenario: the same search in English, code-switched Telugu–English, and Telugu.
  { name: 'en_in', language: 'en-IN', phrase: 'Find me a dermatologist Saturday evening under 1000 rupees.' },
  { name: 'te_mixed', language: 'te-IN', phrase: 'Saturday evening dermatologist appointment choodu, 1000 rupees lopala.' },
  { name: 'te_pure', language: 'te-IN', phrase: 'శనివారం సాయంత్రం చర్మ వైద్యుడి అపాయింట్‌మెంట్ 1000 రూపాయల లోపల చూడు.' }
]

export class LiveAudioFixtureError extends Error {
  constructor(problem: string) {
    super(
      `Gemini Live validation requires synthetic audio fixtures: ${problem}. ` +
      `Run \`npm run live:audio\` and set LUMI_LIVE_AUDIO_DIR=${DEFAULT_LIVE_AUDIO_DIR} (see docs/EVALS.md).`
    )
    this.name = 'LiveAudioFixtureError'
  }
}

/** Extract PCM16 LE mono 16 kHz samples from a WAV file, refusing anything else. */
export function pcmFromWav(wav: Buffer, label = 'WAV'): Buffer {
  const fail = (why: string): never => { throw new LiveAudioFixtureError(`${label} ${why}`) }
  if (wav.length < 12 || wav.toString('ascii', 0, 4) !== 'RIFF' || wav.toString('ascii', 8, 12) !== 'WAVE') fail('is not a RIFF/WAVE file')
  let format: { audioFormat: number; channels: number; rate: number; bits: number } | undefined
  let offset = 12
  while (offset + 8 <= wav.length) {
    const id = wav.toString('ascii', offset, offset + 4)
    const size = wav.readUInt32LE(offset + 4)
    const start = offset + 8
    if (start + size > wav.length) fail(`has a truncated "${id}" chunk`)
    if (id === 'fmt ') {
      if (size < 16) fail('has a malformed fmt chunk')
      format = {
        audioFormat: wav.readUInt16LE(start),
        channels: wav.readUInt16LE(start + 2),
        rate: wav.readUInt32LE(start + 4),
        bits: wav.readUInt16LE(start + 14)
      }
    } else if (id === 'data') {
      if (!format) fail('has no fmt chunk before its data chunk')
      const { audioFormat, channels, rate, bits } = format!
      if (audioFormat !== 1 || channels !== LIVE_AUDIO_CHANNELS || rate !== LIVE_AUDIO_RATE || bits !== LIVE_AUDIO_BITS) {
        fail(`is format=${audioFormat} ${channels}ch ${rate} Hz ${bits}-bit; expected PCM (1) mono 16000 Hz 16-bit`)
      }
      if (size === 0 || size % 2 !== 0) fail('has an empty or odd-length data chunk')
      return wav.subarray(start, start + size)
    }
    offset = start + size + (size % 2)
  }
  return fail('has no data chunk')
}

export function writeWav(pcm: Buffer): Buffer {
  const header = Buffer.alloc(44)
  header.write('RIFF', 0, 'ascii')
  header.writeUInt32LE(36 + pcm.length, 4)
  header.write('WAVE', 8, 'ascii')
  header.write('fmt ', 12, 'ascii')
  header.writeUInt32LE(16, 16)
  header.writeUInt16LE(1, 20)
  header.writeUInt16LE(LIVE_AUDIO_CHANNELS, 22)
  header.writeUInt32LE(LIVE_AUDIO_RATE, 24)
  header.writeUInt32LE(LIVE_AUDIO_RATE * LIVE_AUDIO_CHANNELS * (LIVE_AUDIO_BITS / 8), 28)
  header.writeUInt16LE(LIVE_AUDIO_CHANNELS * (LIVE_AUDIO_BITS / 8), 32)
  header.writeUInt16LE(LIVE_AUDIO_BITS, 34)
  header.write('data', 36, 'ascii')
  header.writeUInt32LE(pcm.length, 40)
  return Buffer.concat([header, pcm])
}

/** Load one fixture as raw PCM, preferring `<name>.pcm` over `<name>.wav`. */
export function loadLiveAudio(directory: string, name: string): Buffer {
  const raw = join(directory, `${name}.pcm`)
  if (existsSync(raw)) {
    const pcm = readFileSync(raw)
    if (pcm.length === 0 || pcm.length % 2 !== 0) throw new LiveAudioFixtureError(`${raw} is empty or not 16-bit PCM`)
    return pcm
  }
  const wav = join(directory, `${name}.wav`)
  if (!existsSync(wav)) throw new LiveAudioFixtureError(`${name}.pcm or ${name}.wav is missing from ${directory}`)
  return pcmFromWav(readFileSync(wav), wav)
}

/** Validate the directory and every fixture up front; returns them by name. */
export function requireLiveAudio(directory: string | undefined): Record<string, Buffer> {
  if (!directory?.trim()) throw new LiveAudioFixtureError('LUMI_LIVE_AUDIO_DIR is not set')
  if (!existsSync(directory) || !statSync(directory).isDirectory()) throw new LiveAudioFixtureError(`${directory} does not exist`)
  return Object.fromEntries(LIVE_AUDIO_FIXTURES.map(({ name }) => [name, loadLiveAudio(directory, name)]))
}
