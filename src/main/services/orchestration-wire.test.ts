import { describe, expect, it } from 'vitest'
import { WireError } from './agent-wire'
import { parseLatestOrchestration, parseOrchestration, projectOrchestrationRuntimeError } from './orchestration-wire'

/**
 * Milestone 11 S2: the runtime response is untrusted wire data until this parser says otherwise. These
 * tests pin the two properties that matter -- a violation rejects the whole response, and a capability id
 * outside the closed catalog is never silently passed through even if the runtime's own CHECK constraint
 * should already have refused it.
 */

function orchestrationBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    orchestration_id: '00000000-0000-4000-8000-000000000001',
    status: 'RUNNING',
    pause_reason: null,
    live: true,
    revision: 1,
    objective: 'Research the Lumi repository and summarize it',
    step_count: 0,
    child_task_count: 0,
    planner_calls: 0,
    created_at: '2026-09-24T10:00:00+00:00',
    expires_at: '2026-09-24T10:30:00+00:00',
    stopped_at: null,
    available_capabilities: ['public_research', 'project_status'],
    steps: [],
    ...overrides
  }
}

describe('parseOrchestration', () => {
  it('parses a well-formed response', () => {
    const view = parseOrchestration(orchestrationBody())
    expect(view.orchestrationId).toBe('00000000-0000-4000-8000-000000000001')
    expect(view.status).toBe('RUNNING')
    expect(view.availableCapabilities).toEqual(['public_research', 'project_status'])
  })

  it('parses a step, including a task-backed and a synchronous one', () => {
    const view = parseOrchestration(orchestrationBody({
      steps: [
        { sequence: 1, capability_id: 'public_research', status: 'AWAITING_APPROVAL', child_task_id: '00000000-0000-4000-8000-000000000002', result_handle: null, result_summary: null },
        { sequence: 2, capability_id: 'project_status', status: 'SUCCEEDED', child_task_id: null, result_handle: 'project_status:2', result_summary: 'Project run phase: running, ready.' }
      ]
    }))
    expect(view.steps).toHaveLength(2)
    expect(view.steps[0].childTaskId).toBe('00000000-0000-4000-8000-000000000002')
    expect(view.steps[1].resultSummary).toBe('Project run phase: running, ready.')
  })

  it('rejects a capability id outside the closed catalog, even inside available_capabilities', () => {
    expect(() => parseOrchestration(orchestrationBody({ available_capabilities: ['run_shell'] }))).toThrow(WireError)
  })

  it('rejects a step whose capability_id is outside the closed catalog', () => {
    expect(() => parseOrchestration(orchestrationBody({
      steps: [{ sequence: 1, capability_id: 'run_shell', status: 'SUCCEEDED', result_handle: 'x:1', result_summary: 'y' }]
    }))).toThrow(WireError)
  })

  it('rejects an unknown status or pause_reason', () => {
    expect(() => parseOrchestration(orchestrationBody({ status: 'DELETING' }))).toThrow(WireError)
    expect(() => parseOrchestration(orchestrationBody({ pause_reason: 'something_made_up' }))).toThrow(WireError)
  })

  it('rejects a response whose top-level id does not round-trip', () => {
    expect(() => parseOrchestration({ ...orchestrationBody(), orchestration_id: 'not-a-uuid' })).toThrow(WireError)
  })

  it('rejects a malformed or missing required field outright, never defaulting it', () => {
    const body = orchestrationBody()
    delete body.revision
    expect(() => parseOrchestration(body)).toThrow(WireError)
  })
})

describe('parseLatestOrchestration', () => {
  it('parses null as no orchestration', () => {
    expect(parseLatestOrchestration({ orchestration: null })).toBeNull()
  })

  it('parses a present orchestration', () => {
    expect(parseLatestOrchestration({ orchestration: orchestrationBody() })?.orchestrationId)
      .toBe('00000000-0000-4000-8000-000000000001')
  })
})

describe('projectOrchestrationRuntimeError', () => {
  it('maps orchestration_state_changed to a known reason message', () => {
    const error = projectOrchestrationRuntimeError(409, { error: { code: 'orchestration_state_changed', reason: 'revision_conflict' } })
    expect(error.code).toBe('orchestration_state_changed')
    expect(error.message).toContain('changed since it was last read')
  })

  it('maps orchestration_refused with an unknown reason to a generic message, never crashing', () => {
    const error = projectOrchestrationRuntimeError(422, { error: { code: 'orchestration_refused', reason: 'something_new' } })
    expect(error.code).toBe('orchestration_refused')
    expect(error.message.length).toBeGreaterThan(0)
  })

  it('falls back to invalid_request/request_failed for anything else', () => {
    expect(projectOrchestrationRuntimeError(422, {}).code).toBe('invalid_request')
    expect(projectOrchestrationRuntimeError(500, {}).code).toBe('request_failed')
  })
})
