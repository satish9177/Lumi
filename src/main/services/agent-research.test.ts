import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ActiveTaskStore, AgentTaskController, type ResearchSupport } from './agent-tasks'
import { FakeResearchRuntime, PROJECT_BLOCKS } from '../testing/fake-research-runtime'
import { PublicUrlPolicy, RESEARCH_POLICY_VERSION } from '../agent/public-url-policy'
import { observationLines, scriptedResearchDecision, type ResearchPlanOutcome } from '../agent/research-planner'
import { NOT_VERIFIED_TEXT, scriptedResearchAnswer, verifyResearchGrounding } from '../agent/research-answer'
import type { AgentDisclosureRecipient, AgentTaskSnapshot } from '../../shared/agent-contracts'

/**
 * The research loop in Electron main, against the runtime's semantics.
 *
 * The properties under test are the ones that make this safe rather than
 * merely functional:
 *
 *  - nothing is searched or opened before the trusted Allow click;
 *  - the trusted click names the grant and the revision that were on screen;
 *  - the planner's step is submitted, never performed here, and a refusal
 *    ends the loop or forces a re-observation rather than being argued with;
 *  - a budget stops the loop with an honest partial answer;
 *  - an unconfirmed step is reported, never repeated.
 */

const OBJECTIVE = 'Find the Lumi project page and tell me how many contributors it has'

interface Harness {
  controller: AgentTaskController
  runtime: FakeResearchRuntime
  plannerCalls: number
}

let directory: string

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'lumi-research-'))
})

afterEach(async () => {
  await rm(directory, { recursive: true, force: true })
})

/** A planner that is the deterministic stand-in, and counts its own calls. */
function scriptedPlanner(state: { calls: number }, override?: () => ResearchPlanOutcome) {
  return {
    recipients: (): string[] => ['scripted'],
    async next(input: Parameters<NonNullable<ResearchSupport['planner']>['next']>[0]): Promise<ResearchPlanOutcome> {
      state.calls += 1
      if (override) return override()
      const operations = input.view.grant?.scope.allowedOperations ?? []
      return {
        decision: scriptedResearchDecision(input.objective, observationLines(input.view), { operations }),
        provider: 'scripted',
        model: 'scripted-research'
      }
    }
  }
}

function scriptedAnswerer(failures?: 'unavailable') {
  return {
    recipients: (): AgentDisclosureRecipient[] => ['scripted'],
    async answer(input: Parameters<NonNullable<ResearchSupport['answerer']>['answer']>[0]) {
      if (failures === 'unavailable') return { kind: 'unavailable' as const }
      const composed = scriptedResearchAnswer(input.objective, observationLines(input.view))
      verifyResearchGrounding(input.view, composed)
      return {
        kind: 'answered' as const,
        answer: { ...composed, stopReason: input.stopReason },
        provider: 'scripted' as AgentDisclosureRecipient,
        model: 'scripted-research'
      }
    }
  }
}

function harness(options: {
  runtime?: FakeResearchRuntime
  planner?: ResearchSupport['planner']
  answerer?: ResearchSupport['answerer']
  policy?: PublicUrlPolicy
} = {}): Harness & { state: { calls: number } } {
  const runtime = options.runtime ?? new FakeResearchRuntime()
  const state = { calls: 0 }
  const support: ResearchSupport = {
    policy: options.policy ?? new PublicUrlPolicy({
      allowAnyPublicHost: true, version: RESEARCH_POLICY_VERSION
    }),
    planner: options.planner ?? scriptedPlanner(state),
    answerer: options.answerer ?? scriptedAnswerer()
  }
  const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, support)
  return { controller, runtime, plannerCalls: 0, state }
}

async function started(controller: AgentTaskController): Promise<AgentTaskSnapshot> {
  const created = await controller.createResearchTask(OBJECTIVE)
  expect(created.ok).toBe(true)
  if (!created.ok) throw new Error('unreachable')
  return created.value
}

async function granted(controller: AgentTaskController): Promise<AgentTaskSnapshot> {
  const snapshot = await started(controller)
  const grant = snapshot.research?.grant
  expect(grant?.status).toBe('PENDING')
  const confirmed = await controller.grantResearchScope(grant!.grantId, grant!.revision)
  expect(confirmed.ok).toBe(true)
  if (!confirmed.ok) throw new Error('unreachable')
  return confirmed.value
}

describe('starting a research task', () => {
  it('shows the bounded scope and searches or opens nothing', async () => {
    const { controller, runtime } = harness()
    const snapshot = await started(controller)
    expect(snapshot.task.kind).toBe('public_research')
    expect(snapshot.task.research?.objective).toBe(OBJECTIVE)
    const scope = snapshot.research?.grant?.scope
    expect(snapshot.research?.grant?.status).toBe('PENDING')
    expect(snapshot.research?.grant?.expiresAt).toBeUndefined()
    expect(scope?.methods).toEqual(['GET', 'HEAD'])
    expect(scope?.forbidden).toContain('login')
    expect(scope?.forbidden).toContain('uploads_and_downloads')
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(0)
  })

  it('refuses an objective that is empty or too long', async () => {
    const { controller } = harness()
    for (const objective of ['', '   ', 'x'.repeat(501)]) {
      const result = await controller.createResearchTask(objective)
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.error.code).toBe('invalid_request')
    }
  })

  it('is unavailable without a configured destination policy', async () => {
    const { controller } = harness({ policy: new PublicUrlPolicy({ version: RESEARCH_POLICY_VERSION }) })
    const result = await controller.createResearchTask(OBJECTIVE)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('research_unavailable')
  })

  it('is unavailable without a planner and an answerer', async () => {
    const runtime = new FakeResearchRuntime()
    const controller = new AgentTaskController(runtime, new ActiveTaskStore(directory), undefined, {
      policy: new PublicUrlPolicy({ allowAnyPublicHost: true, version: RESEARCH_POLICY_VERSION })
    })
    const result = await controller.createResearchTask(OBJECTIVE)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('research_unavailable')
  })

  it('answers the same typed request once, however often it arrives', async () => {
    const { controller, runtime } = harness()
    const origin = { source: 'text' as const, turnId: 'req_research_1', utterance: OBJECTIVE }
    const first = await controller.createResearchTask(OBJECTIVE, origin)
    const again = await controller.createResearchTask(OBJECTIVE, origin)
    expect(first.ok && again.ok).toBe(true)
    if (first.ok && again.ok) expect(again.value.task.taskId).toBe(first.value.task.taskId)
    expect(runtime.tasks.size).toBe(1)
  })
})

describe('the trusted click', () => {
  it('is what makes research possible, and binds the revision on screen', async () => {
    const { controller, runtime } = harness()
    const snapshot = await started(controller)
    const grant = snapshot.research!.grant!

    // Running before the click is refused, and nothing leaves the machine.
    const early = await controller.runResearch()
    expect(early.ok).toBe(false)
    if (!early.ok) expect(early.error.code).toBe('research_not_granted')
    expect(runtime.searches).toBe(0)

    const stale = await controller.grantResearchScope(grant.grantId, grant.revision + 1)
    expect(stale.ok).toBe(false)
    if (!stale.ok) expect(stale.error.code).toBe('stale_revision')

    const confirmed = await controller.grantResearchScope(grant.grantId, grant.revision)
    expect(confirmed.ok).toBe(true)
    if (confirmed.ok) {
      expect(confirmed.value.research?.grant?.status).toBe('ACTIVE')
      expect(confirmed.value.research?.grant?.expiresAt).toBeTruthy()
      // Confirming does not start anything by itself.
      expect(runtime.searches).toBe(0)
      expect(runtime.pageOpens).toBe(0)
    }
  })

  it('refuses a grant that does not belong to the current task', async () => {
    const { controller } = harness()
    await started(controller)
    const result = await controller.grantResearchScope('00000000-0000-4000-8000-0000000000cc', 1)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('not_found')
  })

  it('declining withdraws the scope without opening anything', async () => {
    const { controller, runtime } = harness()
    const snapshot = await started(controller)
    const grant = snapshot.research!.grant!
    const declined = await controller.declineResearchScope(grant.grantId, grant.revision)
    expect(declined.ok).toBe(true)
    if (declined.ok) expect(declined.value.research?.grant?.status).toBe('REVOKED')
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
  })
})

describe('the research loop', () => {
  it('searches, opens a result, follows a link and records a grounded answer', async () => {
    const { controller, runtime } = harness()
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    const research = ran.value.research!
    expect(runtime.searches).toBe(1)
    expect(runtime.pageOpens).toBeGreaterThanOrEqual(2)
    expect(research.answer?.status).toBe('answered')
    expect(research.answer?.stopReason).toBe('goal_reached')
    expect(research.answer?.evidence.length).toBeGreaterThan(0)
    // The recorded quote really is text a page showed.
    const quoted = research.answer!.evidence[0].quote
    expect(PROJECT_BLOCKS.some((block) => block.includes(quoted))).toBe(true)
    expect(research.grant?.status).toBe('COMPLETED')
    // Every step went through the runtime; main performed none itself.
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(research.usage.steps)
  })

  it('never submits two steps at once', async () => {
    const { controller, runtime } = harness()
    await granted(controller)
    const [first, second] = await Promise.all([controller.runResearch(), controller.runResearch()])
    const busy = [first, second].filter((result) => !result.ok && result.error.code === 'busy')
    expect(busy).toHaveLength(1)
    expect(runtime.searches).toBe(1)
  })

  it('stops at the step budget with an honest partial answer', async () => {
    const runtime = new FakeResearchRuntime({ maxSteps: 2 })
    const { controller } = harness({ runtime })
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    expect(runtime.count('POST', /\/research\/steps$/)).toBeLessThanOrEqual(3)
    const answer = ran.value.research!.answer!
    expect(['budget_exhausted', 'goal_reached']).toContain(answer.stopReason)
  })

  it('re-observes rather than repeating a refused step, then stops', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime })
    await granted(controller)
    runtime.refuseNextNavigate = 'stale_target_ref'
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    // The refused navigate was not retried; an observe step took its place.
    const observes = runtime.calls.filter((call) =>
      /\/research\/steps$/.test(call.path) &&
      (call.body as { step?: { operation?: string } }).step?.operation === 'observe')
    expect(observes.length).toBeGreaterThanOrEqual(1)
    expect(ran.value.research?.answer).toBeTruthy()
  })

  it('reports an unconfirmed step instead of repeating it', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime })
    await granted(controller)
    runtime.loseNextStepResponse = true
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(false)
    if (!ran.ok) expect(ran.error.code).toBe('runtime_restarted')
    // The step did happen once. It is never sent again by this loop.
    expect(runtime.searches).toBe(1)
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(1)
  })

  it('stops with an unknown outcome rather than deciding what happened', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime })
    await granted(controller)
    runtime.nextStepOutcome = 'OUTCOME_UNKNOWN'
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    expect(ran.value.research?.answer).toBeUndefined()
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(1)
  })

  it('stops honestly when no model can plan inside the contract', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({
      runtime,
      planner: {
        recipients: () => ['scripted'],
        async next() { throw new Error('no provider answered') }
      }
    })
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    expect(ran.value.research?.answer?.stopReason).toBe('planner_failed')
    expect(ran.value.research?.answer?.answer).toBe(NOT_VERIFIED_TEXT)
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
  })

  it('will not choose an operation the scope withheld', async () => {
    // A scope without search: the planner is told so, and the runtime refuses
    // a search anyway. Nothing is searched either way.
    const runtime = new FakeResearchRuntime({ operations: ['observe'], searchConfigured: false })
    const { controller } = harness({ runtime })
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    expect(runtime.searches).toBe(0)
    expect(runtime.pageOpens).toBe(0)
  })

  it('keeps the evidence when no model can answer from it', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime, answerer: scriptedAnswerer('unavailable') })
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(false)
    if (!ran.ok) expect(ran.error.code).toBe('answer_unavailable')
    const view = await controller.loadActiveTask(0)
    expect(view.ok).toBe(true)
    if (view.ok && view.value) {
      expect(view.value.research?.observations.length).toBeGreaterThan(0)
      expect(view.value.research?.answer).toBeUndefined()
    }
  })

  it('stopping withdraws the scope and keeps what was read', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime })
    await granted(controller)
    await controller.runResearch()
    const stopped = await controller.stopResearch()
    expect(stopped.ok).toBe(true)
    if (stopped.ok) {
      expect(stopped.value.research?.grant?.status).toBe('REVOKED')
      expect(stopped.value.research?.observations.length).toBeGreaterThan(0)
    }
  })

  it('does not run again once an answer is recorded', async () => {
    const runtime = new FakeResearchRuntime()
    const { controller } = harness({ runtime })
    await granted(controller)
    await controller.runResearch()
    const before = runtime.count('POST', /\/research\/steps$/)
    const again = await controller.runResearch()
    expect(again.ok).toBe(true)
    expect(runtime.count('POST', /\/research\/steps$/)).toBe(before)
  })

  it('refuses a step the runtime says is outside the scope, without arguing', async () => {
    // A planner that insists on an operation the scope withholds.
    const runtime = new FakeResearchRuntime({ operations: ['observe'] })
    const { controller } = harness({
      runtime,
      planner: {
        recipients: () => ['scripted'],
        async next() {
          return {
            decision: { kind: 'step' as const, step: { operation: 'public_search' as const, query: 'lumi' }, reason: 'insisting' },
            provider: 'scripted',
            model: 'scripted-research'
          }
        }
      }
    })
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    expect(runtime.searches).toBe(0)
    expect(ran.value.research?.answer?.stopReason).toBe('blocked')
  })
})

describe('what the desktop is given', () => {
  it('receives refs, labels and hosts for links, never their addresses', async () => {
    const { controller } = harness()
    await granted(controller)
    const ran = await controller.runResearch()
    expect(ran.ok).toBe(true)
    if (!ran.ok) throw new Error('unreachable')
    for (const observation of ran.value.research!.observations) {
      for (const link of observation.links) {
        expect(Object.keys(link).sort()).toEqual(['host', 'ref', 'text'])
      }
      for (const result of observation.results) {
        expect(Object.keys(result).sort()).toEqual(['host', 'ref', 'snippet', 'title'])
      }
      expect(observation.kind === 'search_results' || observation.finalUrl || observation.kind === 'tab_state').toBeTruthy()
    }
  })
})
