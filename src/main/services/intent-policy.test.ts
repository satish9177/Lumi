import { describe, expect, it } from 'vitest'
import { normalizeSearchQuery } from '../../shared/search-query'
import { IntentTracker } from './intent-policy'

const query = (terms: string) => normalizeSearchQuery({ queryTerms: terms })

describe('IntentTracker', () => {
  it('stores the latest normalized request with its classified intent', () => {
    const tracker = new IntentTracker()
    const classified = tracker.noteUserRequest('  Find   my latest resume ')

    expect(classified).toMatchObject({ normalizedRequest: 'Find my latest resume', intent: 'local_file_search' })
    expect(tracker.currentIntent()).toBe('local_file_search')
  })

  it('blocks capture_screen_context while the current intent is local_file_search', () => {
    const tracker = new IntentTracker()
    tracker.noteUserRequest('Find my latest resume')

    const decision = tracker.evaluateToolRequest('capture_screen_context', true)

    expect(decision).toMatchObject({ allowed: false, code: 'use_search_documents' })
  })

  it('allows capture_screen_context for visible-screen questions', () => {
    const tracker = new IntentTracker()
    tracker.noteUserRequest('What is this email about?')

    expect(tracker.evaluateToolRequest('capture_screen_context', true)).toEqual({ allowed: true })
  })

  it('lets a stale intent expire so later voice-only requests are not blocked', () => {
    let currentTime = 0
    const tracker = new IntentTracker(() => currentTime, 1_000)
    tracker.noteUserRequest('Find my latest resume')

    currentTime = 2_000

    expect(tracker.currentIntent()).toBe('unknown')
    expect(tracker.evaluateToolRequest('capture_screen_context', true)).toEqual({ allowed: true })
  })

  it('trusts a search whose terms match the request the user actually made', () => {
    const tracker = new IntentTracker()
    tracker.noteUserRequest('Find my latest resume')

    expect(tracker.supportsFileSearch(query('resume'))).toBe(true)
    expect(tracker.supportsFileSearch(query('latest resume'))).toBe(true)
    // The synonym group links the spoken noun to the model's chosen wording.
    expect(tracker.supportsFileSearch(query('cv'))).toBe(true)
  })

  it('fails closed for an unrelated, stale, or absent request', () => {
    let currentTime = 0
    const tracker = new IntentTracker(() => currentTime, 1_000)

    // Nothing noted yet.
    expect(tracker.supportsFileSearch(query('resume'))).toBe(false)

    tracker.noteUserRequest('Find my latest resume')
    expect(tracker.supportsFileSearch(query('tax passwords'))).toBe(false)

    currentTime = 2_000
    expect(tracker.supportsFileSearch(query('resume'))).toBe(false)
  })

  it('does not trust a search that follows a screen question', () => {
    const tracker = new IntentTracker()
    tracker.noteUserRequest('What is this email about?')

    expect(tracker.supportsFileSearch(query('resume'))).toBe(false)
  })

  it('reports a missing approved folder for search_documents', () => {
    const tracker = new IntentTracker()
    tracker.noteUserRequest('Open my resume')

    expect(tracker.evaluateToolRequest('search_documents', false)).toMatchObject({ allowed: false, code: 'needs_approved_folder' })
    expect(tracker.evaluateToolRequest('search_documents', true)).toEqual({ allowed: true })
  })
})
