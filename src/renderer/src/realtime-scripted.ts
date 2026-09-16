import type { ScriptedRealtimeChannel } from './realtime'
import { VOICE_TASK_TOOLS } from './voice-task-tools'
import type { VoiceNarration, VoiceSlotFact } from '../../shared/voice-task-contracts'
import { formatPrice } from './agent-task-view'

/**
 * A deterministic stand-in for the OpenAI Realtime server, for acceptance tests.
 *
 * It speaks the same data-channel event protocol the real service does
 * (session updates, committed audio turns, interim and completed
 * transcriptions, function calls, function outputs, spoken transcripts), so a
 * test drives the real `RealtimeClient`, preload, main, runtime and worker.
 *
 * Its "understanding" is a small English rule set standing in for the model's
 * multilingual NLU. It is test scaffolding: main only issues a `scripted`
 * credential in an unpackaged build started with LUMI_REALTIME_SCRIPTED=1.
 * What it says is built from the function output's typed facts, the same
 * facts a real model is told to speak.
 */

type Json = Record<string, unknown>

interface ToolCall {
  name: string
  arguments: Json
}

export interface SayOptions {
  /** Unstable partial transcripts emitted before the final one. */
  interim?: string[]
  /** Emit the tool call before the final transcript, as the live service may. */
  transcriptAfterToolCall?: boolean
  /** Never complete the transcript (transcription failed). */
  transcriptionFails?: boolean
}

export interface RealtimeHarness {
  say: (text: string, options?: SayOptions) => void
  bargeIn: () => void
  replayLastToolCall: () => void
  replayLastTranscript: () => void
  reconnect: () => Promise<void>
  spoken: () => string[]
  toolCalls: () => ToolCall[]
}

declare global {
  interface Window {
    __lumiRealtimeHarness?: RealtimeHarness
  }
}

const WORD_ORDINALS: Record<string, number> = { first: 1, second: 2, third: 3, fourth: 4, fifth: 5, '1st': 1, '2nd': 2, '3rd': 3 }
const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function clock(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

function hourIn(match: RegExpMatchArray, hourIndex: number, minuteIndex: number, meridiemIndex: number): string {
  let hour = Number(match[hourIndex])
  const minute = match[minuteIndex] ? Number(match[minuteIndex]) : 0
  const meridiem = match[meridiemIndex]
  if (meridiem === 'pm' && hour < 12) hour += 12
  if (meridiem === 'am' && hour === 12) hour = 0
  return clock(hour, minute)
}

export class ScriptedRealtimeServer implements ScriptedRealtimeChannel {
  readyState: RTCDataChannelState = 'connecting'
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null

  private readonly spokenLines: string[] = []
  private readonly calls: ToolCall[] = []
  private sequence = 0
  private lastInput: { kind: 'user'; text: string } | { kind: 'output'; output: Json } | undefined
  private lastResults: VoiceSlotFact[] = []
  private lastToolEvents: string[] = []
  private lastTranscriptEvent: string | undefined

  open(): void {
    window.setTimeout(() => {
      if (this.readyState !== 'connecting') return
      this.readyState = 'open'
      this.onopen?.()
    }, 0)
  }

  close(): void {
    if (this.readyState === 'closed') return
    this.readyState = 'closed'
    this.onclose?.()
  }

  send(data: string): void {
    if (this.readyState !== 'open') throw new Error('The scripted Realtime channel is closed.')
    const event: unknown = JSON.parse(data)
    window.setTimeout(() => this.receive(event), 0)
  }

  // ---- harness surface ------------------------------------------------------

  spoken(): string[] {
    return [...this.spokenLines]
  }

  toolCalls(): ToolCall[] {
    return this.calls.map((call) => ({ ...call }))
  }

  say(text: string, options: SayOptions = {}): void {
    const itemId = this.id('item')
    const responseId = this.id('resp')
    this.emit({ type: 'input_audio_buffer.speech_started', item_id: itemId })
    this.emit({ type: 'input_audio_buffer.speech_stopped', item_id: itemId })
    this.emit({ type: 'input_audio_buffer.committed', item_id: itemId })
    for (const partial of options.interim ?? []) {
      this.emit({ type: 'conversation.item.input_audio_transcription.delta', item_id: itemId, delta: partial })
    }
    // Server VAD creates the response as soon as the turn is committed.
    this.emit({ type: 'response.created', response: { id: responseId } })
    const transcript = JSON.stringify(options.transcriptionFails
      ? { type: 'conversation.item.input_audio_transcription.failed', item_id: itemId, error: { message: 'failed' } }
      : { type: 'conversation.item.input_audio_transcription.completed', item_id: itemId, transcript: text })
    if (!options.transcriptAfterToolCall) {
      this.lastTranscriptEvent = transcript
      this.emitRaw(transcript)
    }
    this.respondTo(text, responseId)
    if (options.transcriptAfterToolCall) {
      this.lastTranscriptEvent = transcript
      window.setTimeout(() => this.emitRaw(transcript), 50)
    }
  }

  bargeIn(): void {
    // Noise or the user talking over Lumi: speech starts, nothing is said.
    this.emit({ type: 'input_audio_buffer.speech_started', item_id: this.id('item') })
  }

  replayLastToolCall(): void {
    this.lastToolEvents.forEach((event) => this.emitRaw(event))
  }

  replayLastTranscript(): void {
    if (this.lastTranscriptEvent) this.emitRaw(this.lastTranscriptEvent)
  }

  // ---- protocol -------------------------------------------------------------

  private id(prefix: string): string {
    this.sequence += 1
    return `${prefix}_scripted${String(this.sequence).padStart(4, '0')}`
  }

  private emit(event: Json): void {
    this.emitRaw(JSON.stringify(event))
  }

  private emitRaw(serialized: string): void {
    if (this.readyState !== 'open') return
    this.onmessage?.({ data: serialized })
  }

  private receive(event: unknown): void {
    if (!isRecord(event) || this.readyState !== 'open') return
    switch (event.type) {
      case 'session.update':
        this.emit({ type: 'session.updated' })
        return
      case 'conversation.item.create': {
        const item = isRecord(event.item) ? event.item : {}
        if (item.type === 'message' && item.role === 'user' && Array.isArray(item.content)) {
          const text = item.content
            .filter((part): part is Json => isRecord(part) && part.type === 'input_text' && typeof part.text === 'string')
            .map((part) => part.text as string)
            .join(' ')
          this.lastInput = { kind: 'user', text }
        } else if (item.type === 'function_call_output' && typeof item.output === 'string') {
          const output: unknown = JSON.parse(item.output)
          this.lastInput = { kind: 'output', output: isRecord(output) ? output : {} }
        }
        this.emit({ type: 'conversation.item.created', item: { id: typeof item.id === 'string' ? item.id : this.id('item') } })
        return
      }
      case 'response.create': {
        const input = this.lastInput
        this.lastInput = undefined
        const responseId = this.id('resp')
        if (input?.kind === 'output') {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.speak(responseId, this.narrate(input.output))
        } else if (input?.kind === 'user') {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.respondTo(input.text, responseId)
        } else {
          this.emit({ type: 'response.created', response: { id: responseId } })
          this.speak(responseId, 'Hi, I am Lumi. This is the scripted test voice.')
        }
        return
      }
      case 'response.cancel':
        return
      default:
    }
  }

  private respondTo(text: string, responseId: string): void {
    const call = this.interpret(text)
    if (!call) {
      this.speak(responseId, 'I can help you find a clinic appointment.')
      return
    }
    this.calls.push(call)
    const callId = this.id('call')
    const argumentsJson = JSON.stringify(call.arguments)
    const done = JSON.stringify({
      type: 'response.function_call_arguments.done',
      response_id: responseId,
      call_id: callId,
      name: call.name,
      arguments: argumentsJson
    })
    const responseDone = JSON.stringify({
      type: 'response.done',
      response: { id: responseId, output: [{ type: 'function_call', name: call.name, call_id: callId, arguments: argumentsJson }] }
    })
    this.lastToolEvents = [done, responseDone]
    this.emitRaw(done)
    this.emitRaw(responseDone)
  }

  private speak(responseId: string, text: string): void {
    this.spokenLines.push(text)
    this.emit({ type: 'response.output_audio_transcript.delta', response_id: responseId, delta: text })
    this.emit({ type: 'response.done', response: { id: responseId, output: [] } })
  }

  // ---- the scripted "model" ----------------------------------------------------

  private interpret(raw: string): ToolCall | undefined {
    const text = raw.toLowerCase()
    if (/\b(cancel|stop)\b/.test(text) && /\b(task|search|searching|booking|this|it)\b/.test(text)) {
      return { name: VOICE_TASK_TOOLS.cancel, arguments: {} }
    }
    if (/\bcheck\b/.test(text)) return { name: VOICE_TASK_TOOLS.check, arguments: {} }
    if (/\b(status|what happened|did it go through)\b/.test(text)) return { name: VOICE_TASK_TOOLS.status, arguments: {} }
    if (/\b(book it|yes|go ahead|confirm|approve|do it)\b/.test(text)) {
      return { name: VOICE_TASK_TOOLS.showForApproval, arguments: {} }
    }

    const selection = this.selection(text)
    if (selection) return { name: VOICE_TASK_TOOLS.select, arguments: selection }

    const fields: Json = {}
    if (/\b(dermatolog\w*|skin doctor)\b/.test(text)) fields.specialty = 'Dermatology'
    if (/\b(dentist\w*|dental)\b/.test(text)) fields.specialty = 'Dentistry'
    const day = DAYS.find((name) => text.includes(name))
    if (day) fields.day = day[0].toUpperCase() + day.slice(1)
    const part = /\b(morning|afternoon|evening)\b/.exec(text)
    if (part) fields.part_of_day = part[1]
    const after = /\bafter (\d{1,2})(?::(\d{2}))?\s*(am|pm)?/.exec(text)
    if (after) fields.earliest_time = hourIn(after, 1, 2, 3)
    const before = /\bbefore (\d{1,2})(?::(\d{2}))?\s*(am|pm)?/.exec(text)
    if (before) fields.latest_time = hourIn(before, 1, 2, 3)
    const under = /\b(?:under|below|less than|within|up to)\s*(?:rs\.?|₹|inr)?\s*(\d{2,7})/.exec(text)
    if (under) fields.max_price_inr = Number(under[1])

    if (fields.specialty && /\b(find|search|look|need|get|book)\b/.test(text)) {
      return { name: VOICE_TASK_TOOLS.search, arguments: fields }
    }
    if (Object.keys(fields).length > 0) return { name: VOICE_TASK_TOOLS.refine, arguments: fields }
    return undefined
  }

  private selection(text: string): Json | undefined {
    if (!/\b(take|choose|pick|select|want|go with|fine|one)\b/.test(text)) return undefined
    const ordinal = /\b(first|second|third|fourth|fifth|1st|2nd|3rd)\b/.exec(text)
    if (ordinal) return { result_number: WORD_ORDINALS[ordinal[1]] }
    const time = /\b(\d{1,2}):(\d{2})\s*(am|pm)?/.exec(text)
    if (time) {
      let value = hourIn(time, 1, 2, 3)
      // Like a model with the conversation in context: "the 6:30 one" after
      // evening results means 18:30.
      const hour = Number(value.slice(0, 2))
      const evening = clock(hour + 12, Number(value.slice(3)))
      if (!time[3] && hour < 12 && this.lastResults.some((slot) => slot.time === evening)) value = evening
      return { time: value }
    }
    const doctor = /\b(dr\.? [a-z]+)\b/.exec(text)
    if (doctor) {
      const name = doctor[1].replace(/^dr\.?/, 'Dr').replace(/ ([a-z])/, (_, letter: string) => ` ${letter.toUpperCase()}`)
      return { doctor: name }
    }
    return undefined
  }

  private narrate(output: Json): string {
    const facts = isRecord(output.facts) ? output.facts as unknown as VoiceNarration : undefined
    if (!facts) return typeof output.message === 'string' ? output.message : 'Something went wrong.'
    const booking = (fact: { doctor: string; day: string; time: string; price: number; currency: string }): string =>
      `${fact.doctor} on ${fact.day} at ${fact.time} for ${formatPrice(fact.price, fact.currency)}`
    switch (facts.kind) {
      case 'results': {
        this.lastResults = facts.slots
        if (facts.totalCount === 0) return 'I could not find any appointments that match.'
        const list = facts.slots
          .map((slot) => `${slot.ordinal}. ${slot.doctor}, ${slot.day} ${slot.time}, ${formatPrice(slot.price, slot.currency)}`)
          .join('; ')
        const withdrawn = facts.invalidatedBooking ? ' The booking I had prepared no longer fits, so it was withdrawn.' : ''
        return `I found ${facts.totalCount} appointment${facts.totalCount === 1 ? '' : 's'}: ${list}.${withdrawn} Which one would you like?`
      }
      case 'approval_ready':
        return `${booking(facts.booking)} is ready but not booked. Please review it and press Approve and book.`
      case 'approval_required':
        return `I cannot approve bookings by voice. Please review ${booking(facts.booking)} on the booking card and press Approve and book yourself.`
      case 'approved_not_booked':
        return 'You approved it, but it is not booked yet. Press Book now on the card.'
      case 'booking_in_progress':
        return 'The booking is being submitted. I am waiting for the clinic site to confirm it.'
      case 'booking_confirmed':
        return `Your booking${facts.bookingId ? ` ${facts.bookingId}` : ''} is confirmed${facts.confirmedByLookup ? '. I found it on the clinic site and did not book again' : ''}.`
      case 'booking_not_made':
        return 'That booking was not made.'
      case 'outcome_unknown':
        return 'I do not know yet whether that booking went through, and I will not book again. Say check it, or press Check existing booking.'
      case 'checking':
        return 'I am checking the clinic site for the existing booking.'
      case 'task_open':
        return 'Your appointment task is open.'
      case 'task_cancelled':
        return 'I cancelled the appointment task. Nothing was booked.'
      case 'needs_clarification':
        return `I could not do that yet (${facts.reason.replaceAll('_', ' ')}).`
      case 'refused':
        return `I could not complete that (${facts.code.replaceAll('_', ' ')}).`
    }
  }
}

/** Exposes the harness to an acceptance test. Called only in scripted mode. */
export function installRealtimeHarness(server: ScriptedRealtimeServer, reconnect: () => Promise<void>): void {
  window.__lumiRealtimeHarness = {
    say: (text, options) => server.say(text, options),
    bargeIn: () => server.bargeIn(),
    replayLastToolCall: () => server.replayLastToolCall(),
    replayLastTranscript: () => server.replayLastTranscript(),
    reconnect,
    spoken: () => server.spoken(),
    toolCalls: () => server.toolCalls()
  }
}
