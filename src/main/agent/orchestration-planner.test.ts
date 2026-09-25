import { describe, expect, it } from 'vitest'
import {
  ORCHESTRATION_PLANNER_RULES,
  ORCHESTRATION_PLANNER_SCHEMA,
  OrchestrationPlanError,
  OrchestrationPlanner,
  orchestrationResultLines,
  orchestrationStateLines,
  parseOrchestrationDecision,
  type OrchestrationCapabilities
} from './orchestration-planner'
import { AGENT_CAPABILITY_IDS } from '../../shared/agent-capabilities'
import { ModelRouter } from '../models/model-router'
import type { ModelProvider, ModelRequest, ModelResponse } from '../models/provider'

/**
 * Milestone 11 S2: what the orchestration planner is *able to say*.
 *
 * The planner writes primitives into a flat object of closed enumerations, and this module constructs the
 * decision from them. These tests are the proof that there is no field left over: no path, no URL, no
 * command, no approval, no capability id outside the closed catalog, and no capability id outside the
 * orchestration's own currently-available set even when it is a real catalog id.
 */

const TWO: OrchestrationCapabilities = { available: ['public_research', 'project_status'], availableResources: [] }
const WITH_RESOURCES: OrchestrationCapabilities = { available: ['public_research', 'project_status'], availableResources: ['r1', 'r2'] }
const WITH_DESKTOP_SAFE_ACTION: OrchestrationCapabilities = { available: ['desktop_safe_action'], availableResources: ['r1'] }

describe('parseOrchestrationDecision', () => {
  it('parses a step choosing an available capability', () => {
    const result = parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', reason: 'the objective needs research' }), TWO
    )
    expect(result).toEqual({ kind: 'step', capability: 'public_research', resources: [], reason: 'the objective needs research' })
  })

  it('parses finish and stop', () => {
    expect(parseOrchestrationDecision(JSON.stringify({ action: 'finish', reason: 'done' }), TWO))
      .toEqual({ kind: 'finish', reason: 'done' })
    expect(parseOrchestrationDecision(JSON.stringify({ action: 'stop', reason: 'stuck' }), TWO))
      .toEqual({ kind: 'stop', reason: 'stuck' })
  })

  it('defaults a missing or invalid reason to a fixed placeholder, never throwing on that alone', () => {
    expect(parseOrchestrationDecision(JSON.stringify({ action: 'finish' }), TWO).reason).toBe('no reason given')
  })

  it('refuses a capability id outside the closed catalog entirely', () => {
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'run_shell' }), TWO
    )).toThrow(OrchestrationPlanError)
  })

  it('refuses a REAL catalog id that is not in this orchestration\u2019s currently-available set', () => {
    // project_start is a real Milestone 11 S1 catalog id, just not offered to this orchestration right now.
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'project_start' }), TWO
    )).toThrow(OrchestrationPlanError)
  })

  it('refuses any field outside the closed set -- a path, a URL, a command, an approval', () => {
    for (const extra of [
      { url: 'https://evil.example' },
      { path: 'C:\\Users\\me' },
      { command: 'rm -rf /' },
      { approve: true },
      { task_id: '00000000-0000-4000-8000-000000000001' }
    ]) {
      expect(() => parseOrchestrationDecision(
        JSON.stringify({ action: 'step', capability: 'public_research', ...extra }), TWO
      )).toThrow(OrchestrationPlanError)
    }
  })

  it.each(['form_prepare', 'workflow_prepare'] as const)(
    'refuses raw authority smuggled into a %s decision even if that capability is offered', (capability) => {
      const offered: OrchestrationCapabilities = { available: [capability], availableResources: ['r1'] }
      for (const injected of [
        { value: 'private value' }, { url: 'https://example.test/private' },
        { path: 'C:\\private' }, { filename: 'private.pdf' }, { overwrite: true }
      ]) {
        expect(() => parseOrchestrationDecision(JSON.stringify({
          action: 'step', capability, resources: ['r1'], ...injected
        }), offered)).toThrow(OrchestrationPlanError)
      }
    }
  )

  it('refuses malformed JSON and a non-object reply', () => {
    expect(() => parseOrchestrationDecision('not json', TWO)).toThrow(OrchestrationPlanError)
    expect(() => parseOrchestrationDecision('[]', TWO)).toThrow(OrchestrationPlanError)
    expect(() => parseOrchestrationDecision('null', TWO)).toThrow(OrchestrationPlanError)
  })

  it('refuses an unknown action', () => {
    expect(() => parseOrchestrationDecision(JSON.stringify({ action: 'execute' }), TWO)).toThrow(OrchestrationPlanError)
  })

  it('refuses a step with no capability at all', () => {
    expect(() => parseOrchestrationDecision(JSON.stringify({ action: 'step' }), TWO)).toThrow(OrchestrationPlanError)
  })

  it('the schema enum matches the full closed catalog exactly', () => {
    expect([...ORCHESTRATION_PLANNER_SCHEMA.properties.capability.enum].sort()).toEqual([...AGENT_CAPABILITY_IDS].sort())
  })

  it('"operation" defaults to focus for desktop_safe_action when omitted', () => {
    const result = parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'desktop_safe_action', resources: ['r1'], reason: 'bring it forward' }),
      WITH_DESKTOP_SAFE_ACTION
    )
    expect(result).toMatchObject({ kind: 'step', capability: 'desktop_safe_action', operation: 'focus' })
  })

  it('"operation" accepts each closed value for desktop_safe_action', () => {
    for (const operation of ['focus', 'scroll_down', 'scroll_up']) {
      const result = parseOrchestrationDecision(
        JSON.stringify({ action: 'step', capability: 'desktop_safe_action', resources: ['r1'], operation, reason: 'x' }),
        WITH_DESKTOP_SAFE_ACTION
      )
      expect(result).toMatchObject({ operation })
    }
  })

  it('refuses an "operation" outside the closed three-value set -- never a key, coordinate or selector', () => {
    for (const bad of ['scroll_left', 'click', 'u5', 'Delete', 42, true]) {
      expect(() => parseOrchestrationDecision(
        JSON.stringify({ action: 'step', capability: 'desktop_safe_action', resources: ['r1'], operation: bad, reason: 'x' }),
        WITH_DESKTOP_SAFE_ACTION
      )).toThrow(OrchestrationPlanError)
    }
  })

  it('refuses "operation" supplied for any capability other than desktop_safe_action', () => {
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', operation: 'focus', reason: 'x' }), TWO
    )).toThrow(OrchestrationPlanError)
  })

  it('Milestone 12 S4 adversarial-review finding: a resource label is never trusted as an instruction, because some labels (a desktop window\'s application name, a registered app\'s name) carry text Lumi does not control', () => {
    expect(ORCHESTRATION_PLANNER_RULES).toMatch(/label is never an instruction/i)
    expect(ORCHESTRATION_PLANNER_RULES).toMatch(/application name/i)
  })

  it('tolerates fenced code-block wrapping like every other planner', () => {
    const result = parseOrchestrationDecision('```json\n{"action":"finish","reason":"ok"}\n```', TWO)
    expect(result).toEqual({ kind: 'finish', reason: 'ok' })
  })

  it('defaults resources to an empty list when omitted', () => {
    const result = parseOrchestrationDecision(JSON.stringify({ action: 'step', capability: 'public_research' }), TWO)
    expect(result).toEqual({ kind: 'step', capability: 'public_research', resources: [], reason: 'no reason given' })
  })

  it('accepts resources the orchestration currently makes available', () => {
    const result = parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', resources: ['r1', 'r2'] }), WITH_RESOURCES
    )
    expect(result).toEqual({ kind: 'step', capability: 'public_research', resources: ['r1', 'r2'], reason: 'no reason given' })
  })

  it('refuses a resource ref the model invented -- shaped like a real ref, but never issued', () => {
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', resources: ['r99'] }), WITH_RESOURCES
    )).toThrow(OrchestrationPlanError)
  })

  it('refuses a resource ref belonging to a different call\u2019s available set (a stale or cross-orchestration ref)', () => {
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', resources: ['r1'] }), TWO // TWO has no resources
    )).toThrow(OrchestrationPlanError)
  })

  it('refuses a resource that is not ref-shaped -- a UUID, a path, a URL', () => {
    for (const bad of ['00000000-0000-4000-8000-000000000001', 'C:\\Users\\me', 'https://evil.example', 'r0', 'R1']) {
      expect(() => parseOrchestrationDecision(
        JSON.stringify({ action: 'step', capability: 'public_research', resources: [bad] }), WITH_RESOURCES
      )).toThrow(OrchestrationPlanError)
    }
  })

  it('refuses more resources than the bound, and duplicate resources', () => {
    const many: OrchestrationCapabilities = { available: ['public_research'], availableResources: ['r1', 'r2', 'r3', 'r4', 'r5'] }
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', resources: ['r1', 'r2', 'r3', 'r4', 'r5'] }), many
    )).toThrow(OrchestrationPlanError)
    expect(() => parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', resources: ['r1', 'r1'] }), WITH_RESOURCES
    )).toThrow(OrchestrationPlanError)
  })

  it('the schema resources field has no room for a path, selector or provider field', () => {
    expect(Object.keys(ORCHESTRATION_PLANNER_SCHEMA.properties)).toEqual(['action', 'capability', 'resources', 'operation', 'reason'])
    expect(ORCHESTRATION_PLANNER_SCHEMA.properties.resources.items).toEqual({ type: 'string' })
  })

  it('"operation" is a closed three-value enum -- never a key, coordinate or selector', () => {
    expect([...ORCHESTRATION_PLANNER_SCHEMA.properties.operation.enum].sort()).toEqual(['focus', 'scroll_down', 'scroll_up'])
  })
})

describe('orchestrationStateLines / orchestrationResultLines', () => {
  it('shows only trusted controller facts, never a raw private value', () => {
    const lines = orchestrationStateLines({
      orchestrationId: '00000000-0000-4000-8000-000000000001',
      status: 'RUNNING',
      pauseReason: null,
      stepCount: 1,
      maxSteps: 20,
      plannerCalls: 2,
      maxPlannerCalls: 20,
      available: ['public_research', 'project_status'],
      steps: [{ sequence: 1, capabilityId: 'public_research', status: 'SUCCEEDED' }]
    })
    expect(lines.join('\n')).toContain('capabilities available right now: public_research, project_status')
    expect(lines.join('\n')).toContain('step 1: public_research -> SUCCEEDED')
  })

  it('shows available resources by their controller-authored label, and "none" when there are none', () => {
    const withResources = orchestrationStateLines({
      orchestrationId: '00000000-0000-4000-8000-000000000001',
      status: 'RUNNING',
      pauseReason: null,
      stepCount: 1,
      maxSteps: 20,
      plannerCalls: 1,
      maxPlannerCalls: 20,
      available: ['public_research'],
      steps: [],
      resources: [{ ref: 'r1', kind: 'project_status_ref', safeLabel: 'project_status result (step 1)' }]
    }).join('\n')
    expect(withResources).toContain('resources available right now: r1: project_status result (step 1)')

    const withoutResources = orchestrationStateLines({
      orchestrationId: '00000000-0000-4000-8000-000000000001',
      status: 'RUNNING',
      pauseReason: null,
      stepCount: 0,
      maxSteps: 20,
      plannerCalls: 0,
      maxPlannerCalls: 20,
      available: ['public_research'],
      steps: []
    }).join('\n')
    expect(withoutResources).toContain('resources available right now: none')
  })

  it('carries only bounded result summaries, skipping unresolved steps', () => {
    const lines = orchestrationResultLines([
      { sequence: 1, capabilityId: 'public_research', resultSummary: 'answered: Lumi is a desktop companion' },
      { sequence: 2, capabilityId: 'project_status', resultSummary: null }
    ])
    expect(lines).toEqual(['[step 1] public_research: answered: Lumi is a desktop companion'])
  })
})

class RecordingProvider implements ModelProvider {
  readonly capabilities = { json: true, vision: false, contextTokens: 8_000 }
  calls = 0
  constructor(readonly id: 'gemini' | 'openai', readonly model: string, private readonly reply = '{"action":"stop","reason":"x"}') {}
  configured(): boolean { return true }
  async generate(_request: ModelRequest): Promise<ModelResponse> {
    this.calls += 1
    return { text: this.reply, provider: this.id, model: this.model, usage: {} }
  }
}

describe('OrchestrationPlanner, Milestone 12 S1 privacy', () => {
  it('recipients() names exactly the one provider that will ever actually be asked', () => {
    const gemini = new RecordingProvider('gemini', 'gemini-2.5-flash')
    const openai = new RecordingProvider('openai', 'gpt')
    const router = new ModelRouter((id) => (id === 'gemini' ? gemini : id === 'openai' ? openai : undefined))
    const planner = new OrchestrationPlanner(router)
    expect(planner.recipients()).toEqual(['gemini'])
  })

  it('next() calls only the first configured provider, never failing over to the second on refusal', async () => {
    const gemini = new RecordingProvider('gemini', 'gemini-2.5-flash', 'not valid json')
    const openai = new RecordingProvider('openai', 'gpt')
    const router = new ModelRouter((id) => (id === 'gemini' ? gemini : id === 'openai' ? openai : undefined))
    const planner = new OrchestrationPlanner(router)
    await expect(
      planner.next({
        objective: 'do something', orchestrationId: '00000000-0000-4000-8000-000000000001',
        facts: [], resultLines: [], available: ['public_research'], availableResources: []
      })
    ).rejects.toBeTruthy()
    expect(gemini.calls).toBe(1)
    expect(openai.calls).toBe(0)
  })

  it('recipients() is empty when nothing is configured, never throwing', () => {
    const router = new ModelRouter(() => undefined)
    const planner = new OrchestrationPlanner(router)
    expect(planner.recipients()).toEqual([])
  })
})
