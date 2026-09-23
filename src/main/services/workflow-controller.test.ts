import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'
import { WorkflowController, type AdoptionConfirmation } from './workflow-controller'
import { parseWorkflow } from './workflow-wire'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import { isAllowedRuntimeRoute, type RuntimeMethod, type RuntimeReply } from './agent-runtime-supervisor'
import { WorkflowAdoptionCard, WorkflowCandidates } from '../../renderer/src/components/WorkflowPanel'
import { describeDisclosureCard } from '../../renderer/src/agent-task-view'
import type { AgentResult, AgentTaskSnapshot } from '../../shared/agent-contracts'

const WORKFLOW = '11111111-2222-4333-8444-555555555555'
const ACTION = '22222222-2222-4333-8444-555555555555'
const CANDIDATE = '33333333-2222-4333-8444-555555555555'
const FORM_TASK = '44444444-2222-4333-8444-555555555555'
const DOCS_TASK = '55555555-2222-4333-8444-555555555555'
const PROFILE = '66666666-2222-4333-8444-555555555555'
const T = '2026-09-23T10:00:00Z'

interface Call { method: RuntimeMethod; path: string; body: unknown }

const ok = (body: unknown): RuntimeReply => ({ status: 200, body }) as RuntimeReply

function workflowBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    workflow_id: WORKFLOW, status: 'ACTIVE', live: true, revision: 3, objective: 'Prepare my application',
    expires_at: T, stop_reason: null,
    steps: [{ role: 'documents', task_id: DOCS_TASK, task_status: 'READY' }],
    transfer_status: 'PLACED', placed_name: 'details.pdf', document_count: 1, disclosure_status: null,
    candidates: [{
      candidate_id: CANDIDATE, kind: 'email', provenance: 'document_extracted', value: 'priya@example.test',
      preview: 'p***@e***.test', status: 'PROPOSED', document_label: 'details.pdf', quote: null, doc_ref: null
    }],
    values: [],
    adoptions: [{
      action_id: ACTION, revision: 2, action_status: 'WAITING_APPROVAL', approval_status: 'PENDING', candidate_id: CANDIDATE,
      kind: 'email', provenance: 'provider_derived', preview: 'p***@e***.test', value: 'priya@example.test', document_label: 'details.pdf'
    }],
    ...overrides
  }
}

function controller(
  reply: (call: Call) => RuntimeReply, allow = true,
  activate?: (taskId: string, recipientId: string) => Promise<AgentResult<AgentTaskSnapshot>>
): { workflows: WorkflowController; calls: Call[]; confirmations: AdoptionConfirmation[] } {
  const calls: Call[] = []
  const runtime: DesktopRuntimeRequester = {
    request: async (method, path, body) => { const call = { method, path, body }; calls.push(call); return reply(call) }
  }
  const confirmations: AdoptionConfirmation[] = []
  const workflows = new WorkflowController({
    runtime,
    confirmAdoption: async (card) => { confirmations.push(card); return allow },
    ...(activate ? { activateFormTask: activate } : {})
  })
  return { workflows, calls, confirmations }
}

describe('WorkflowController (M10 S4)', () => {
  it('confirms an adoption natively with the RUNTIME’s kind, value and provenance, then approves by id and revision', async () => {
    const { workflows, calls, confirmations } = controller(() => ok(workflowBody()))
    const result = await workflows.approveWorkflowAdoption(WORKFLOW, ACTION, 2)
    expect(result.ok).toBe(true)
    expect(confirmations).toEqual([{
      kind: 'email', value: 'priya@example.test', provenance: 'provider_derived', documentLabel: 'details.pdf',
      workflowObjective: 'Prepare my application'
    }])
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      `GET /workflows/${WORKFLOW}`, `POST /workflows/adoptions/${ACTION}/approve`, `GET /workflows/${WORKFLOW}`
    ])
    expect(calls[1]!.body).toEqual({ expected_revision: 2 })
  })

  it('adopts nothing when the native dialog is cancelled, or when the reviewed revision moved', async () => {
    const cancelled = controller(() => ok(workflowBody()), false)
    expect(await cancelled.workflows.approveWorkflowAdoption(WORKFLOW, ACTION, 2)).toEqual({ ok: true, value: null })
    expect(cancelled.calls.some((call) => call.path.endsWith('/approve'))).toBe(false)
    const moved = controller(() => ok(workflowBody()))
    const result = await moved.workflows.approveWorkflowAdoption(WORKFLOW, ACTION, 1)
    expect(result.ok).toBe(false)
    expect(moved.confirmations).toHaveLength(0)
  })

  it('refuses malformed ids before any request, and never sends a value, provenance or origin', async () => {
    const { workflows, calls } = controller(() => ok(workflowBody()))
    expect((await workflows.proposeWorkflowAdoption(WORKFLOW, 'not-an-id')).ok).toBe(false)
    expect((await workflows.startWorkflowDownload(WORKFLOW, 'file:///C:/x.pdf', WORKFLOW, 'x.pdf', 'x')).ok).toBe(false)
    expect((await workflows.startWorkflowDownload(WORKFLOW, 'https://example.test/x.pdf', WORKFLOW, '..\\x.pdf', 'x')).ok).toBe(false)
    expect(calls).toHaveLength(0)
    await workflows.proposeWorkflowAdoption(WORKFLOW, CANDIDATE)
    expect(calls).toEqual([{ method: 'POST', path: `/workflows/${WORKFLOW}/candidates/${CANDIDATE}/adopt`, body: {} }])
  })

  it('creates the form step once and activates it through the account-task path', async () => {
    let created = false
    const activations: Array<[string, string]> = []
    const { workflows, calls } = controller((call) => {
      if (call.method === 'POST' && call.path.endsWith('/form')) created = true
      const steps = created ? [{ role: 'form', task_id: FORM_TASK, task_status: 'CREATED' }] : []
      return ok(workflowBody({ steps }))
    }, true, async (taskId, recipientId) => { activations.push([taskId, recipientId]); return { ok: true, value: {} as AgentTaskSnapshot } })
    const result = await workflows.startWorkflowForm(WORKFLOW, PROFILE, 'Fill the application', 'gemini')
    expect(result.ok).toBe(true)
    expect(activations).toEqual([[FORM_TASK, 'gemini']])
    expect(calls.filter((call) => call.path.endsWith('/form'))).toEqual([
      { method: 'POST', path: `/workflows/${WORKFLOW}/form`, body: { profile_id: PROFILE, objective: 'Fill the application' } }
    ])
  })

  it('rejects a workflow reply that carries an adopted raw value or an unknown provenance', () => {
    expect(() => parseWorkflow(workflowBody({ values: [{ kind: 'email', provenance: 'document_extracted', preview: 'p', purged: false, value: 'x@y.z' }] }))).toThrow()
    expect(() => parseWorkflow(workflowBody({ values: [{ kind: 'email', provenance: 'user_typed', preview: 'p', purged: false }] }))).toThrow()
    const view = parseWorkflow(workflowBody())
    expect(view.candidates[0]!.value).toBe('priya@example.test')
  })

  it('renders provenance as inert text on the candidate list and the adoption card', () => {
    const view = parseWorkflow(workflowBody())
    const list = renderToStaticMarkup(createElement(WorkflowCandidates, { candidates: view.candidates, busy: false, onAdopt: () => undefined }))
    expect(list).toContain('Found in your document')
    const card = renderToStaticMarkup(createElement(WorkflowAdoptionCard, { card: view.adoptions[0]!, busy: false, onAdopt: () => undefined, onDecline: () => undefined }))
    expect(card).toContain('Suggested by the AI from the approved excerpt')
    expect(card).toContain('not saved as one of your own details')
  })

  it('labels a workflow value on the form manifest card by its source, never as a saved detail', () => {
    const described = describeDisclosureCard({
      actionId: ACTION, revision: 1, actionStatus: 'WAITING_APPROVAL', site: 'jobs.example.test', revealsCountry: false,
      executable: true, valueSource: 'workflow',
      fields: [{ kind: 'saved_detail', fieldLabel: 'Email', controlType: 'email', dataRef: 'email', preview: 'p***@e***.test', provenance: 'provider_derived' }]
    })
    expect(described.rows[0]!.savedLabel).toContain('AI suggestion from your document')
    expect(described.rows[0]!.savedLabel).not.toContain('Saved')
  })

  it('the runtime allowlist pins exactly the workflow routes main calls, and nothing that submits or names a value', () => {
    for (const [method, path] of [
      ['POST', '/workflows'], ['GET', '/workflows/latest'], ['GET', `/workflows/${WORKFLOW}`],
      ['POST', `/workflows/${WORKFLOW}/download`], ['POST', `/workflows/${WORKFLOW}/documents`], ['POST', `/workflows/${WORKFLOW}/form`],
      ['POST', `/workflows/${WORKFLOW}/stop`], ['POST', `/workflows/${WORKFLOW}/candidates/extract`],
      ['POST', `/workflows/${WORKFLOW}/candidates/derive`], ['POST', `/workflows/${WORKFLOW}/candidates/${CANDIDATE}/adopt`],
      ['POST', `/workflows/adoptions/${ACTION}/approve`], ['POST', `/workflows/adoptions/${ACTION}/reject`]
    ] as const) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    for (const [method, path] of [
      ['POST', `/workflows/${WORKFLOW}/submit`], ['POST', `/workflows/${WORKFLOW}/values`], ['DELETE', `/workflows/${WORKFLOW}`],
      ['POST', `/workflows/${WORKFLOW}/candidates/${CANDIDATE}/value`], ['GET', `/workflows/${WORKFLOW}/values`]
    ]) expect(isAllowedRuntimeRoute(method as RuntimeMethod, path), path).toBe(false)
  })
})
