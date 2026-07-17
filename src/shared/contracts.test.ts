import { describe, expect, it } from 'vitest'
import { extractSignals, parseToolProposal } from './contracts'

const sourceContext = {
  captureId: 'capture-1',
  summary: 'Interview invitation with a preparation link.',
  capturedAt: '2026-07-17T10:00:00.000Z',
  signals: [{ kind: 'date', label: 'Date', value: 'July 20, 2026' }]
}

describe('parseToolProposal', () => {
  it('accepts a confirmed reminder proposal with source context', () => {
    const proposal = parseToolProposal({
      id: 'proposal-1',
      toolName: 'create_reminder',
      reason: 'The interview needs preparation.',
      requiresConfirmation: true,
      arguments: {
        title: 'Prepare for interview',
        dueAt: '2026-07-20T09:00:00.000Z',
        sourceContext
      }
    })

    expect(proposal.toolName).toBe('create_reminder')
    expect(proposal.arguments).toMatchObject({ title: 'Prepare for interview' })
  })

  it('rejects an unconfirmed action and unsafe URL', () => {
    expect(() =>
      parseToolProposal({
        id: 'proposal-2',
        toolName: 'open_url',
        reason: 'Open this.',
        requiresConfirmation: false,
        arguments: { url: 'file:///C:/secret.txt' }
      })
    ).toThrow('explicitly require confirmation')

    expect(() =>
      parseToolProposal({
        id: 'proposal-3',
        toolName: 'open_url',
        reason: 'Open this.',
        requiresConfirmation: true,
        arguments: { url: 'file:///C:/secret.txt' }
      })
    ).toThrow('Only http and https URLs')
  })
})

describe('extractSignals', () => {
  it('extracts one date, one link, and a next action', () => {
    const signals = extractSignals(
      'Your interview is July 20, 2026. Please prepare the latest resume. Details: https://example.com/interview'
    )

    expect(signals).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ kind: 'date', value: 'July 20, 2026' }),
        expect.objectContaining({ kind: 'link', value: 'https://example.com/interview' }),
        expect.objectContaining({ kind: 'next_action' })
      ])
    )
  })
})
