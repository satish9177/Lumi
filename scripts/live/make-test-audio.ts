/**
 * Generate the synthetic utterances used by the opt-in Gemini Live validation
 * (src/main/voice/providers.live.test.ts).
 *
 *   npm run live:audio                       # writes dist/live-audio/*.wav
 *   npm run live:audio -- --out <directory>  # somewhere else
 *
 * Speech comes from Vertex AI `gemini-2.5-flash-tts` (override with
 * LUMI_LIVE_TTS_MODEL / LUMI_LIVE_TTS_VOICE), authenticated with the same
 * Application Default Credentials Lumi uses (`gcloud auth application-default
 * login`; project from LUMI_VERTEX_PROJECT, GOOGLE_CLOUD_PROJECT or the ADC
 * quota project). The phrases are fixed in live-audio-fixtures.ts; TTS output
 * is not bit-identical between runs, so manifest.json records the model,
 * voice and a SHA-256 of each file.
 *
 * Output: canonical PCM WAV, 16 kHz, mono, signed 16-bit little-endian (the
 * Gemini Live input format), resampled from the TTS model's 24 kHz output.
 * Nothing is committed; dist/ is git-ignored.
 */

import { createHash } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { ApplicationDefaultCredentials } from '../../src/main/models/google-auth'
import {
  DEFAULT_LIVE_AUDIO_DIR,
  LIVE_AUDIO_FIXTURES,
  LIVE_AUDIO_RATE,
  loadLiveAudio,
  writeWav
} from '../../src/main/voice/live-audio-fixtures'

const MODEL = process.env.LUMI_LIVE_TTS_MODEL ?? 'gemini-2.5-flash-tts'
const VOICE = process.env.LUMI_LIVE_TTS_VOICE ?? 'Kore'
const LOCATION = process.env.LUMI_VERTEX_LOCATION ?? 'us-central1'
const LEAD_SILENCE_MS = 300

const outFlag = process.argv.indexOf('--out')
const OUT = resolve(outFlag > 0 && process.argv[outFlag + 1] ? process.argv[outFlag + 1] : DEFAULT_LIVE_AUDIO_DIR)

/** Parse `rate=NNNNN` out of an `audio/L16;codec=pcm;rate=24000` style mime type. */
function rateOf(mimeType: string): number {
  const match = /rate=(\d+)/.exec(mimeType)
  if (!match || !/^audio\/(l16|pcm)/i.test(mimeType)) throw new Error(`unexpected TTS audio type ${mimeType}`)
  return Number(match[1])
}

/**
 * Resample PCM16 mono with a Hann-windowed sinc low-pass (cutoff just under
 * the lower Nyquist). Deterministic and plenty for speech.
 */
export function resamplePcm16(input: Buffer, from: number, to: number): Buffer {
  if (from === to) return input
  const source = new Int16Array(input.buffer, input.byteOffset, Math.floor(input.length / 2))
  const ratio = from / to
  const cutoff = 0.95 * Math.min(1, to / from)
  const half = 24
  const output = Buffer.alloc(Math.floor(source.length / ratio) * 2)
  for (let index = 0; index < output.length / 2; index += 1) {
    const center = index * ratio
    const base = Math.floor(center)
    let sum = 0
    let weights = 0
    for (let tap = base - half + 1; tap <= base + half; tap += 1) {
      const distance = center - tap
      const x = Math.PI * cutoff * distance
      const sinc = distance === 0 ? 1 : Math.sin(x) / x
      const window = 0.5 + 0.5 * Math.cos(Math.PI * distance / half)
      const weight = sinc * window
      weights += weight
      if (tap >= 0 && tap < source.length) sum += source[tap] * weight
    }
    output.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(sum / weights))), index * 2)
  }
  return output
}

async function synthesize(tokens: ApplicationDefaultCredentials, language: string, phrase: string): Promise<Buffer> {
  const [token, project] = await Promise.all([tokens.accessToken(), tokens.projectId()])
  const host = LOCATION === 'global' ? 'aiplatform.googleapis.com' : `${LOCATION}-aiplatform.googleapis.com`
  const url = `https://${host}/v1/projects/${project}/locations/${LOCATION}/publishers/google/models/${encodeURIComponent(MODEL)}:generateContent`
  const response = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      contents: [{ role: 'user', parts: [{ text: `Say this clearly at a natural pace, exactly as written: ${phrase}` }] }],
      generationConfig: {
        responseModalities: ['AUDIO'],
        speechConfig: { languageCode: language, voiceConfig: { prebuiltVoiceConfig: { voiceName: VOICE } } }
      }
    }),
    signal: AbortSignal.timeout(60_000)
  })
  if (!response.ok) {
    // Status only: the body can echo request details.
    throw new Error(`Vertex TTS ${MODEL} returned HTTP ${response.status}`)
  }
  const value = await response.json() as { candidates?: { content?: { parts?: { inlineData?: { mimeType?: string; data?: string } }[] } }[] }
  const inline = value.candidates?.[0]?.content?.parts?.find((part) => part.inlineData?.data)?.inlineData
  if (!inline?.data || !inline.mimeType) throw new Error(`Vertex TTS ${MODEL} returned no audio`)
  const pcm = resamplePcm16(Buffer.from(inline.data, 'base64'), rateOf(inline.mimeType), LIVE_AUDIO_RATE)
  return Buffer.concat([Buffer.alloc(Math.round(LIVE_AUDIO_RATE * LEAD_SILENCE_MS / 1000) * 2), pcm])
}

async function main(): Promise<void> {
  const tokens = new ApplicationDefaultCredentials()
  mkdirSync(OUT, { recursive: true })
  const manifest: Record<string, unknown>[] = []
  for (const fixture of LIVE_AUDIO_FIXTURES) {
    const wav = writeWav(await synthesize(tokens, fixture.language, fixture.phrase))
    writeFileSync(resolve(OUT, `${fixture.name}.wav`), wav)
    // Read back through the same validator the live test uses.
    const seconds = loadLiveAudio(OUT, fixture.name).length / 2 / LIVE_AUDIO_RATE
    manifest.push({ ...fixture, file: `${fixture.name}.wav`, seconds: Number(seconds.toFixed(2)), sha256: createHash('sha256').update(wav).digest('hex') })
    console.log(`${fixture.name}.wav  ${seconds.toFixed(2)} s  ${fixture.language}`)
  }
  writeFileSync(resolve(OUT, 'manifest.json'), JSON.stringify({
    generatedAt: new Date().toISOString(),
    source: `Vertex AI ${MODEL}, voice ${VOICE}, ${LOCATION}`,
    format: 'PCM WAV, 16000 Hz, mono, signed 16-bit little-endian',
    fixtures: manifest
  }, null, 2))
  console.log(`\nWrote ${manifest.length} fixtures to ${OUT}\n$env:LUMI_LIVE_AUDIO_DIR = '${OUT}'`)
}

main().catch((error: unknown) => {
  console.error(`make-test-audio: ${error instanceof Error ? error.message : String(error)}`)
  process.exit(1)
})
