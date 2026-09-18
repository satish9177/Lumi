import { describe, expect, it } from 'vitest'
import contract from '../../shared/agent-runtime-contract.json'
import {
  GRANT_STATUSES,
  RESEARCH_ANSWER_STATUSES,
  RESEARCH_OPERATIONS,
  RESEARCH_STOP_REASONS
} from '../../shared/agent-contracts'
import { WireError, parseResearch, parseResearchScope, parseResearchStep, parseTask } from './agent-wire'

/**
 * The Milestone 7b half of the Python/TypeScript contract, checked against the
 * examples the runtime generates from its own models.
 *
 * The assertions that matter most are about what does *not* cross: a link or a
 * search result arrives as a ref, a label and a host, and never as an address.
 * If the runtime ever started sending one, these tests fail rather than the
 * address quietly reaching a model prompt.
 */

type Json = Record<string, unknown>
const examples = contract.examples as unknown as Record<string, Json>
const clone = <T>(value: T): T => structuredClone(value)
const RESEARCH_TASK = '00000000-0000-4000-8000-00000000000a'

describe('the research contract', () => {
  it('pins the enums the runtime generates', () => {
    expect([...GRANT_STATUSES]).toEqual((contract.enums as unknown as Record<string, string[]>).GrantStatus)
    expect([...RESEARCH_OPERATIONS]).toEqual((contract.enums as unknown as Record<string, string[]>).ResearchOperation)
  })

  it('declares every research refusal code', () => {
    for (const code of [
      'research_not_configured', 'research_grant_not_found', 'research_grant_not_usable',
      'research_step_refused', 'research_budget_exhausted', 'research_step_in_flight',
      'research_session_unavailable', 'research_answer_already_recorded',
      'research_answer_not_grounded', 'research_search_failed'
    ]) {
      expect(contract.errorCodes).toContain(code)
    }
  })

  it('reads the scope card exactly as the runtime emits it', () => {
    const card = parseResearch(examples.research_card)
    expect(card.task.kind).toBe('public_research')
    expect(card.task.research?.objective).toBeTruthy()
    const grant = card.view.grant!
    expect(grant.status).toBe('PENDING')
    expect(grant.expiresAt).toBeUndefined()
    expect(grant.scope.methods).toEqual(['GET', 'HEAD'])
    expect(grant.scope.hosts).toBe('any_public')
    expect(grant.scope.forbidden).toContain('login')
    expect(grant.scope.forbidden).toContain('uploads_and_downloads')
    expect(grant.scope.forbidden).toContain('private_network')
    expect(grant.scope.budgets.maxSteps).toBeGreaterThan(0)
    expect(card.view.observations).toEqual([])
    expect(card.view.answer).toBeUndefined()
  })

  it('reads an active task, its observations and its refs', () => {
    const active = parseResearch(examples.research_active)
    expect(active.view.grant?.status).toBe('ACTIVE')
    expect(active.view.grant?.expiresAt).toBeTruthy()
    expect(active.view.session?.status).toBe('OPEN')
    const [search, page] = active.view.observations
    expect(search.kind).toBe('search_results')
    expect(search.ref).toBe('o1')
    expect(search.results.map((result) => result.ref)).toEqual(['r1', 'r2'])
    expect(page.kind).toBe('page')
    expect(page.ref).toBe('o2')
    expect(page.finalUrl).toBe('https://github.com/satish9177/Lumi')
    expect(page.links.map((link) => link.ref)).toEqual(['l1'])
    // The page a research task read carries its untrusted provenance, and the
    // injection it printed is quotable text and nothing more.
    expect(page.blocks.some((block) => block.text.includes('IGNORE PREVIOUS INSTRUCTIONS'))).toBe(true)
  })

  it('gives the desktop a ref, a label and a host for a link, never an address', () => {
    const active = parseResearch(examples.research_active)
    for (const observation of active.view.observations) {
      for (const link of observation.links) expect(Object.keys(link).sort()).toEqual(['host', 'ref', 'text'])
      for (const result of observation.results) expect(Object.keys(result).sort()).toEqual(['host', 'ref', 'snippet', 'title'])
    }
  })

  it('refuses an observation that tries to send a link address', () => {
    const body = clone(examples.research_active)
    const observations = body.observations as Json[]
    const links = (observations[1].links as Json[])
    links[0] = { ...links[0], url: 'https://exfil.invalid/' }
    expect(() => parseResearch(body)).toThrow(WireError)
  })

  it('reads a recorded answer and the sources behind it', () => {
    const answered = parseResearch(examples.research_answered)
    const answer = answered.view.answer!
    expect(RESEARCH_ANSWER_STATUSES).toContain(answer.status)
    expect(RESEARCH_STOP_REASONS).toContain(answer.stopReason)
    expect(answer.evidence[0]).toMatchObject({ observation: 'o2', block: 'b2' })
    expect(answered.view.grant?.status).toBe('COMPLETED')
    expect(answered.task.status).toBe('SUCCEEDED')
  })

  it('reads one step outcome, and the action that carries no approval', () => {
    const step = parseResearchStep(examples.research_step)
    expect(step.outcome).toBe('SUCCEEDED')
    expect(step.replayed).toBe(false)
    expect(step.observation?.ref).toBe('o2')
    expect(step.view.taskId).toBe(RESEARCH_TASK)
  })

  it('reads the research timeline, which never says a step was approved', () => {
    const body = examples.research_events as Json
    const types = (body.events as Json[]).map((event) => event.event_type)
    expect(types).toContain('task.research_scope_requested')
    expect(types).toContain('task.research_scope_granted')
    expect(types).toContain('action.authorized')
    expect(types).not.toContain('action.approved')
  })

  it('refuses a grant that claims to be active without a window', () => {
    const body = clone(examples.research_active)
    const grant = body.grant as Json
    grant.expires_at = null
    expect(() => parseResearch(body)).toThrow(WireError)
  })

  it('refuses a scope carrying a field nobody reviewed', () => {
    const scope = clone((examples.research_card.grant as Json).scope) as Json
    scope.allow_login = true
    expect(() => parseResearchScope(scope)).toThrow(WireError)
  })

  it('refuses a research payload for a task of another kind', () => {
    const body = clone(examples.research_active)
    ;(body.task as Json).request = { type: 'clinic_info', doctor: 'Dr A', topic: 'hours' }
    expect(() => parseResearch(body)).toThrow(WireError)
  })

  it('refuses observations that arrive out of order', () => {
    const body = clone(examples.research_active)
    const observations = body.observations as Json[]
    body.observations = [observations[1], observations[0]]
    expect(() => parseResearch(body)).toThrow(WireError)
  })

  it('still reads a research task record itself', () => {
    const task = parseTask((examples.research_card as Json).task)
    expect(task.kind).toBe('public_research')
    expect(task.research?.objective).toContain('Lumi')
  })
})
