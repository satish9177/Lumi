import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'
import { TransferController, type TransferConfirmation } from './transfer-controller'
import { parseTransfer } from './transfer-wire'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import type { RuntimeMethod, RuntimeReply } from './agent-runtime-supervisor'
import { TransferCard } from '../../renderer/src/components/TransferPanel'
import type { AgentTransferView } from '../../shared/transfer-contracts'

const TASK = '11111111-2222-4333-8444-555555555555'
const ROOT = '22222222-2222-4333-8444-555555555555'
const GRANT = '33333333-2222-4333-8444-555555555555'
const TRANSFER = '44444444-2222-4333-8444-555555555555'
const T = '2026-09-23T10:00:00Z'
const URL_OK = 'http://127.0.0.1:8899/files/resume.pdf'

interface Call { method: RuntimeMethod; path: string; body: unknown }

function requester(reply: (call: Call) => RuntimeReply | Promise<RuntimeReply>): { runtime: DesktopRuntimeRequester; calls: Call[] } {
  const calls: Call[] = []
  return {
    calls,
    runtime: { request: async (method, path, body) => { const call = { method, path, body }; calls.push(call); return reply(call) } }
  }
}

const ok = (body: unknown): RuntimeReply => ({ status: 200, body }) as RuntimeReply

function transferBody(phase = 'awaiting_approval', overrides: Record<string, unknown> = {}, cardOverrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_id: TASK, task_status: 'WAITING_APPROVAL', task_revision: 2, transfer_id: TRANSFER, phase, status: 'PENDING',
    error_code: null, download_status: null, place_status: null, length: null, sha256: null, kind: null,
    dest_root_label: 'Downloads', dest_name: 'resume.pdf',
    card: {
      grant_id: GRANT, grant_revision: 1, grant_status: 'PENDING', expires_at: T, source_url: URL_OK,
      source_origin: 'http://127.0.0.1:8899', intent: 'My synthetic resume', dest_root_id: ROOT, dest_root_label: 'Downloads',
      dest_name: 'resume.pdf', expected_kind: 'pdf', max_bytes: 10485760, overwrite: false, ...cardOverrides
    },
    ...overrides
  }
}

function controller(reply: (call: Call) => RuntimeReply | Promise<RuntimeReply>, allow = true): {
  transfers: TransferController; calls: Call[]; confirmations: TransferConfirmation[]
} {
  const { runtime, calls } = requester(reply)
  const confirmations: TransferConfirmation[] = []
  const transfers = new TransferController({ runtime, confirmTransfer: async (card) => { confirmations.push(card); return allow } })
  return { transfers, calls, confirmations }
}

describe('TransferController (M10 S2)', () => {
  it('sends only the URL, folder id, name and intent; never a path or an overwrite flag', async () => {
    const { transfers, calls } = controller(() => ok(transferBody()))
    const result = await transfers.createTransfer(URL_OK, ROOT, 'resume.pdf', 'My synthetic resume')
    expect(result.ok).toBe(true)
    expect(calls).toEqual([{ method: 'POST', path: '/transfers', body: { url: URL_OK, root_id: ROOT, file_name: 'resume.pdf', intent: 'My synthetic resume' } }])
  })

  it.each([
    ['C:\\Users\\me\\evil.pdf'], ['..\\escape.pdf'], ['sub/evil.pdf'], ['resume.pdf:stream'], ['\\\\server\\share\\x.pdf']
  ])('refuses a destination that is not just a name: %s', async (name) => {
    const { transfers, calls } = controller(() => ok(transferBody()))
    const result = await transfers.createTransfer(URL_OK, ROOT, name, 'x')
    expect(result.ok).toBe(false)
    expect(calls).toHaveLength(0)
  })

  it.each([['file:///C:/Windows/win.ini'], ['javascript:alert(1)'], ['https://user:pw@example.com/x.pdf'], ['not a url']])(
    'refuses a non-web or credentialed address: %s', async (url) => {
      const { transfers, calls } = controller(() => ok(transferBody()))
      expect((await transfers.createTransfer(url, ROOT, 'x.pdf', 'x')).ok).toBe(false)
      expect(calls).toHaveLength(0)
    })

  it('re-confirms the approval with main’s native dialog, built from the runtime’s record', async () => {
    const { transfers, calls, confirmations } = controller((call) => ok(call.path.endsWith('/grant') ? transferBody('approved') : transferBody()))
    const result = await transfers.grantTransfer(TASK, GRANT, 1)
    expect(result.ok).toBe(true)
    expect(confirmations).toEqual([{
      sourceUrl: URL_OK, sourceOrigin: 'http://127.0.0.1:8899', destRootLabel: 'Downloads', destName: 'resume.pdf', expectedKind: 'pdf', maxBytes: 10485760
    }])
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([`GET /transfers/${TASK}`, `POST /transfers/${TASK}/grant`])
  })

  it('does not approve when the native dialog is cancelled', async () => {
    const { transfers, calls } = controller(() => ok(transferBody()), false)
    const result = await transfers.grantTransfer(TASK, GRANT, 1)
    expect(result).toEqual({ ok: true, value: null })
    expect(calls.some((call) => call.path.endsWith('/grant'))).toBe(false)
  })

  it('refuses a stale approval revision before showing any dialog', async () => {
    const { transfers, confirmations } = controller(() => ok(transferBody('awaiting_approval', {}, { grant_revision: 3 })))
    const result = await transfers.grantTransfer(TASK, GRANT, 1)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error).toMatchObject({ code: 'transfer_state_changed', currentRevision: 3 })
    expect(confirmations).toHaveLength(0)
  })

  it('never lets a double click become two downloads', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    const { transfers, calls } = controller(async () => { await gate; return ok(transferBody('quarantined', { status: 'QUARANTINED' })) })
    const first = transfers.downloadTransfer(TASK)
    const second = await transfers.downloadTransfer(TASK)
    expect(second.ok).toBe(false)
    if (!second.ok) expect(second.error.code).toBe('busy')
    // Nor can a placement start while the download is in flight.
    expect((await transfers.placeTransfer(TASK)).ok).toBe(false)
    release()
    expect((await first).ok).toBe(true)
    expect(calls.filter((call) => call.path.endsWith('/download'))).toHaveLength(1)
    expect(calls.some((call) => call.path.endsWith('/place'))).toBe(false)
  })

  it('projects the effect lock and the runtime reasons without leaking anything else', async () => {
    const { transfers } = controller(() => ({ status: 409, body: { error: { code: 'effect_locked', message: 'x', reason: 'same_effect_unresolved' } } }) as RuntimeReply)
    const result = await transfers.downloadTransfer(TASK)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('effect_locked')
    const refused = controller(() => ({ status: 422, body: { error: { code: 'transfer_refused', message: 'C:\\secret', reason: 'destination_exists' } } }) as RuntimeReply)
    const answer = await refused.transfers.createTransfer(URL_OK, ROOT, 'resume.pdf', 'x')
    expect(answer.ok).toBe(false)
    if (!answer.ok) {
      expect(answer.error.code).toBe('transfer_refused')
      expect(answer.error.message).toContain('never replaces')
      expect(answer.error.message).not.toContain('C:\\')
    }
  })
})

describe('the transfer wire', () => {
  it('rejects a response that carries an absolute path anywhere', () => {
    expect(() => parseTransfer(transferBody('placed', { dest_name: 'C:\\Users\\me\\resume.pdf' }))).toThrow()
    expect(() => parseTransfer(transferBody('placed', {}, { dest_root_label: '\\\\?\\C:\\quarantine' }))).toThrow()
  })

  it('rejects a card that claims it may overwrite', () => {
    expect(() => parseTransfer(transferBody('awaiting_approval', {}, { overwrite: true }))).toThrow()
  })

  it('rejects an unknown phase or kind', () => {
    expect(() => parseTransfer(transferBody('opened'))).toThrow()
    expect(() => parseTransfer(transferBody('placed', { kind: 'exe' }))).toThrow()
  })
})

describe('the trusted transfer card', () => {
  const view = (overrides: Partial<AgentTransferView> = {}): AgentTransferView => ({ ...parseTransfer(transferBody()), ...overrides })
  const render = (v: AgentTransferView): string => renderToStaticMarkup(createElement(TransferCard, {
    transfer: v, busy: false, onAllow: () => undefined, onDecline: () => undefined, onDownload: () => undefined,
    onPlace: () => undefined, onReconcile: () => undefined
  }))

  it('names the source, folder, name and says it never replaces a file', () => {
    const html = render(view())
    expect(html).toContain(URL_OK)
    expect(html).toContain('resume.pdf')
    expect(html).toContain('Never')
    expect(html).toContain('data-testid="transfer-allow"')
  })

  it('renders a hostile intent inertly', () => {
    const base = view()
    const html = render({ ...base, card: { ...base.card!, intent: '<script>alert(1)</script>' } })
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('offers only “Check what happened” when a step is uncertain', () => {
    for (const phase of ['download_unknown', 'placement_unknown'] as const) {
      const html = render(view({ phase }))
      expect(html).toContain('data-testid="transfer-reconcile"')
      expect(html).not.toContain('data-testid="transfer-download"')
      expect(html).not.toContain('data-testid="transfer-place"')
      expect(html).not.toContain('data-testid="transfer-allow"')
    }
  })
})
