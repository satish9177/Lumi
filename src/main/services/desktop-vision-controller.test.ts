import { describe, expect, it } from 'vitest'
import { DesktopVisionController } from './desktop-vision-controller'
import type { DesktopVisionReasoner } from '../agent/desktop-vision'
import type { LocalOcrEngine } from '../vision/ocr-engine'
import type { DesktopRuntimeRequester } from './desktop-read-controller'
import { setFormDraftWindowActive, setTakeoverActive } from './capture'
import type { RuntimeMethod, RuntimeReply } from './agent-runtime-supervisor'

const TASK = '11111111-2222-4333-8444-555555555555'
const GRANT = '22222222-2222-4333-8444-555555555555'
const DISCLOSURE_GRANT = '33333333-2222-4333-8444-555555555555'

interface Call { method: RuntimeMethod; path: string; body: unknown }

function requester(reply: (call: Call) => RuntimeReply | Promise<RuntimeReply>): { runtime: DesktopRuntimeRequester; calls: Call[] } {
  const calls: Call[] = []
  return {
    calls,
    runtime: { request: async (method, path, body) => { const call = { method, path, body }; calls.push(call); return reply(call) } }
  }
}

const ok = (body: unknown): RuntimeReply => ({ status: 200, body }) as RuntimeReply

function approvedCaptureView(): unknown {
  return {
    task_id: TASK, task_status: 'EXECUTING', task_revision: 2, objective: 'Find Settings', phase: 'approved',
    capture_card: {
      grant_id: GRANT, grant_revision: 2, grant_status: 'ACTIVE',
      application_label: 'VS Code', window_title: 'index.ts', fallback_reason: 'uia_empty'
    }
  }
}

function disclosureApprovedView(): unknown {
  return {
    task_id: TASK, task_status: 'EXECUTING', task_revision: 4, objective: 'Find Settings', phase: 'disclosure_approved',
    disclosure_card: {
      grant_id: DISCLOSURE_GRANT, grant_revision: 2, grant_status: 'ACTIVE',
      application_label: 'VS Code', window_title: 'index.ts', provider: 'gemini', model: 'gemini-2.5-flash',
      purpose: 'Find the Settings button'
    }
  }
}

function rawCaptureBody(): unknown {
  return { capture_id: '44444444-2222-4333-8444-555555555555', image_base64: 'QQ==', width: 10, height: 10, dpi: 96 }
}

function disclosureClaimBody(): unknown {
  return {
    disclosure_id: '55555555-2222-4333-8444-555555555555', task_id: TASK, purpose: 'Find the Settings button',
    recipient: 'gemini', model: 'gemini-2.5-flash', capture: rawCaptureBody()
  }
}

function noOcr(): LocalOcrEngine | undefined {
  return undefined
}

function fakeReasoner(canServe: boolean): DesktopVisionReasoner {
  return {
    candidateProvider: () => ({ recipient: 'gemini', model: 'gemini-2.5-flash' }),
    canServe: () => canServe,
    reason: async () => ({ kind: 'result', candidates: [], provider: 'gemini', model: 'gemini-2.5-flash' })
  } as unknown as DesktopVisionReasoner
}

// Sol Finding 6: eligibility (the card) can be reviewed while capture is still permitted, but the
// desktop state can become blocked (a sign-in window opens, a form draft appears) in the gap before
// the person actually clicks "Allow once". `isCaptureBlocked()` must be re-checked immediately before
// each claim, at the last responsible moment, not only implied by the card having existed. These
// tests pin that ordering in code: the blocked state is flipped to `true` as a side effect of the
// very GET that reads the card back (i.e. as late as possible before the claim), and the claim must
// still never fire.
describe('the desktop vision controller re-checks isCaptureBlocked() immediately before each claim', () => {
  it('takes zero screenshots when a sign-in takeover opens after the card was reviewed but before the claim', async () => {
    setTakeoverActive(false)
    setFormDraftWindowActive(false)
    const { runtime, calls } = requester((call) => {
      if (call.method === 'GET') {
        // The last thing that happens before the controller's own isCaptureBlocked() check: a
        // takeover opens right now, simulating the worst-case timing for the guard to still work.
        setTakeoverActive(true)
      }
      return ok(approvedCaptureView())
    })
    let ocrCalls = 0
    const engine = { recognize: async () => { ocrCalls += 1; return { text: '', tokens: [] } } } as unknown as LocalOcrEngine
    const controller = new DesktopVisionController(runtime, undefined, () => engine)
    const result = await controller.runDesktopCapture(TASK)
    expect(result).toMatchObject({ ok: false, error: { code: 'desktop_read_stale' } })
    expect(calls.map((c) => c.method + ' ' + c.path)).toEqual([`GET /desktop/captures/${TASK}`])
    expect(ocrCalls).toBe(0)
    setTakeoverActive(false)
  })

  it('takes the screenshot when nothing became blocked', async () => {
    setTakeoverActive(false)
    setFormDraftWindowActive(false)
    const { runtime, calls } = requester((call) => {
      if (call.method === 'POST' && call.path.endsWith('/claim')) return ok(rawCaptureBody())
      return ok(approvedCaptureView())
    })
    let ocrCalls = 0
    const engine = { recognize: async () => { ocrCalls += 1; return { text: '', tokens: [] } } } as unknown as LocalOcrEngine
    const controller = new DesktopVisionController(runtime, undefined, () => engine)
    const result = await controller.runDesktopCapture(TASK)
    expect(result.ok).toBe(true)
    expect(calls.some((c) => c.method === 'POST' && c.path === `/desktop/captures/${TASK}/claim`)).toBe(true)
    expect(ocrCalls).toBe(1)
  })

  it('sends zero images to the provider when a form draft appears after the disclosure card was reviewed but before the claim', async () => {
    setTakeoverActive(false)
    setFormDraftWindowActive(false)
    const { runtime, calls } = requester((call) => {
      if (call.method === 'GET') {
        setFormDraftWindowActive(true)
      }
      return ok(disclosureApprovedView())
    })
    const reasoner = fakeReasoner(true)
    let reasonCalls = 0
    ;(reasoner as unknown as { reason: () => Promise<unknown> }).reason = async () => { reasonCalls += 1; return { kind: 'result', candidates: [] } }
    const controller = new DesktopVisionController(runtime, reasoner, noOcr)
    const result = await controller.runDesktopVisionDisclosure(TASK)
    expect(result).toMatchObject({ ok: false, error: { code: 'desktop_read_stale' } })
    expect(calls.map((c) => c.method + ' ' + c.path)).toEqual([`GET /desktop/captures/${TASK}`])
    expect(reasonCalls).toBe(0)
    setFormDraftWindowActive(false)
  })

  it('sends the image to the provider when nothing became blocked', async () => {
    setTakeoverActive(false)
    setFormDraftWindowActive(false)
    const { runtime, calls } = requester((call) => {
      if (call.method === 'POST' && call.path.endsWith('/claim')) return ok(disclosureClaimBody())
      if (call.method === 'POST' && call.path.endsWith('/result')) return ok(disclosureApprovedView())
      return ok(disclosureApprovedView())
    })
    const controller = new DesktopVisionController(runtime, fakeReasoner(true), noOcr)
    const result = await controller.runDesktopVisionDisclosure(TASK)
    expect(result.ok).toBe(true)
    expect(calls.some((c) => c.method === 'POST' && c.path === `/desktop/captures/${TASK}/disclosure/claim`)).toBe(true)
  })
})
