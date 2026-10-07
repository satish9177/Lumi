import { describe, expect, it } from 'vitest'
import { isAllowedRuntimeRoute, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import { OrchestrationController } from './orchestration-controller'

/**
 * Every `/orchestrations*` call main makes goes through the supervisor's route allowlist. The allowlist
 * once had none of them, so General task and every typed orchestrated request failed in the running app
 * ("Lumi could not complete that request.") while the controller's own tests, which use a fake runtime,
 * stayed green. This drives the real controller through a requester that refuses exactly what the
 * supervisor refuses, so the controller and the allowlist cannot drift apart again.
 */

const ORCHESTRATION = '00000000-0000-4000-8000-000000000001'

function orchestrationBody(): Record<string, unknown> {
  return {
    orchestration_id: ORCHESTRATION,
    status: 'RUNNING',
    pause_reason: null,
    live: true,
    revision: 1,
    objective: 'Show the status of my registered projects',
    step_count: 0,
    child_task_count: 0,
    planner_calls: 0,
    created_at: '2026-09-24T10:00:00+00:00',
    expires_at: '2026-09-24T10:30:00+00:00',
    stopped_at: null,
    available_capabilities: ['project_status'],
    resources: [],
    steps: []
  }
}

function allowlistedRuntime(): { calls: Array<[RuntimeMethod, string]>; request: (method: RuntimeMethod, path: string) => Promise<RuntimeReply> } {
  const calls: Array<[RuntimeMethod, string]> = []
  return {
    calls,
    request: async (method, path) => {
      calls.push([method, path])
      // The same refusal the supervisor applies before anything is sent.
      if (!isAllowedRuntimeRoute(method, path)) throw new Error('Refused an unlisted agent runtime route.')
      const body = path === '/orchestrations/latest' ? { orchestration: orchestrationBody() } : orchestrationBody()
      return { status: 200, body, generation: '00000000-0000-4000-8000-0000000000aa' }
    }
  }
}

describe('OrchestrationController against the runtime route allowlist', () => {
  it('reaches every orchestration route it uses through the allowlist', async () => {
    const runtime = allowlistedRuntime()
    const controller = new OrchestrationController(runtime)
    const results = [
      await controller.createOrchestration('Show the status of my registered projects'),
      await controller.getLatestOrchestration(),
      await controller.getOrchestration(ORCHESTRATION),
      await controller.recordPlannerCall(ORCHESTRATION, 1),
      await controller.advanceOrchestration(ORCHESTRATION, 1, 'project_status', { resolvedSummary: 'No registered project run.' }),
      await controller.registerResource(ORCHESTRATION, 1, { kind: 'document_ref', safeLabel: 'offer.txt', backingId: ORCHESTRATION }),
      await controller.resumeOrchestration(ORCHESTRATION, 1),
      await controller.finishOrchestration(ORCHESTRATION, 1),
      await controller.stopOrchestration(ORCHESTRATION)
    ]
    for (const result of results) expect(result.ok, JSON.stringify(result)).toBe(true)
    expect(runtime.calls.map(([method, path]) => `${method} ${path.replace(ORCHESTRATION, '{id}')}`)).toEqual([
      'POST /orchestrations',
      'GET /orchestrations/latest',
      'GET /orchestrations/{id}',
      'POST /orchestrations/{id}/planner-call',
      'POST /orchestrations/{id}/advance',
      'POST /orchestrations/{id}/resources',
      'POST /orchestrations/{id}/resume',
      'POST /orchestrations/{id}/finish',
      'GET /orchestrations/{id}',
      'POST /orchestrations/{id}/stop'
    ])
  })

  it('allows only those exact shapes', () => {
    for (const [method, path] of [
      ['GET', '/orchestrations'],
      ['POST', '/orchestrations/latest'],
      ['POST', `/orchestrations/${ORCHESTRATION}`],
      ['GET', `/orchestrations/${ORCHESTRATION}/stop`],
      ['POST', `/orchestrations/${ORCHESTRATION}/execute`],
      ['POST', `/orchestrations/${ORCHESTRATION}/steps`],
      ['POST', `/orchestrations/${ORCHESTRATION}/resources/r1`],
      ['POST', '/orchestrations/not-a-uuid/advance'],
      ['POST', `/orchestrations/${ORCHESTRATION}/advance?capability=run_shell`],
      ['POST', `/orchestrations/${ORCHESTRATION}/../../tasks`]
    ] as const) expect(isAllowedRuntimeRoute(method, path), `${method} ${path}`).toBe(false)
  })
})
