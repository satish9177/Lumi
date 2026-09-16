import { LAPTOP_MIC_CONSTRAINTS } from './openai-realtime-provider'

/**
 * Raw PCM audio for providers that stream audio frames themselves (Gemini
 * Live): 16 kHz mono PCM16 up, 24 kHz mono PCM16 down.
 *
 * Capture uses a ScriptProcessorNode rather than an AudioWorklet: it needs no
 * extra module and no CSP relaxation, and 16 kHz mono is light enough for the
 * main thread. Playback schedules buffers back to back and can be cut off at
 * once when the user interrupts.
 */

export interface AudioIO {
  start(onChunk: (base64: string) => void): Promise<void>
  setCapturing(enabled: boolean): void
  play(base64: string): void
  stopPlayback(): void
  close(): void
}

const INPUT_RATE = 16_000
const OUTPUT_RATE = 24_000
const FRAME_SIZE = 2_048

export function floatToPcm16Base64(samples: Float32Array): string {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true)
  }
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  }
  return btoa(binary)
}

export function pcm16Base64ToFloat(base64: string): Float32Array {
  const binary = atob(base64)
  const samples = new Float32Array(Math.floor(binary.length / 2))
  for (let index = 0; index < samples.length; index += 1) {
    const low = binary.charCodeAt(index * 2)
    const high = binary.charCodeAt(index * 2 + 1)
    let value = (high << 8) | low
    if (value >= 0x8000) value -= 0x10000
    samples[index] = value / 0x8000
  }
  return samples
}

export class BrowserPcmAudio implements AudioIO {
  private stream?: MediaStream
  private inputContext?: AudioContext
  private processor?: ScriptProcessorNode
  private outputContext?: AudioContext
  private nextStart = 0
  private readonly playing = new Set<AudioBufferSourceNode>()
  private capturing = true

  async start(onChunk: (base64: string) => void): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia(LAPTOP_MIC_CONSTRAINTS)
    // Chromium resamples the microphone to the context rate.
    this.inputContext = new AudioContext({ sampleRate: INPUT_RATE })
    const source = this.inputContext.createMediaStreamSource(this.stream)
    this.processor = this.inputContext.createScriptProcessor(FRAME_SIZE, 1, 1)
    this.processor.onaudioprocess = (event) => {
      if (!this.capturing) return
      onChunk(floatToPcm16Base64(event.inputBuffer.getChannelData(0)))
    }
    source.connect(this.processor)
    // A ScriptProcessor only runs while connected; its output stays silent.
    this.processor.connect(this.inputContext.destination)
  }

  setCapturing(enabled: boolean): void {
    this.capturing = enabled
    this.stream?.getAudioTracks().forEach((track) => { track.enabled = enabled })
  }

  play(base64: string): void {
    this.outputContext ??= new AudioContext({ sampleRate: OUTPUT_RATE })
    const context = this.outputContext
    const samples = pcm16Base64ToFloat(base64)
    if (samples.length === 0) return
    const buffer = context.createBuffer(1, samples.length, OUTPUT_RATE)
    buffer.getChannelData(0).set(samples)
    const node = context.createBufferSource()
    node.buffer = buffer
    node.connect(context.destination)
    const startAt = Math.max(context.currentTime, this.nextStart)
    node.start(startAt)
    this.nextStart = startAt + buffer.duration
    this.playing.add(node)
    node.onended = () => this.playing.delete(node)
  }

  stopPlayback(): void {
    for (const node of this.playing) {
      try { node.stop() } catch { /* already stopped */ }
    }
    this.playing.clear()
    this.nextStart = 0
  }

  close(): void {
    this.stopPlayback()
    this.processor?.disconnect()
    this.stream?.getTracks().forEach((track) => track.stop())
    void this.inputContext?.close().catch(() => undefined)
    void this.outputContext?.close().catch(() => undefined)
    this.processor = undefined
    this.stream = undefined
    this.inputContext = undefined
    this.outputContext = undefined
  }
}

/** No microphone and no speaker: the scripted acceptance harness. */
export class SilentAudio implements AudioIO {
  played = 0
  async start(): Promise<void> {}
  setCapturing(): void {}
  play(): void { this.played += 1 }
  stopPlayback(): void {}
  close(): void {}
}
