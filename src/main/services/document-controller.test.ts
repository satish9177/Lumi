import { describe, expect, it } from 'vitest'
import { DocumentController } from './document-controller'
import type { DocumentComparer, DocumentCompareOutcome } from '../agent/document-comparer'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import type { RuntimeMethod, RuntimeReply } from './agent-runtime-supervisor'
import type { DroppedFileLookup, DroppedFileSnapshot } from './dropped-files'

const TASK = '11111111-2222-4333-8444-555555555555'
const ROOT = '22222222-2222-4333-8444-555555555555'
const GRANT = '33333333-2222-4333-8444-555555555555'
const DOC_A = '44444444-2222-4333-8444-555555555555'
const DOC_B = '55555555-2222-4333-8444-555555555555'
const DROPPED = '66666666-2222-4333-8444-555555555555'
const DISCLOSURE = '77777777-2222-4333-8444-555555555555'
const T = '2026-09-23T10:00:00Z'

interface Call { method: RuntimeMethod; path: string; body: unknown }

function requester(reply: (call: Call) => RuntimeReply | Promise<RuntimeReply>): { runtime: DesktopRuntimeRequester; calls: Call[] } {
  const calls: Call[] = []
  return {
    calls,
    runtime: { request: async (method, path, body) => { const call = { method, path, body }; calls.push(call); return reply(call) } }
  }
}

const ok = (body: unknown): RuntimeReply => ({ status: 200, body }) as RuntimeReply

function rootBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { root_id: ROOT, label: 'Resumes', can_read: true, can_create: false, can_modify: false, revision: 1, created_at: T, ...overrides }
}

function taskBody(phase = 'local', extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_id: TASK, task_status: 'READY', task_revision: 3, objective: '', phase, files: [], documents: [],
    card: null, disclosure: null, answer: null, ...extra
  }
}

function approvedTask(): Record<string, unknown> {
  return taskBody('approved', {
    card: {
      grant_id: GRANT, grant_revision: 2, grant_status: 'ACTIVE', expires_at: T, recipient: 'gemini', model: 'gemini-2.5-flash',
      purpose: 'How well do I match?', documents: [{ doc_ref: 'd1', document_id: DOC_A, label: 'resume.pdf', excerpt: 'Senior Python engineer' }],
      max_excerpt_bytes: 6144, text_bytes: 22, redaction_count: 0, truncated: false, redaction_policy: 'identifier-redaction-v1'
    }
  })
}

function claimBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    disclosure_id: DISCLOSURE, task_id: TASK, purpose: 'How well do I match?', recipient: 'gemini', model: 'gemini-2.5-flash',
    projection: {
      schema_version: 1, classification: 'document_private', trust: 'untrusted_environment', purpose: 'How well do I match?',
      documents: [{ doc_ref: 'd1', excerpt: 'Senior Python engineer', truncated: false }]
    },
    ...overrides
  }
}

class FakeComparer {
  calls = 0
  serve = true
  outcome: DocumentCompareOutcome = {
    kind: 'result', provider: 'gemini', model: 'gemini-2.5-flash',
    result: { schema_version: 1, kind: 'cannot_compare', reason: 'not_in_documents' }
  }
  candidate(): { recipient: 'gemini'; model: string } { return { recipient: 'gemini', model: 'gemini-2.5-flash' } }
  canServe(): boolean { return this.serve }
  async compare(): Promise<DocumentCompareOutcome> { this.calls += 1; return this.outcome }
}

function controller(runtime: DesktopRuntimeRequester, options: {
  comparer?: FakeComparer
  folder?: string
  dropped?: DroppedFileLookup
} = {}): DocumentController {
  return new DocumentController({
    runtime,
    comparer: (options.comparer ?? new FakeComparer()) as unknown as DocumentComparer,
    chooseFolder: async () => options.folder,
    droppedFiles: options.dropped
  })
}

describe('M10 S1 file roots', () => {
  it('takes the folder from the native dialog, never from the renderer', async () => {
    const { runtime, calls } = requester(() => ok(rootBody()))
    const result = await controller(runtime, { folder: 'C:\\Users\\alex\\Resumes' }).addFileRoot('Resumes', true, false)
    expect(result.ok).toBe(true)
    expect(calls).toEqual([{
      method: 'POST', path: '/file-roots',
      body: { path: 'C:\\Users\\alex\\Resumes', label: 'Resumes', can_read: true, can_create: false, can_modify: false }
    }])
    // The view the renderer gets back carries no path.
    expect(JSON.stringify(result)).not.toContain('Users')
  })

  it('does nothing when the person cancels the dialog', async () => {
    const { runtime, calls } = requester(() => ok(rootBody()))
    const result = await controller(runtime, { folder: undefined }).addFileRoot('Resumes', true, true)
    expect(result).toEqual({ ok: true, value: null })
    expect(calls).toHaveLength(0)
  })

  it('refuses a request with no permission, or a non-boolean one, before the dialog', async () => {
    const { runtime, calls } = requester(() => ok(rootBody()))
    const documents = controller(runtime, { folder: 'C:\\x' })
    expect((await documents.addFileRoot('X', false, false)).ok).toBe(false)
    expect((await documents.addFileRoot('X', 'yes', false)).ok).toBe(false)
    expect(calls).toHaveLength(0)
  })

  it('refuses a runtime response that would carry an absolute path to the renderer', async () => {
    const { runtime } = requester(() => ok({ roots: [rootBody({ label: 'C:\\Users\\alex' })] }))
    const result = await controller(runtime).listFileRoots()
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error.code).toBe('invalid_response')
  })
})

describe('M10 S1 document files', () => {
  it.each(['C:\\Windows\\win.ini', '\\\\server\\share\\a.pdf', '../outside.pdf', 'a/../../b.pdf', 'resume.pdf:ads', '/etc/passwd', ''])(
    'refuses the relative name %j before any runtime call', async (relative) => {
      const { runtime, calls } = requester(() => ok(taskBody()))
      const result = await controller(runtime).addDocumentFromRoot(TASK, ROOT, relative)
      expect(result.ok).toBe(false)
      expect(calls).toHaveLength(0)
    })

  it('resolves a dropped file from main’s own store by its opaque id', async () => {
    const { runtime, calls } = requester(() => ok(taskBody()))
    const dropped: DroppedFileLookup = {
      snapshot: (id) => (id === DROPPED ? { fileName: 'resume.pdf' } as DroppedFileSnapshot : undefined),
      resolve: async (id) => (id === DROPPED ? 'C:\\Users\\alex\\Downloads\\resume.pdf' : undefined),
      wasInvalidated: () => false
    }
    const result = await controller(runtime, { dropped }).addDroppedDocument(TASK, DROPPED)
    expect(result.ok).toBe(true)
    expect(calls[0]).toEqual({
      method: 'POST', path: `/document-tasks/${TASK}/dropped-files`,
      body: { path: 'C:\\Users\\alex\\Downloads\\resume.pdf', display_name: 'resume.pdf' }
    })
  })

  it('an unknown or expired dropped id reaches nothing', async () => {
    const { runtime, calls } = requester(() => ok(taskBody()))
    const dropped: DroppedFileLookup = { snapshot: () => undefined, resolve: async () => undefined, wasInvalidated: () => true }
    const result = await controller(runtime, { dropped }).addDroppedDocument(TASK, DROPPED)
    expect(result.ok).toBe(false)
    expect(calls).toHaveLength(0)
  })
})

describe('M10 S1 exact document disclosure', () => {
  it('opens the card with the provider main chose, not one the renderer named', async () => {
    const { runtime, calls } = requester(() => ok(taskBody('awaiting_approval')))
    await controller(runtime).createDocumentDisclosure(TASK, [DOC_A, DOC_B], 'How well do I match?')
    expect(calls[0].body).toEqual({ document_ids: [DOC_A, DOC_B], recipient: 'gemini', model: 'gemini-2.5-flash', purpose: 'How well do I match?' })
  })

  it('claims, then makes exactly one provider call, then records it', async () => {
    const comparer = new FakeComparer()
    const { runtime, calls } = requester((call) => {
      if (call.path.endsWith('/disclosure/claim')) return ok(claimBody())
      if (call.path.endsWith('/disclosure/result')) return ok(taskBody('compared'))
      return ok(approvedTask())
    })
    const result = await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(result.ok).toBe(true)
    expect(comparer.calls).toBe(1)
    expect(calls.map((call) => call.path)).toEqual([
      `/document-tasks/${TASK}`, `/document-tasks/${TASK}/disclosure/claim`, `/document-tasks/${TASK}/disclosure/result`
    ])
  })

  it('checks the approved provider BEFORE spending the approval', async () => {
    const comparer = new FakeComparer()
    comparer.serve = false
    const { runtime, calls } = requester(() => ok(approvedTask()))
    const result = await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(result.ok).toBe(false)
    expect(calls.map((call) => call.path)).toEqual([`/document-tasks/${TASK}`])
    expect(comparer.calls).toBe(0)
  })

  it('refuses a claim bound to another recipient and never calls a provider', async () => {
    const comparer = new FakeComparer()
    const { runtime } = requester((call) => call.path.endsWith('/claim') ? ok(claimBody({ recipient: 'openai' })) : ok(approvedTask()))
    const result = await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(result.ok).toBe(false)
    expect(comparer.calls).toBe(0)
  })

  it('refuses a claimed projection that carries anything beyond the closed excerpt shape', async () => {
    const comparer = new FakeComparer()
    const leaky = claimBody()
    ;(leaky.projection as Record<string, unknown>).documents = [{ doc_ref: 'd1', excerpt: 'x', truncated: false, path: 'C:\\resume.pdf' }]
    const { runtime } = requester((call) => call.path.endsWith('/claim') ? ok(leaky) : ok(approvedTask()))
    const result = await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(result.ok).toBe(false)
    expect(comparer.calls).toBe(0)
  })

  it('does not claim before the person allowed the card', async () => {
    const comparer = new FakeComparer()
    const { runtime, calls } = requester(() => ok(taskBody('awaiting_approval', { card: (approvedTask().card as object) })))
    const result = await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(result.ok).toBe(false)
    expect(calls.every((call) => !call.path.endsWith('/claim'))).toBe(true)
  })

  it('records a provider failure as a failure, never retrying another provider', async () => {
    const comparer = new FakeComparer()
    comparer.outcome = { kind: 'failed', code: 'model_unavailable' }
    const { runtime, calls } = requester((call) => {
      if (call.path.endsWith('/claim')) return ok(claimBody())
      if (call.path.endsWith('/result')) return ok(taskBody('failed'))
      return ok(approvedTask())
    })
    await controller(runtime, { comparer }).runDocumentDisclosure(TASK)
    expect(comparer.calls).toBe(1)
    expect(calls.at(-1)?.body).toEqual({ disclosure_id: DISCLOSURE, failure: 'model_unavailable' })
  })

  it('maps a runtime refusal to a fixed message, never the runtime text or a path', async () => {
    const { runtime } = requester(() => ({ status: 409, body: { error: { code: 'document_state_changed', message: 'C:\\secret', reason: 'file_changed' } } }) as RuntimeReply)
    const result = await controller(runtime).extractDocument(TASK, DOC_A)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.code).toBe('document_state_changed')
      expect(result.error.message).not.toContain('secret')
      expect(result.error.message).toMatch(/changed/)
    }
  })
})
