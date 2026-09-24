import type { AgentError, AgentResult } from '../../shared/agent-contracts'
import type { AgentCapabilityId } from '../../shared/agent-capabilities'
import type { AgentOrchestrationView } from '../../shared/orchestration-contracts'
import { WireError } from './agent-wire'
import { AgentRequestError } from './agent-tasks'
import { RuntimeRestartedError, RuntimeUnavailableError, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import { parseLatestOrchestration, parseOrchestration, projectOrchestrationRuntimeError } from './orchestration-wire'

/**
 * The low-level trusted client for Milestone 11 S2's durable orchestration graph.
 *
 * It only records durable state: creating an orchestration, advancing one already-decided step, resuming a
 * paused one, finishing or stopping it. It never chooses a capability itself (that is
 * `OrchestrationPlanner`) and never drives a capability to completion itself (that is
 * `OrchestrationCoordinator`, which composes this class with the planner and each capability's own
 * existing controller). Every method here maps 1:1 onto one `/orchestrations*` runtime route.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/
const TIMEOUTS = { read: 10_000, write: 30_000 } as const

type OrchestrationFailureCode = 'invalid_request' | 'not_found' | 'orchestration_state_changed' | 'runtime_unavailable' | 'runtime_restarted'

function fail(code: OrchestrationFailureCode, message: string): never {
  throw new AgentRequestError({ code, message })
}

function toAgentError(error: unknown): AgentError {
  if (error instanceof AgentRequestError) return error.agentError
  if (error instanceof WireError) {
    return { code: 'invalid_response', message: 'Lumi received an unexpected response from its agent runtime and ignored it.' }
  }
  return { code: 'request_failed', message: 'Lumi could not complete that request.' }
}

function id(value: unknown, what: string): string {
  if (typeof value !== 'string' || !UUID.test(value)) fail('invalid_request', `That ${what} reference is invalid.`)
  return value
}

function objectiveText(value: unknown): string {
  if (typeof value !== 'string') fail('invalid_request', 'Say what task Lumi should work on.')
  const text = value.trim().replace(/\s+/g, ' ')
  if (!text || text.length > 500 || CONTROL_CHARS.test(text)) fail('invalid_request', 'Say what task Lumi should work on, in 500 characters or fewer.')
  return text
}

export class OrchestrationController {
  private readonly inFlight = new Set<string>()

  constructor(private readonly runtime: DesktopRuntimeRequester) {}

  private async call(method: RuntimeMethod, path: string, body: unknown, timeoutMs: number): Promise<RuntimeReply> {
    let reply: RuntimeReply
    try {
      reply = await this.runtime.request(method, path, body, timeoutMs)
    } catch (error) {
      if (error instanceof RuntimeUnavailableError) fail('runtime_unavailable', 'The Lumi agent runtime is not running. Nothing was done.')
      if (error instanceof RuntimeRestartedError) {
        fail('runtime_restarted', 'Lumi could not confirm that step. Showing the latest saved state; nothing will be retried automatically.')
      }
      throw error
    }
    if (reply.status < 200 || reply.status > 299) throw new AgentRequestError(projectOrchestrationRuntimeError(reply.status, reply.body))
    return reply
  }

  private async guarded<T>(key: string, parse: () => void, work: () => Promise<T>): Promise<AgentResult<T>> {
    try {
      parse()
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    }
    if (this.inFlight.has(key)) return { ok: false, error: { code: 'busy', message: 'Lumi is already working on that.' } }
    this.inFlight.add(key)
    try {
      return { ok: true, value: await work() }
    } catch (error) {
      return { ok: false, error: toAgentError(error) }
    } finally {
      this.inFlight.delete(key)
    }
  }

  async current(orchestrationId: string): Promise<AgentOrchestrationView> {
    const view = parseOrchestration((await this.call('GET', `/orchestrations/${orchestrationId}`, undefined, TIMEOUTS.read)).body)
    if (view.orchestrationId !== orchestrationId) throw new WireError('orchestration.id')
    return view
  }

  private async post(orchestrationId: string, suffix: string, body: unknown, timeoutMs: number): Promise<AgentOrchestrationView> {
    const view = parseOrchestration((await this.call('POST', `/orchestrations/${orchestrationId}/${suffix}`, body, timeoutMs)).body)
    if (view.orchestrationId !== orchestrationId) throw new WireError('orchestration.id')
    return view
  }

  createOrchestration(objectiveValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    let objective = ''
    return this.guarded('orchestrations:create', () => {
      objective = objectiveText(objectiveValue)
    }, async () => parseOrchestration((await this.call('POST', '/orchestrations', { objective }, TIMEOUTS.write)).body))
  }

  getOrchestration(orchestrationIdValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    let orchestrationId = ''
    return this.guarded(`orchestrations:get:${String(orchestrationIdValue)}`, () => {
      orchestrationId = id(orchestrationIdValue, 'orchestration')
    }, async () => this.current(orchestrationId))
  }

  getLatestOrchestration(): Promise<AgentResult<AgentOrchestrationView | null>> {
    return this.guarded('orchestrations:latest', () => undefined, async () =>
      parseLatestOrchestration((await this.call('GET', '/orchestrations/latest', undefined, TIMEOUTS.read)).body))
  }

  /** Counts one planner call against the budget. Called immediately before asking a model anything. */
  recordPlannerCall(orchestrationId: string, expectedRevision: number): Promise<AgentResult<AgentOrchestrationView>> {
    return this.guarded(`orchestrations:step:${orchestrationId}`, () => undefined, async () =>
      this.post(orchestrationId, 'planner-call', { expected_revision: expectedRevision }, TIMEOUTS.write))
  }

  /**
   * Record one already-decided capability choice. `taskId` for a task-backed capability (the caller
   * already created it through that capability's own boundary); `resolvedSummary` for a synchronous one
   * (the caller already computed it through that capability's own existing read method). Never both.
   */
  advanceOrchestration(
    orchestrationId: string, expectedRevision: number, capabilityId: AgentCapabilityId,
    options: { taskId?: string; resolvedSummary?: string }
  ): Promise<AgentResult<AgentOrchestrationView>> {
    return this.guarded(`orchestrations:step:${orchestrationId}`, () => undefined, async () =>
      this.post(orchestrationId, 'advance', {
        expected_revision: expectedRevision,
        capability_id: capabilityId,
        ...(options.taskId !== undefined ? { task_id: options.taskId } : {}),
        ...(options.resolvedSummary !== undefined ? { resolved_summary: options.resolvedSummary } : {})
      }, TIMEOUTS.write))
  }

  resumeOrchestration(orchestrationId: string, expectedRevision: number): Promise<AgentResult<AgentOrchestrationView>> {
    return this.guarded(`orchestrations:step:${orchestrationId}`, () => undefined, async () =>
      this.post(orchestrationId, 'resume', { expected_revision: expectedRevision }, TIMEOUTS.write))
  }

  finishOrchestration(orchestrationId: string, expectedRevision: number): Promise<AgentResult<AgentOrchestrationView>> {
    return this.guarded(`orchestrations:step:${orchestrationId}`, () => undefined, async () =>
      this.post(orchestrationId, 'finish', { expected_revision: expectedRevision }, TIMEOUTS.write))
  }

  stopOrchestration(orchestrationIdValue: unknown): Promise<AgentResult<AgentOrchestrationView>> {
    let orchestrationId = ''
    // Stop is never blocked behind another request on the same orchestration: it has its own key.
    return this.guarded(`orchestrations:stop:${String(orchestrationIdValue)}`, () => {
      orchestrationId = id(orchestrationIdValue, 'orchestration')
    }, async () => {
      const view = await this.current(orchestrationId)
      return this.post(orchestrationId, 'stop', { expected_revision: view.revision }, TIMEOUTS.write)
    })
  }
}
