import { describe, expect, it } from 'vitest'
import { extractSignals, parseFileSearchRequest, parseToolProposal } from './contracts'

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

  it('rejects an unconfirmed action and dangerous URL schemes', () => {
    expect(() =>
      parseToolProposal({
        id: 'proposal-2',
        toolName: 'open_url',
        reason: 'Open this.',
        requiresConfirmation: false,
        arguments: { url: 'file:///C:/secret.txt' }
      })
    ).toThrow('explicitly require confirmation')

    for (const url of ['file:///C:/secret.txt', 'javascript:alert(1)', 'data:text/html,unsafe', 'mailto:test@example.com']) {
      expect(() =>
        parseToolProposal({
          id: `proposal-${url}`,
          toolName: 'open_url',
          reason: 'Open this.',
          requiresConfirmation: true,
          arguments: { url }
        })
      ).toThrow('Only http and https URLs')
    }
  })

  it('normalizes reminder due dates to an instant while retaining the submitted timezone meaning', () => {
    const proposal = parseToolProposal({
      id: 'proposal-timezone',
      toolName: 'create_reminder',
      reason: 'The interview needs preparation.',
      requiresConfirmation: true,
      arguments: {
        title: 'Prepare for interview',
        dueAt: '2026-07-20T09:00:00+05:30',
        sourceContext
      }
    })

    expect(proposal.toolName).toBe('create_reminder')
    if (proposal.toolName === 'create_reminder') {
      expect(proposal.arguments.dueAt).toBe('2026-07-20T03:30:00.000Z')
    }
  })

  it('accepts only the closed Telegram attachment schema and preserves the caption exactly', () => {
    const proposal = parseToolProposal({
      id: 'attachment', toolName: 'send_telegram_attachment', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'recipient-id', fileResultId: 'file-id', caption: '  exact caption  ' }
    })
    expect(proposal.toolName).toBe('send_telegram_attachment')
    if (proposal.toolName === 'send_telegram_attachment') expect(proposal.arguments.caption).toBe('  exact caption  ')

    const boundary = parseToolProposal({
      id: 'attachment-caption-boundary', toolName: 'send_telegram_attachment', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'recipient-id', fileResultId: 'file-id', caption: 'x'.repeat(1_024) }
    })
    expect(boundary.toolName === 'send_telegram_attachment' && boundary.arguments.caption?.length).toBe(1_024)

    expect(() => parseToolProposal({
      id: 'attachment-extra', toolName: 'send_telegram_attachment', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'recipient-id', fileResultId: 'file-id', path: 'C:\\secret.pdf' }
    })).toThrow(/unsupported properties/i)
    expect(() => parseToolProposal({
      id: 'attachment-caption', toolName: 'send_telegram_attachment', reason: 'Send it.', requiresConfirmation: true,
      arguments: { recipientResultId: 'recipient-id', fileResultId: 'file-id', caption: 'x'.repeat(1_025) }
    })).toThrow(/1024/i)
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

describe('semantic search contract', () => {
  it('accepts up to three short concepts and rejects unknown or path-like fields', () => {
    expect(parseFileSearchRequest({ queryTerms: 'beach', kind: 'photo', concepts: ['beach'], origin: 'user' }).concepts).toEqual(['beach'])
    expect(() => parseFileSearchRequest({ queryTerms: 'beach', concepts: ['a', 'b', 'c', 'd'], origin: 'user' })).toThrow(/one to three/i)
    expect(() => parseFileSearchRequest({ queryTerms: 'beach', concepts: ['C:\\Photos'], origin: 'user' })).toThrow(/natural-language/i)
    expect(() => parseFileSearchRequest({ queryTerms: 'beach', concepts: ['beach'], embedding: [1], origin: 'user' })).toThrow(/unsupported/i)
  })
})
