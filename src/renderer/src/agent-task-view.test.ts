import { describe, expect, it } from 'vitest'
import type { AgentActionView, AgentEventView } from '../../shared/agent-contracts'
import { describeBooking, describeEvent, mergeEvents } from './agent-task-view'

const NOW = Date.parse('2026-09-16T10:01:00Z')

function action(overrides: Partial<AgentActionView> = {}): AgentActionView {
  return {
    actionId: '00000000-0000-4000-8000-000000000002',
    taskId: '00000000-0000-4000-8000-000000000001',
    toolName: 'commit_booking',
    status: 'WAITING_APPROVAL',
    revision: 2,
    riskTier: 'R2',
    proposalDigest: 'a'.repeat(64),
    createdAt: '2026-09-16T10:00:00Z',
    updatedAt: '2026-09-16T10:00:00Z',
    booking: { site: 'appointment_fixture', slotId: 'slot-a-1830', doctor: 'Dr A', time: '2026-09-19T18:30:00+05:30', price: 800, currency: 'INR' },
    approval: {
      approvalId: '00000000-0000-4000-8000-000000000003', status: 'PENDING', actionRevision: 2,
      proposalDigest: 'a'.repeat(64), createdAt: '2026-09-16T10:00:00Z', expiresAt: '2026-09-16T10:05:00Z'
    },
    attempts: [],
    ...overrides
  }
}

function reconciled(result: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN', sequence = 9): AgentEventView {
  return {
    sequence, type: 'action.reconciled', taskRevision: sequence, createdAt: '2026-09-16T10:02:00Z',
    actionId: '00000000-0000-4000-8000-000000000002',
    reconciliation: { result, lookup: result === 'SUCCEEDED' ? 'FOUND' : result === 'FAILED' ? 'NOT_FOUND' : 'UNKNOWN', ...(result === 'SUCCEEDED' ? { bookingId: 'BK-0001' } : {}) }
  }
}

const attempt = (outcome: 'SUCCEEDED' | 'FAILED' | 'OUTCOME_UNKNOWN', result?: AgentActionView['attempts'][number]['result']) => ({
  attemptId: '00000000-0000-4000-8000-000000000004', attemptNumber: 1,
  runtimeGeneration: '00000000-0000-4000-8000-000000000005', startedAt: '2026-09-16T10:00:30Z',
  finishedAt: '2026-09-16T10:00:40Z', outcome, ...(result ? { result } : {})
})

describe('describeBooking', () => {
  it('offers approve and reject for a live approval request, and nothing that edits values', () => {
    const model = describeBooking(action(), [], NOW)
    expect(model.title).toBe('Book appointment')
    expect(model.controls).toEqual(['reject', 'approve_and_book'])
    expect(model.showBookingDetails).toBe(true)
  })

  it('invalidates an expired approval request', () => {
    const model = describeBooking(action(), [], Date.parse('2026-09-16T10:05:00Z'))
    expect(model.controls).toEqual(['discard_and_review'])
    expect(model.controls).not.toContain('approve_and_book')
  })

  it('never offers a retry, approval or booking while the outcome is unknown', () => {
    for (const status of ['OUTCOME_UNKNOWN', 'RECONCILING'] as const) {
      const model = describeBooking(action({ status, approval: undefined, attempts: [attempt('OUTCOME_UNKNOWN')] }), [], NOW)
      expect(model.tone).toBe('uncertain')
      expect(model.controls).toEqual(['check_booking'])
      expect(model.title).not.toMatch(/fail|confirmed|no booking/i)
      expect(model.lines.join(' ')).not.toMatch(/nothing was booked/i)
    }
    const unknown = describeBooking(action({ status: 'OUTCOME_UNKNOWN', approval: undefined }), [], NOW)
    expect(unknown.title).toBe('Booking status uncertain')
    expect(unknown.lines[0]).toContain('may have accepted the booking')
    const checking = describeBooking(action({ status: 'RECONCILING', approval: undefined }), [], NOW)
    expect(checking.lines[0]).toContain('checking, not retrying')
    const stillUnknown = describeBooking(action({ status: 'OUTCOME_UNKNOWN', approval: undefined }), [reconciled('OUTCOME_UNKNOWN')], NOW)
    expect(stillUnknown.controls).toEqual(['check_booking'])
    expect(stillUnknown.lines.join(' ')).toContain('could not get a definite answer')
  })

  it('shows no controls while a booking is executing', () => {
    expect(describeBooking(action({ status: 'EXECUTING', approval: undefined }), [], NOW).controls).toEqual([])
  })

  it('confirms only what execution verified or reconciliation established', () => {
    const verified = describeBooking(action({
      status: 'SUCCEEDED', approval: undefined,
      attempts: [attempt('SUCCEEDED', { dispatchStatus: 'OK', receipt: { bookingId: 'BK-0007', doctor: 'Dr A', price: 800, currency: 'INR' } })]
    }), [], NOW)
    expect(verified).toMatchObject({ title: 'Booking confirmed', tone: 'success', controls: [] })
    expect(verified.lines[0]).toContain('BK-0007')

    const found = describeBooking(action({ status: 'SUCCEEDED', approval: undefined, attempts: [attempt('OUTCOME_UNKNOWN')] }), [reconciled('SUCCEEDED')], NOW)
    expect(found.title).toBe('Booking confirmed')
    expect(found.lines.join(' ')).toContain('found the existing booking BK-0001')
    expect(found.lines.join(' ')).toContain('No second booking')

    const absent = describeBooking(action({ status: 'FAILED', approval: undefined, attempts: [attempt('OUTCOME_UNKNOWN')] }), [reconciled('FAILED')], NOW)
    expect(absent.title).toBe('No booking was created')
    expect(absent.controls).not.toContain('check_booking')
  })

  it('shows approved and current facts after a price change and requires a new review', () => {
    const model = describeBooking(action({
      status: 'FAILED', approval: undefined,
      attempts: [attempt('FAILED', { dispatchStatus: 'CHANGED_RESOURCE', submitted: false, changedFacts: [{ field: 'price', approved: '800', observed: '950' }] })]
    }), [], NOW)
    expect(model.title).toBe('The appointment changed after approval')
    expect(model.lines).toEqual(['Nothing was booked.', 'Review the updated details before trying again.'])
    expect(model.changedFacts).toEqual([{ label: 'Price', approved: '₹800', observed: '₹950' }])
    expect(model.controls).toEqual(['review_updated', 'search_again'])
    expect(model.controls).not.toContain('book_now')
  })

  it('explains a missing slot', () => {
    const model = describeBooking(action({
      status: 'FAILED', approval: undefined,
      attempts: [attempt('FAILED', { dispatchStatus: 'RESOURCE_UNAVAILABLE', submitted: false })]
    }), [], NOW)
    expect(model.title).toBe('This appointment is no longer available')
    expect(model.lines).toEqual(['Nothing was booked.'])
  })
})

describe('timeline helpers', () => {
  const event = (sequence: number, type: AgentEventView['type'], extra: Partial<AgentEventView> = {}): AgentEventView => ({
    sequence, type, taskRevision: sequence, createdAt: '2026-09-16T10:00:00Z', ...extra
  })

  it('merges pages without duplicates and in sequence order', () => {
    const merged = mergeEvents([event(1, 'task.created'), event(2, 'action.proposed')], [event(2, 'action.proposed'), event(4, 'action.approved'), event(3, 'action.approval_requested')])
    expect(merged.map((item) => item.sequence)).toEqual([1, 2, 3, 4])
  })

  it('labels reconciliation as checking, never retrying', () => {
    expect(describeEvent(event(1, 'action.reconciliation_started'))).toContain('not retrying')
    expect(describeEvent(event(2, 'action.outcome_unknown', { reason: 'runtime_restart' }))).toContain('restarted')
    expect(describeEvent(reconciled('SUCCEEDED'))).toContain('existing booking found')
    expect(describeEvent(reconciled('OUTCOME_UNKNOWN'))).toContain('still uncertain')
  })
})
