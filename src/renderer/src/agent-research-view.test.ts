import { describe, expect, it } from 'vitest'
import {
  RESEARCH_ALLOWED_LABELS,
  RESEARCH_FORBIDDEN_LABELS,
  describeEvent,
  describeResearch,
  describeResearchProgress,
  describeScopeEntry,
  researchPageCount,
  researchSources
} from './agent-task-view'
import { describeResearchForConversation } from './composer-routing'
import type {
  AgentGrantStatus,
  AgentResearchObservationView,
  AgentResearchView
} from '../../shared/agent-contracts'

/**
 * What the research card says, derived only from durable state.
 *
 * The card is a trusted surface: every label and every line is Lumi's own, and
 * page-controlled text appears only as plain text in labelled fields. These
 * tests pin the three claims that matter -- nothing is described as running
 * before the permission is confirmed, the "not allowed" list is always shown
 * with the scope, and an unverified answer is never dressed up as an answer.
 */

const NOW = Date.parse('2026-09-18T10:05:00Z')

function observation(overrides: Partial<AgentResearchObservationView> = {}): AgentResearchObservationView {
  return {
    observationId: '00000000-0000-4000-8000-000000000001',
    ref: 'o1',
    sequence: 1,
    kind: 'page',
    operation: 'navigate',
    tab: 't1',
    documentEpoch: 2,
    finalUrl: 'https://github.com/satish9177/Lumi',
    finalHost: 'github.com',
    title: 'GitHub - satish9177/Lumi',
    settled: true,
    truncated: false,
    observedAt: '2026-09-18T10:00:00+00:00',
    contentHash: 'a'.repeat(64),
    blocks: [{ id: 'b1', text: 'A safe floating AI desktop companion for Windows' }],
    links: [],
    results: [],
    openTabs: ['t1'],
    ...overrides
  }
}

function view(options: {
  status?: AgentGrantStatus
  observations?: AgentResearchObservationView[]
  answer?: AgentResearchView['answer']
  expiresAt?: string
  steps?: number
  unresolved?: boolean
} = {}): AgentResearchView {
  const observations = options.observations ?? []
  return {
    taskId: '00000000-0000-4000-8000-0000000000ff',
    objective: 'Find the Lumi repository on GitHub and tell me what it does',
    grant: options.status === undefined ? undefined : {
      grantId: '00000000-0000-4000-8000-0000000000aa',
      status: options.status,
      revision: 2,
      scopeDigest: 'b'.repeat(64),
      scope: {
        policyVersion: 'public-research-v1',
        allowedOperations: ['public_search', 'navigate', 'observe', 'scroll', 'history', 'tab'],
        allowed: ['public_search', 'public_https_navigation', 'follow_public_links', 'read_page_text', 'task_owned_tabs'],
        forbidden: ['login', 'forms_and_typing', 'uploads_and_downloads', 'purchases_and_payments', 'messages', 'files', 'private_network', 'non_get_requests'],
        schemes: ['https'],
        methods: ['GET', 'HEAD'],
        hosts: 'any_public',
        budgets: {
          maxSteps: 20, maxObservations: 30, maxPlannerCalls: 20, maxTabs: 5,
          maxActiveSeconds: 300, maxModelInputTokens: 60_000, maxModelOutputTokens: 8_000, maxVisionCalls: 2
        },
        recipients: ['gemini'],
        maxTextChars: 10_000,
        seeds: []
      },
      createdAt: '2026-09-18T09:59:00+00:00',
      ...(options.status === 'PENDING' ? {} : { confirmedAt: '2026-09-18T09:59:30+00:00' }),
      ...(options.expiresAt ? { expiresAt: options.expiresAt } : options.status === 'ACTIVE' ? { expiresAt: '2099-01-01T00:00:00+00:00' } : {})
    },
    observations,
    ...(options.answer ? { answer: options.answer } : {}),
    usage: {
      steps: options.steps ?? observations.length,
      observations: observations.length,
      plannerCalls: observations.length + 1,
      activeSeconds: 12,
      tabs: 1
    },
    searchConfigured: true,
    unresolvedStep: options.unresolved === true
  }
}

describe('the research permission card', () => {
  it('asks for permission and offers exactly two controls', () => {
    const card = describeResearch(view({ status: 'PENDING' }), NOW)
    expect(card.tone).toBe('approval')
    expect(card.eyebrow).toBe('NEEDS YOUR PERMISSION')
    expect(card.showScope).toBe(true)
    expect(card.showProgress).toBe(false)
    expect(card.controls).toEqual(['decline_research', 'allow_research'])
    expect(card.lines.join(' ')).toContain('not signed in to anything')
  })

  it('never describes work that has not been allowed', () => {
    const card = describeResearch(view({ status: 'PENDING' }), NOW)
    expect(card.title).not.toMatch(/reading|searching/i)
    expect(card.controls).not.toContain('run_research')
  })

  it('turns each scope entry into Lumi’s own words', () => {
    const scope = view({ status: 'PENDING' }).grant!.scope
    for (const entry of scope.allowed) {
      expect(describeScopeEntry(entry, true)).toBe(RESEARCH_ALLOWED_LABELS[entry])
    }
    for (const entry of scope.forbidden) {
      expect(describeScopeEntry(entry, false)).toBe(RESEARCH_FORBIDDEN_LABELS[entry])
    }
    // An entry Lumi does not recognise is still shown, never hidden.
    expect(describeScopeEntry('something_new', false)).toBe('something new')
  })

  it('shows progress once it is running, and a way to stop', () => {
    const running = describeResearch(view({ status: 'ACTIVE', observations: [observation()], steps: 2 }), NOW)
    expect(running.tone).toBe('progress')
    expect(running.showProgress).toBe(true)
    expect(running.controls).toEqual(['stop_research'])
    expect(describeResearchProgress(view({ status: 'ACTIVE', observations: [observation()], steps: 2 })))
      .toBe('1 page read · 2 of 20 steps')
  })

  it('offers to start once allowed but before anything has run', () => {
    const ready = describeResearch(view({ status: 'ACTIVE' }), NOW)
    expect(ready.controls).toEqual(['run_research', 'stop_research'])
  })

  it('says it does not know what a step did rather than showing confident progress', () => {
    const unresolved = describeResearch(
      view({ status: 'ACTIVE', observations: [observation()], steps: 2, unresolved: true }),
      NOW
    )
    expect(unresolved.eyebrow).toBe('STEP UNRESOLVED')
    expect(unresolved.tone).toBe('neutral')
    expect(unresolved.title).toContain('does not know')
    expect(unresolved.showProgress).toBe(true)
    // The only thing offered is the way out; nothing claims the step succeeded.
    expect(unresolved.controls).toEqual(['stop_research'])
    expect(unresolved.lines.join(' ')).not.toMatch(/found|read the page/i)
  })

  it('says the permission expired rather than pretending it is live', () => {
    const expired = describeResearch(view({ status: 'ACTIVE', expiresAt: '2026-09-18T10:00:00+00:00' }), NOW)
    expect(expired.eyebrow).toBe('PERMISSION EXPIRED')
    expect(expired.controls).not.toContain('run_research')
  })

  it('shows a verified answer as an answer, and an unverified one as not verified', () => {
    const answered = describeResearch(view({
      status: 'COMPLETED',
      observations: [observation()],
      answer: {
        status: 'answered', stopReason: 'goal_reached',
        answer: 'Lumi is a safe floating AI desktop companion for Windows.',
        evidence: [{ observation: 'o1', block: 'b1', quote: 'A safe floating AI desktop companion for Windows' }],
        provider: 'gemini', model: 'gemini-2.5-flash', stepsUsed: 2, observationsUsed: 1, plannerCalls: 3,
        createdAt: '2026-09-18T10:01:00+00:00'
      }
    }), NOW)
    expect(answered.tone).toBe('success')
    expect(answered.eyebrow).toBe('ANSWER FROM PUBLIC PAGES')

    const unverified = describeResearch(view({
      status: 'COMPLETED',
      observations: [observation()],
      answer: {
        status: 'not_found', stopReason: 'budget_exhausted',
        answer: 'Lumi could not verify that from the public pages it was able to read.',
        evidence: [], provider: 'gemini', model: 'gemini-2.5-flash',
        stepsUsed: 20, observationsUsed: 1, plannerCalls: 20, createdAt: '2026-09-18T10:01:00+00:00'
      }
    }), NOW)
    expect(unverified.tone).toBe('neutral')
    expect(unverified.eyebrow).toBe('NOT VERIFIED')
    expect(unverified.lines).toContain('Lumi reached the limit for this task and stopped.')
  })

  it('keeps the evidence visible after the research was stopped', () => {
    const stopped = describeResearch(view({ status: 'REVOKED', observations: [observation()] }), NOW)
    expect(stopped.eyebrow).toBe('STOPPED')
    expect(stopped.showProgress).toBe(true)
    expect(stopped.controls).toEqual([])
  })

  it('says nothing has happened when there is no permission at all', () => {
    const none = describeResearch(view(), NOW)
    expect(none.lines).toEqual(['Nothing has been searched or opened.'])
    expect(none.controls).toEqual([])
  })
})

describe('sources', () => {
  it('lists the pages Lumi actually opened, once each, and no search results', () => {
    const research = view({
      status: 'COMPLETED',
      observations: [
        observation({ ref: 'o1', sequence: 1, kind: 'search_results', finalUrl: undefined, finalHost: undefined, blocks: [], tab: undefined }),
        observation({ ref: 'o2', sequence: 2 }),
        observation({ ref: 'o3', sequence: 3 }),
        observation({ ref: 'o4', sequence: 4, finalUrl: 'https://github.com/satish9177/Lumi/blob/main/README.md', finalHost: 'github.com' })
      ]
    })
    const sources = researchSources(research)
    expect(sources.map((source) => source.url)).toEqual([
      'https://github.com/satish9177/Lumi',
      'https://github.com/satish9177/Lumi/blob/main/README.md'
    ])
    expect(researchPageCount(research)).toBe(2)
  })
})

describe('the conversation line', () => {
  it('names the hosts an answer came from', () => {
    const line = describeResearchForConversation(view({
      status: 'COMPLETED',
      observations: [observation()],
      answer: {
        status: 'answered', stopReason: 'goal_reached', answer: 'It is a desktop companion.',
        evidence: [], provider: 'gemini', model: 'gemini-2.5-flash',
        stepsUsed: 2, observationsUsed: 1, plannerCalls: 3, createdAt: '2026-09-18T10:01:00+00:00'
      }
    }))
    expect(line).toContain('1 public page')
    expect(line).toContain('github.com')
    expect(line).toContain('It is a desktop companion.')
  })

  it('says nothing while the task is still running', () => {
    expect(describeResearchForConversation(view({ status: 'ACTIVE', observations: [observation()] }))).toBeUndefined()
  })

  it('points at the card while permission is pending', () => {
    expect(describeResearchForConversation(view({ status: 'PENDING' }))).toContain('needs your permission')
  })
})

describe('the timeline', () => {
  it('describes each research event in Lumi’s words', () => {
    const at = '2026-09-18T10:00:00+00:00'
    const event = (type: string, extra: Record<string, unknown> = {}) =>
      describeEvent({ sequence: 1, type: type as never, taskRevision: 1, createdAt: at, ...extra })
    expect(event('task.research_scope_requested')).toContain('nothing searched yet')
    expect(event('task.research_scope_granted')).toContain('allowed public research')
    expect(event('task.research_scope_revoked')).toContain('withdrawn')
    expect(event('action.authorized')).toContain('permission you gave')
    expect(event('task.research_answer_recorded', { answerStatus: 'answered' })).toContain('Answer recorded')
    expect(event('task.research_answer_recorded', { answerStatus: 'not_found' })).toContain('could not verify')
  })
})
