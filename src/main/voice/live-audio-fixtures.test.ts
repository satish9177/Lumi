import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { LIVE_AUDIO_FIXTURES, LiveAudioFixtureError, pcmFromWav, requireLiveAudio, writeWav } from './live-audio-fixtures'

const samples = Buffer.from([1, 0, 2, 0, 3, 0, 4, 0])

describe('live audio fixtures', () => {
  let directory: string | undefined
  afterEach(() => { if (directory) rmSync(directory, { recursive: true, force: true }) })

  it('round-trips 16 kHz mono PCM16 WAV to raw samples, skipping extra chunks', () => {
    const wav = writeWav(samples)
    expect(pcmFromWav(wav)).toEqual(samples)
    // A LIST chunk (odd size, padded) between fmt and data, as many encoders write.
    const list = Buffer.concat([Buffer.from('LIST'), Buffer.from([3, 0, 0, 0]), Buffer.from('abc'), Buffer.alloc(1)])
    const withList = Buffer.concat([wav.subarray(0, 36), list, wav.subarray(36)])
    expect(pcmFromWav(withList)).toEqual(samples)
  })

  it('refuses malformed or wrong-format WAV files', () => {
    const wav = writeWav(samples)
    expect(() => pcmFromWav(Buffer.from('not a wav at all'))).toThrow(/RIFF\/WAVE/)
    expect(() => pcmFromWav(wav.subarray(0, wav.length - 2))).toThrow(/truncated "data"/)
    expect(() => pcmFromWav(wav.subarray(0, 36))).toThrow(/no data chunk/)
    const stereo = Buffer.from(wav); stereo.writeUInt16LE(2, 22)
    expect(() => pcmFromWav(stereo)).toThrow(/expected PCM \(1\) mono 16000 Hz 16-bit/)
    const rate = Buffer.from(wav); rate.writeUInt32LE(24_000, 24)
    expect(() => pcmFromWav(rate)).toThrow(LiveAudioFixtureError)
    // "data" inside another chunk must not be mistaken for the data chunk.
    const decoy = Buffer.concat([wav.subarray(0, 12), Buffer.from('JUNK'), Buffer.from([4, 0, 0, 0]), Buffer.from('data'), wav.subarray(12, 36)])
    expect(() => pcmFromWav(decoy)).toThrow(/no data chunk/)
  })

  it('explains how to generate fixtures when the directory or a file is missing', () => {
    expect(() => requireLiveAudio('')).toThrow(/LUMI_LIVE_AUDIO_DIR is not set.*npm run live:audio/)
    expect(() => requireLiveAudio(join(tmpdir(), 'lumi-no-such-dir'))).toThrow(/does not exist.*npm run live:audio/)
    directory = mkdtempSync(join(tmpdir(), 'lumi-live-audio-'))
    writeFileSync(join(directory, 'en_compound.wav'), writeWav(samples))
    expect(() => requireLiveAudio(directory)).toThrow(/en_stop\.pcm or en_stop\.wav is missing/)
    for (const { name } of LIVE_AUDIO_FIXTURES) writeFileSync(join(directory, `${name}.pcm`), samples)
    writeFileSync(join(directory, 'te_pure.pcm'), Buffer.alloc(3))
    expect(() => requireLiveAudio(directory)).toThrow(/te_pure\.pcm is empty or not 16-bit PCM/)
    writeFileSync(join(directory, 'te_pure.pcm'), samples)
    expect(Object.keys(requireLiveAudio(directory))).toEqual(['en_compound', 'en_stop', 'en_in', 'te_mixed', 'te_pure'])
  })
})
