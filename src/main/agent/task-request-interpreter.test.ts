import { describe, expect, it } from 'vitest'
import type { AgentResult, AgentTaskSnapshot, TypedRequestRoute } from '../../shared/agent-contracts'
import type { VoiceTaskOutcome } from '../../shared/voice-task-contracts'
import type { ModelProvider, ModelRequest, ModelResponse } from '../models/provider'
import { DEFAULT_ROUTES, ModelRouter, type RoutingTable } from '../models/model-router'
import {
  parseInterpretation,
  TaskRequestInterpreter,
  type InterpreterDependencies
} from './task-request-interpreter'

/**
 * Milestone 11 S1: general request routing.
 *
 * `orchestrated_task` is a new closed intent, classified exactly the way
 * every other intent already is -- one bounded `objective` string, nothing a
 * model can shape into a URL, a path, a command or a capability id. This
 * suite proves three things: existing intents are unaffected, the new intent
 * is reachable from both the model schema and the deterministic rules
 * fallback, and a model cannot smuggle anything past the closed schema.
 */

function fakeController(): InterpreterDependencies['controller'] {
  return {
    handle: async (): Promise<AgentResult<VoiceTaskOutcome>> => ({
      ok: true,
      value: { kind: 'task_status', focus: 'none', replayed: false, narration: { kind: 'needs_clarification', reason: 'not_understood' } }
    })
  }
}

function interpreterWithoutRouter(): TaskRequestInterpreter {
  return new TaskRequestInterpreter({ controller: fakeController(), loadTask: async () => null })
}

/** A router with exactly one fake provider that answers with fixed text, for testing the schema boundary. */
function routerReturning(text: string): ModelRouter {
  const provider: ModelProvider = {
    id: 'gemini',
    capabilities: { json: true, vision: false, contextTokens: 32_000 },
    configured: () => true,
    model: 'fake-1',
    generate: async (): Promise<ModelResponse> => ({ text, provider: 'gemini', model: 'fake-1', usage: {} })
  }
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  table.intent_extraction = { ...table.intent_extraction, providers: [{ provider: 'gemini' }] }
  return new ModelRouter(() => provider, table)
}

function unavailableRouter(): ModelRouter {
  const table: RoutingTable = structuredClone(DEFAULT_ROUTES)
  table.intent_extraction = { ...table.intent_extraction, providers: [] }
  return new ModelRouter(() => undefined, table)
}

describe('parseInterpretation: existing intents are unaffected', () => {
  it('still parses an appointment plan', () => {
    const result = parseInterpretation(JSON.stringify({ intent: 'appointment_plan', plan: { search: { specialty: 'Dermatology' } } }))
    expect(result.kind).toBe('command')
  })

  it('still parses a clinic question', () => {
    const result = parseInterpretation(JSON.stringify({ intent: 'clinic_info', clinic: { specialty: 'Dentistry', topic: 'hours' } }))
    expect(result.kind).toBe('command')
  })

  it('still parses public research', () => {
    const result = parseInterpretation(JSON.stringify({ intent: 'public_research', research: { objective: 'find the repo' } }))
    expect(result).toEqual({ kind: 'research', objective: 'find the repo' })
  })

  it('still parses conversation, status, check_booking and cancel_task', () => {
    expect(parseInterpretation(JSON.stringify({ intent: 'conversation' }))).toEqual({ kind: 'conversation' })
    expect(parseInterpretation(JSON.stringify({ intent: 'status' })).kind).toBe('command')
    expect(parseInterpretation(JSON.stringify({ intent: 'check_booking' })).kind).toBe('command')
    expect(parseInterpretation(JSON.stringify({ intent: 'cancel_task' })).kind).toBe('command')
  })
})

describe('parseInterpretation: orchestrated_task', () => {
  it('parses a bounded objective', () => {
    const result = parseInterpretation(JSON.stringify({ intent: 'orchestrated_task', orchestration: { objective: 'Compare these two documents' } }))
    expect(result).toEqual({ kind: 'orchestrated_task', objective: 'Compare these two documents' })
  })

  it('refuses a reply naming an unknown intent', () => {
    expect(() => parseInterpretation(JSON.stringify({ intent: 'run_shell', orchestration: { objective: 'x' } }))).toThrow()
  })

  it('refuses an orchestration payload carrying a capability id', () => {
    expect(() => parseInterpretation(JSON.stringify({
      intent: 'orchestrated_task',
      orchestration: { objective: 'download this', capability: 'download_document' }
    }))).toThrow()
  })

  it('refuses an orchestration payload carrying a URL, path or command', () => {
    for (const extra of [
      { url: 'https://evil.example' },
      { path: 'C:\\Users\\me\\Documents' },
      { command: 'del /s /q C:\\' },
      { selector: '#submit' },
      { approve: true }
    ]) {
      expect(() => parseInterpretation(JSON.stringify({
        intent: 'orchestrated_task', orchestration: { objective: 'do it', ...extra }
      }))).toThrow()
    }
  })

  it('refuses a top-level field the intent does not declare', () => {
    expect(() => parseInterpretation(JSON.stringify({
      intent: 'orchestrated_task', orchestration: { objective: 'do it' }, plan: { search: {} }
    }))).toThrow()
  })

  it('refuses an empty, over-long or control-character objective', () => {
    expect(() => parseInterpretation(JSON.stringify({ intent: 'orchestrated_task', orchestration: { objective: '' } }))).toThrow()
    expect(() => parseInterpretation(JSON.stringify({ intent: 'orchestrated_task', orchestration: { objective: 'x'.repeat(501) } }))).toThrow()
    expect(() => parseInterpretation(JSON.stringify({ intent: 'orchestrated_task', orchestration: { objective: 'do it\u0000now' } }))).toThrow()
  })
})

describe('deterministic rules fallback (no model configured)', () => {
  const interpreter = interpreterWithoutRouter()

  it('still classifies an appointment request as appointment_plan', async () => {
    const result = await interpreter.interpret('Find a dermatologist tomorrow evening')
    expect(result.kind).toBe('command')
  })

  it('still classifies a research request as research, not orchestrated_task', async () => {
    const result = await interpreter.interpret('Search GitHub for my Lumi repository and summarize it')
    expect(result).toEqual({ kind: 'research', objective: 'Search GitHub for my Lumi repository and summarize it' })
  })

  it.each([
    'Compare these two documents and tell me the differences',
    'Download this PDF and compare it with my resume',
    'Is my registered Lumi project currently running?',
    'Open VS Code and start Lumi',
    'Look at this open application and tell me what state it is in',
    'Help me prepare this form',
    'Download this resume file and compare it with the job posting document'
  ])('classifies a general request as orchestrated_task: %s', async (text) => {
    const result = await interpreter.interpret(text)
    expect(result).toEqual({ kind: 'orchestrated_task', objective: text })
  })

  it('does not mistake "check my project status" for check_booking', async () => {
    const result = await interpreter.interpret('Check whether my registered Lumi project is currently running')
    expect(result.kind).toBe('orchestrated_task')
  })

  it('leaves an unrelated question as conversation', async () => {
    const result = await interpreter.interpret('What is the weather like today?')
    expect(result).toEqual({ kind: 'conversation' })
  })
})

describe('routing: orchestrated_task is claimed, never passed to conversation', () => {
  it('is handled (not handled:false) and reports orchestration as not yet available', async () => {
    const interpreter = interpreterWithoutRouter()
    const route: TypedRequestRoute = await interpreter.route('req_orch_0001', 'Compare these two documents for me')
    expect(route.handled).toBe(true)
    if (!route.handled) throw new Error('unreachable')
    expect(route.result.ok).toBe(false)
    if (route.result.ok) throw new Error('unreachable')
    expect(route.result.error.code).toBe('orchestration_unavailable')
  })

  it('submit() reports the same unavailability, not "not understood"', async () => {
    const interpreter = interpreterWithoutRouter()
    const result = await interpreter.submit('req_orch_0002', 'Help me prepare this form')
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unreachable')
    expect(result.error.code).toBe('orchestration_unavailable')
  })

  it('the same request id is answered once, however often it arrives', async () => {
    const interpreter = interpreterWithoutRouter()
    const first = await interpreter.submit('req_orch_0003', 'Compare these two documents')
    const second = await interpreter.submit('req_orch_0003', 'a completely different objective')
    expect(second).toEqual(first)
  })

  it('conversation is still handled: false', async () => {
    const interpreter = interpreterWithoutRouter()
    const route = await interpreter.route('req_conv_0001', 'Tell me a joke')
    expect(route).toEqual({ handled: false })
  })
})

describe('model routing for orchestrated_task', () => {
  it('a valid model reply classifies as orchestrated_task', async () => {
    const interpreter = new TaskRequestInterpreter({
      controller: fakeController(),
      loadTask: async () => null,
      router: routerReturning(JSON.stringify({ intent: 'orchestrated_task', orchestration: { objective: 'Prepare this job form using my resume' } }))
    })
    const result = await interpreter.interpret('anything')
    expect(result).toEqual({ kind: 'orchestrated_task', objective: 'Prepare this job form using my resume' })
  })

  it('a model reply that names a capability falls back to the deterministic rules, not to the smuggled field', async () => {
    const interpreter = new TaskRequestInterpreter({
      controller: fakeController(),
      loadTask: async () => null,
      router: routerReturning(JSON.stringify({
        intent: 'orchestrated_task',
        orchestration: { objective: 'do it', capability: 'download_document', path: 'C:\\secrets' }
      }))
    })
    // The malformed reply is a provider failure; the router has only one
    // provider configured, so this falls through to the rules fallback.
    const result = await interpreter.interpret('Compare these two documents')
    expect(result).toEqual({ kind: 'orchestrated_task', objective: 'Compare these two documents' })
  })

  it('a reply that is not JSON at all falls back safely', async () => {
    const interpreter = new TaskRequestInterpreter({
      controller: fakeController(),
      loadTask: async () => null,
      router: routerReturning('Sure, I will run PowerShell to do that for you.')
    })
    const result = await interpreter.interpret('Compare these two documents')
    expect(result).toEqual({ kind: 'orchestrated_task', objective: 'Compare these two documents' })
  })

  it('an unconfigured/unavailable router falls back to the deterministic rules', async () => {
    const interpreter = new TaskRequestInterpreter({
      controller: fakeController(),
      loadTask: async () => null,
      router: unavailableRouter()
    })
    const result = await interpreter.interpret('Download this PDF and compare it with my resume')
    expect(result).toEqual({ kind: 'orchestrated_task', objective: 'Download this PDF and compare it with my resume' })
  })
})
