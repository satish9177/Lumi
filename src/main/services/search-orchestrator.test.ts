import { describe, expect, it, vi } from 'vitest'
import type { ApprovedDocumentRoot, FileSearchResults, PendingSearchResolution } from '../../shared/contracts'
import type { NormalizedSearchQuery } from '../../shared/search-query'
import { SearchOrchestrator, type SearchTimer } from './search-orchestrator'

const APPROVED_ROOT: ApprovedDocumentRoot = { id: 'root-1', label: 'Documents' }

function createResults(overrides: Partial<FileSearchResults> = {}): FileSearchResults {
  return {
    results: [
      {
        id: 'result-1',
        rootId: 'root-1',
        name: 'Resume_2026.pdf',
        relativePath: 'career/Resume_2026.pdf',
        modifiedAt: '2026-07-15T09:00:00.000Z',
        kind: 'document'
      }
    ],
    compactResults: [{ ordinal: 1, name: 'Resume_2026.pdf', modifiedAgo: '3 days ago' }],
    fallback: false,
    message: 'Found 1 matching file.',
    ...overrides
  }
}

interface Harness {
  orchestrator: SearchOrchestrator
  emitted: PendingSearchResolution[]
  searched: NormalizedSearchQuery[]
  roots: ApprovedDocumentRoot[]
  advance: (ms: number) => void
  fireTimer: () => void
  trusted: { value: boolean }
  waitCalls: NormalizedSearchQuery[]
}

function createHarness(options: {
  roots?: ApprovedDocumentRoot[]
  trusted?: boolean
  ttlMs?: number
  /** Stands in for a late voice transcript landing during the grace window. */
  waitForTrust?: (query: NormalizedSearchQuery, trusted: { value: boolean }) => Promise<void> | void
} = {}): Harness {
  const emitted: PendingSearchResolution[] = []
  const searched: NormalizedSearchQuery[] = []
  const waitCalls: NormalizedSearchQuery[] = []
  const roots = options.roots ?? []
  const trusted = { value: options.trusted ?? true }
  let currentTime = 1_000
  let scheduled: (() => void) | undefined

  const orchestrator = new SearchOrchestrator({
    listRoots: async () => roots,
    runSearch: async (query) => {
      searched.push(query)
      return createResults()
    },
    isTrustedIntent: () => trusted.value,
    waitForTrust: options.waitForTrust
      ? async (query) => {
        waitCalls.push(query)
        await options.waitForTrust!(query, trusted)
      }
      : undefined,
    emit: (resolution) => emitted.push(resolution),
    now: () => currentTime,
    ttlMs: options.ttlMs ?? 120_000,
    schedule: (callback): SearchTimer => {
      scheduled = callback
      return { cancel: () => { scheduled = undefined } }
    }
  })

  return {
    orchestrator,
    emitted,
    searched,
    roots,
    trusted,
    waitCalls,
    advance: (ms) => { currentTime += ms },
    fireTimer: () => scheduled?.()
  }
}

describe('SearchOrchestrator', () => {
  it('runs a trusted search immediately when a folder is already approved', async () => {
    const harness = createHarness({ roots: [APPROVED_ROOT] })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    expect(outcome.status).toBe('completed')
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
    // The caller answers the call itself, so nothing is emitted separately.
    expect(harness.emitted).toEqual([])
  })

  it('accepts a search request that carries no root identifier at all', async () => {
    const harness = createHarness({ roots: [APPROVED_ROOT] })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'find my latest resume', origin: 'model' })

    expect(outcome.status).toBe('completed')
    expect(harness.searched[0]).toMatchObject({ terms: ['resume'], recency: 'latest' })
  })

  it('retains exactly one pending search when no folder is approved yet', async () => {
    const harness = createHarness()

    const outcome = await harness.orchestrator.begin({ queryTerms: 'latest resume', origin: 'model', callId: 'call-1' })

    expect(outcome.status).toBe('awaiting_folder')
    expect(harness.orchestrator.pendingSearch()).toMatchObject({ callId: 'call-1' })
    expect(harness.orchestrator.pendingSearch()?.query.terms).toEqual(['resume'])
    // No terminal result yet: the Realtime call stays open on purpose.
    expect(harness.emitted).toEqual([])
    expect(harness.searched).toEqual([])
  })

  it('keeps the retained query immutable', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    const pending = harness.orchestrator.pendingSearch()!
    expect(Object.isFrozen(pending)).toBe(true)
    expect(Object.isFrozen(pending.query)).toBe(true)
  })

  it('resumes the original search automatically once a folder is approved', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'find my latest resume', origin: 'model', callId: 'call-1' })

    harness.roots.push(APPROVED_ROOT)
    await harness.orchestrator.notifyFolderApproved()

    expect(harness.searched).toHaveLength(1)
    expect(harness.searched[0]).toMatchObject({ terms: ['resume'], recency: 'latest' })
    expect(harness.emitted).toEqual([
      expect.objectContaining({ status: 'completed', callId: 'call-1' })
    ])
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
  })

  it('emits exactly one terminal result for a call id even if approval repeats', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })
    harness.roots.push(APPROVED_ROOT)

    await harness.orchestrator.notifyFolderApproved()
    await harness.orchestrator.notifyFolderApproved()
    harness.orchestrator.notifyFolderDeclined()
    harness.orchestrator.sweep()

    expect(harness.emitted).toHaveLength(1)
    expect(harness.searched).toHaveLength(1)
  })

  it('terminates the held call when the user cancels the folder chooser', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    harness.orchestrator.notifyFolderDeclined()

    expect(harness.emitted).toEqual([
      expect.objectContaining({ status: 'declined', callId: 'call-1' })
    ])
    expect(harness.searched).toEqual([])
  })

  it('expires a stale pending search and never runs it afterwards', async () => {
    const harness = createHarness({ ttlMs: 120_000 })
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    harness.advance(120_001)
    harness.fireTimer()
    harness.roots.push(APPROVED_ROOT)
    await harness.orchestrator.notifyFolderApproved()

    expect(harness.emitted).toEqual([
      expect.objectContaining({ status: 'expired', callId: 'call-1' })
    ])
    expect(harness.searched).toEqual([])
  })

  it('refuses to resume an expired search even without a timer sweep', async () => {
    const harness = createHarness({ ttlMs: 120_000 })
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    harness.advance(120_001)
    harness.roots.push(APPROVED_ROOT)
    await harness.orchestrator.notifyFolderApproved()

    expect(harness.searched).toEqual([])
    expect(harness.emitted).toEqual([expect.objectContaining({ status: 'expired' })])
  })

  it('lets a newer request supersede an older pending search', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })
    await harness.orchestrator.begin({ queryTerms: 'certificate', origin: 'model', callId: 'call-2' })

    expect(harness.emitted).toEqual([
      expect.objectContaining({ status: 'superseded', callId: 'call-1' })
    ])
    expect(harness.orchestrator.pendingSearch()).toMatchObject({ callId: 'call-2' })

    harness.roots.push(APPROVED_ROOT)
    await harness.orchestrator.notifyFolderApproved()

    expect(harness.searched[0]?.terms).toEqual(['certificate'])
  })

  it('drops pending work silently on disconnect or shutdown', async () => {
    const harness = createHarness()
    await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })

    harness.orchestrator.clear()
    harness.roots.push(APPROVED_ROOT)
    await harness.orchestrator.notifyFolderApproved()
    harness.fireTimer()

    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
    expect(harness.emitted).toEqual([])
    expect(harness.searched).toEqual([])
  })

  it('fails closed to a confirmation card when no fresh user intent supports the search', async () => {
    const harness = createHarness({ roots: [APPROVED_ROOT], trusted: false })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'passwords', origin: 'model', callId: 'call-1' })

    expect(outcome).toMatchObject({ status: 'needs_confirmation', input: { queryTerms: 'passwords' } })
    expect(harness.searched).toEqual([])
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
  })

  it('never opens a folder chooser for an untrusted model request', async () => {
    const harness = createHarness({ trusted: false })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'model' })

    expect(outcome.status).toBe('needs_confirmation')
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
  })

  it('resumes without a confirmation card when a late transcript arrives during the grace window', async () => {
    // The transcript lands mid-wait, so the model request becomes trusted and
    // is held for folder approval rather than routed to a confirmation card.
    const harness = createHarness({
      trusted: false,
      waitForTrust: (_query, trusted) => { trusted.value = true }
    })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'find my latest resume', origin: 'model', callId: 'call-1' })

    expect(harness.waitCalls).toHaveLength(1)
    expect(outcome).toMatchObject({ status: 'awaiting_folder' })
    expect(harness.orchestrator.pendingSearch()).toMatchObject({ callId: 'call-1' })
    expect(harness.orchestrator.pendingSearch()?.query.terms).toEqual(['resume'])
  })

  it('fails closed to a confirmation card when the grace window ends without a transcript', async () => {
    // A suspicious model-initiated search whose intent never arrives must still
    // require an explicit confirmation and never open the folder chooser.
    const harness = createHarness({
      trusted: false,
      waitForTrust: () => undefined
    })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'passwords', origin: 'model', callId: 'call-1' })

    expect(harness.waitCalls).toHaveLength(1)
    expect(outcome).toMatchObject({ status: 'needs_confirmation', input: { queryTerms: 'passwords' } })
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
    expect(harness.searched).toEqual([])
  })

  it('skips the grace window entirely for an explicit in-app request', async () => {
    const harness = createHarness({
      roots: [APPROVED_ROOT],
      trusted: false,
      waitForTrust: () => undefined
    })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'user' })

    expect(harness.waitCalls).toEqual([])
    expect(outcome.status).toBe('completed')
  })

  it('treats an explicit in-app request as its own consent', async () => {
    const harness = createHarness({ roots: [APPROVED_ROOT], trusted: false })

    const outcome = await harness.orchestrator.begin({ queryTerms: 'resume', origin: 'user' })

    expect(outcome.status).toBe('completed')
    expect(harness.searched).toHaveLength(1)
  })

  it('reports an invalid request without creating pending state', async () => {
    const harness = createHarness({ roots: [APPROVED_ROOT] })

    const outcome = await harness.orchestrator.begin({ queryTerms: '   ', origin: 'model', callId: 'call-1' })

    expect(outcome.status).toBe('failed')
    expect(harness.orchestrator.pendingSearch()).toBeUndefined()
  })

  it('reports a search failure instead of leaving the call unanswered', async () => {
    const emitted: PendingSearchResolution[] = []
    const roots: ApprovedDocumentRoot[] = []
    const orchestrator = new SearchOrchestrator({
      listRoots: async () => roots,
      runSearch: vi.fn(async () => { throw new Error('Approved folder is unavailable.') }),
      isTrustedIntent: () => true,
      emit: (resolution) => emitted.push(resolution)
    })

    await orchestrator.begin({ queryTerms: 'resume', origin: 'model', callId: 'call-1' })
    roots.push(APPROVED_ROOT)
    await orchestrator.notifyFolderApproved()

    expect(emitted).toEqual([
      expect.objectContaining({ status: 'failed', callId: 'call-1', message: expect.stringMatching(/unavailable/i) })
    ])
  })
})
