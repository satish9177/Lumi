import { describe, expect, it } from 'vitest'
import { classifyUserIntent, evaluateGuardedToolRequest, normalizeUserRequest } from './intent'

describe('classifyUserIntent', () => {
  it.each([
    'Find my latest resume',
    'Locate my CV',
    'Find the newest PDF',
    'Open my resume',
    'Search for my certificate',
    'Where is my offer letter document?'
  ])('classifies "%s" as local_file_search', (request) => {
    expect(classifyUserIntent(request).intent).toBe('local_file_search')
  })

  it.each([
    'What is this email about?',
    'Explain this visible image',
    'Explain this image on my screen',
    'What is on my screen?',
    'Look at this error',
    'What does this page say?'
  ])('classifies "%s" as visible_screen_question', (request) => {
    expect(classifyUserIntent(request).intent).toBe('visible_screen_question')
  })

  it.each([
    'Find my newest screenshot',
    'Find my latest screen shot',
    'Open my screen capture from yesterday',
    'Find the photo of Ravi at the beach',
    'Find my pictures from Goa'
  ])('routes the stored-image request "%s" to local file search, not screen capture', (request) => {
    expect(classifyUserIntent(request).intent).toBe('local_file_search')
  })

  it.each([
    'Is this message a scam?',
    'Check this email for a scam',
    'Is this payment link suspicious?',
    'Can I trust this message?',
    'Check this WhatsApp message for fraud',
    'Is this a phishing email?',
    'Is this sender impersonating my bank?'
  ])('classifies "%s" as scam_check', (request) => {
    expect(classifyUserIntent(request).intent).toBe('scam_check')
  })

  it('needs both a scam cue and something to check, so it stays narrow', () => {
    // A bare "check this email" is deliberately *not* a scam check: it is the
    // ordinary screen brief, and hijacking it would change what an existing
    // request does.
    expect(classifyUserIntent('Check this email').intent).toBe('visible_screen_question')
    // A general question about scams is not a request to review the screen.
    expect(classifyUserIntent('How do phone scams usually work?').intent).toBe('general_question')
    // And a reminder about a scam is still a reminder.
    expect(classifyUserIntent('Remind me to report that scam call tomorrow').intent).toBe('reminder')
  })

  it('leaves an ordinary screen question alone', () => {
    expect(classifyUserIntent('What is this email about?').intent).toBe('visible_screen_question')
    expect(classifyUserIntent('Summarise this message').intent).toBe('visible_screen_question')
  })

  it('still treats a deictic image request as a question about the visible screen', () => {
    expect(classifyUserIntent('Explain this image on my screen').intent).toBe('visible_screen_question')
    expect(classifyUserIntent('What is this picture showing?').intent).toBe('visible_screen_question')
  })

  it('keeps stored-file requests away from screen capture even when a file noun is generic', () => {
    const classified = classifyUserIntent('Find the newest PDF')
    expect(classified.intent).toBe('local_file_search')
    expect(classified.fileQuery).toBe('pdf')
  })

  it('classifies reminder requests as reminder', () => {
    expect(classifyUserIntent('Remind me to submit the form tomorrow').intent).toBe('reminder')
    expect(classifyUserIntent('Set a reminder for the interview').intent).toBe('reminder')
  })

  it('classifies link-opening requests as open_target', () => {
    expect(classifyUserIntent('Open https://example.com').intent).toBe('open_target')
    expect(classifyUserIntent('Open the interview prep link').intent).toBe('open_target')
  })

  it('classifies advice questions about a document as general_question', () => {
    expect(classifyUserIntent('How can I improve my resume?').intent).toBe('general_question')
  })

  it('marks the ambiguous "Check my resume" as unknown with the exact clarification question', () => {
    const classified = classifyUserIntent('Check my resume')
    expect(classified.intent).toBe('unknown')
    expect(classified.clarification).toBe('Should I inspect the resume currently visible, or find it in your approved folder?')
  })

  it('treats an ambiguous document request as a screen question when a screen context is active', () => {
    expect(classifyUserIntent('Check my resume', { hasScreenContext: true }).intent).toBe('visible_screen_question')
  })

  it('normalizes the stored request text', () => {
    const classified = classifyUserIntent('  Find   my latest resume  ')
    expect(classified.normalizedRequest).toBe('Find my latest resume')
    expect(normalizeUserRequest('  a  \n b ')).toBe('a b')
  })

  it('returns unknown for an empty request', () => {
    expect(classifyUserIntent('   ').intent).toBe('unknown')
  })
})

describe('evaluateGuardedToolRequest', () => {
  it('rejects capture_screen_context for local-file intent with a recoverable search_documents pointer', () => {
    const decision = evaluateGuardedToolRequest('capture_screen_context', { intent: 'local_file_search', hasApprovedFolder: true })
    expect(decision).toMatchObject({ allowed: false, code: 'use_search_documents' })
    if (!decision.allowed) {
      expect(decision.message).toMatch(/search_documents/)
    }
  })

  it('allows capture_screen_context for a visible-screen question', () => {
    expect(evaluateGuardedToolRequest('capture_screen_context', { intent: 'visible_screen_question', hasApprovedFolder: false })).toEqual({ allowed: true })
  })

  it('requires an approved folder before search_documents', () => {
    expect(evaluateGuardedToolRequest('search_documents', { intent: 'local_file_search', hasApprovedFolder: false })).toMatchObject({ allowed: false, code: 'needs_approved_folder' })
    expect(evaluateGuardedToolRequest('search_documents', { intent: 'local_file_search', hasApprovedFolder: true })).toEqual({ allowed: true })
  })
})
