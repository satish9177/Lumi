import { describe, expect, it } from 'vitest'
import {
  ORCHESTRATION_PLANNER_SCHEMA,
  OrchestrationPlanError,
  orchestrationResultLines,
  orchestrationStateLines,
  parseOrchestrationDecision,
  type OrchestrationCapabilities
} from './orchestration-planner'
import { AGENT_CAPABILITY_IDS } from '../../shared/agent-capabilities'

/**
 * Milestone 11 S2: what the orchestration planner is *able to say*.
 *
 * The planner writes primitives into a flat object of closed enumerations, and this module constructs the
 * decision from them. These tests are the proof that there is no field left over: no path, no URL, no
 * command, no approval, no capability id outside the closed catalog, and no capability id outside the
 * orchestration's own currently-available set even when it is a real catalog id.
 */

const TWO: OrchestrationCapabilities = { available: ['public_research', 'project_status'] }

describe('parseOrchestrationDecision', () => {
  it('parses a step choosing an available capability', () => {
    const result = parseOrchestrationDecision(
      JSON.stringify({ action: 'step', capability: 'public_research', reason: 'the objective needs research' }), TWO
    )
    expect(result).toEqual({ kind: 'step', capability: 'public_research', reason: 'the objective needs research' })
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

  it('tolerates fenced code-block wrapping like every other planner', () => {
    const result = parseOrchestrationDecision('```json\n{"action":"finish","reason":"ok"}\n```', TWO)
    expect(result).toEqual({ kind: 'finish', reason: 'ok' })
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

  it('carries only bounded result summaries, skipping unresolved steps', () => {
    const lines = orchestrationResultLines([
      { sequence: 1, capabilityId: 'public_research', resultSummary: 'answered: Lumi is a desktop companion' },
      { sequence: 2, capabilityId: 'project_status', resultSummary: null }
    ])
    expect(lines).toEqual(['[step 1] public_research: answered: Lumi is a desktop companion'])
  })
})
