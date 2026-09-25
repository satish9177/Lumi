import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS } from '../../shared/agent-contracts'
import { isAllowedRuntimeRoute } from '../services/agent-runtime-supervisor'

/**
 * Milestone 9, slice 2: the desktop firewall, rewritten on purpose.
 *
 * S1 proved that NOTHING in Electron could read a desktop observation. S2 deliberately changes that
 * invariant, so this file does not delete the S1 proof; it narrows it to a structural fact that must
 * stay true:
 *
 *   without a confirmed, exact desktop disclosure, zero provider paths can read an observation; with
 *   one, exactly the reviewed S2 path (runtime claim -> `DesktopReader` -> one provider attempt) can.
 *
 * "Reviewed" is a list of files below. A new file that names a desktop route, a desktop record or the
 * reader fails here until somebody reads it and adds it, on purpose.
 *
 * Milestone 9, slice 3 adds exactly three desktop EFFECTS (focus, semantic scroll, registered-app launch),
 * each behind an exact trusted approval. They are listed here by name, on purpose: the renderer bridge may
 * gain those eight methods and no others, and there is still no click, key, mouse, coordinate, value,
 * selection, invoke, shell or path anywhere in Electron.
 *
 * Milestone 9, slice 4 adds exactly three more EFFECTS (set a value, select an option, invoke a control),
 * but never as a direct proposal from the renderer: they reach the ledger only through
 * `proposeDesktopActionFromPlan`, from a plan a model proposed and the runtime independently
 * re-verified. It also adds a SEPARATE reviewed disclosure path (`DesktopPlanner` / `desktop_action_planning`),
 * exactly parallel to S2's, whose only output is a validated proposal -- never execution authority -- and
 * one way out of an unresolved mutation (`reconcileDesktopAction`), which only ever records what the
 * person themselves reports. All of it is listed here by name, on purpose.
 *
 * Milestone 9, slice 5 adds a scoped visual fallback: a capture card (no provider named) and, only
 * after a capture succeeds, a SEPARATE vision-disclosure card (one provider/model/purpose), exactly
 * parallel to S4's plan/execute split. `DesktopVisionReasoner` / `desktop_vision` is a second reviewed
 * path to a provider, alongside S2's reader and S4's planner, and it is the ONLY one of the three
 * allowed to carry an image. Its output is a closed candidate list (`AgentVisionCandidate`) -- evidence
 * only, never an action, a click or a coordinate -- and nothing anywhere in this codebase turns one
 * into either. `runDesktopCapture` runs local OCR as a side effect and returns no pixel to the renderer.
 *
 * Milestone 12, slice 4 composes `desktop_observe`/`desktop_reason`/`desktop_safe_action`/
 * `launch_registered_app` into the general orchestrator, over these SAME reviewed S1-S3 entry points --
 * no new desktop route, verb or wire shape. `OrchestrationCoordinator` (`src/main/services/
 * orchestration-coordinator.ts`) becomes a new, reviewed caller of `proposeDesktopFocus`/
 * `proposeDesktopScroll`/`proposeDesktopLaunch` (S3's own three effect proposals, unchanged) and of a
 * new read-only method, `DesktopReadController.observeDesktopSurface` (S1's own observation call,
 * returning ONLY a bounded node count and truncation flag -- never a node, a role, a name or any
 * scanned text). One new renderer-bridge method, `attachApprovedDesktopTarget`, lets the user pick a
 * window from the SAME `listDesktopSurfaces` listing the S2 disclosure card already offers; it mints an
 * opaque resource ref, never itself an observation, a focus, a scroll or a launch.
 */

const ROOT = join(__dirname, '..', '..', '..')
const SELF = join('src', 'main', 'agent', 'desktop-firewall.test.ts')
const SOURCE_DIRECTORIES = ['src/main', 'src/preload', 'src/renderer', 'src/shared', 'src/features']

function walk(directory: string): string[] {
  let entries: string[]
  try {
    entries = readdirSync(directory)
  } catch {
    return []
  }
  return entries.flatMap((entry) => {
    const path = join(directory, entry)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const rel = (path: string): string => relative(ROOT, path).split(sep).join('/')
const isTest = (path: string): boolean => /\.test\.(ts|tsx)$/.test(path)

const files = SOURCE_DIRECTORIES.flatMap((directory) => walk(join(ROOT, directory)))
  .filter((path) => /\.(ts|tsx|json|mjs|js)$/.test(path))
  .filter((path) => relative(ROOT, path) !== SELF)
/** Production code only: what could ever run. Tests may name anything. */
const production = files.filter((path) => !isTest(path))
const source = (path: string): string => readFileSync(path, 'utf8')
const mentioning = (token: string | RegExp, among = production): string[] =>
  among.filter((path) => typeof token === 'string' ? source(path).includes(token) : token.test(source(path))).map(rel).sort()

describe('the desktop firewall', () => {
  it('scans real source files', () => {
    expect(files.length).toBeGreaterThan(50)
    expect(files.some((path) => path.endsWith(`${sep}agent-wire.ts`))).toBe(true)
  })

  it('never names a raw observation, its table or the S1 planted marker in Electron', () => {
    // Production code only: a test may plant the S2 marker to prove it does not leak (that is its job).
    for (const token of ['DesktopObservation', 'desktop_observations', 'M9_S1_', 'M9_S2_', 'snapshot_digest']) {
      expect(mentioning(token), token).toEqual([])
    }
  })

  it('confines the runtime wire words for a desktop record to the one reviewed parser and client', () => {
    expect(mentioning('surface_epoch')).toEqual([
      'src/main/services/desktop-action-controller.ts',
      'src/main/services/desktop-planning-controller.ts',
      'src/main/services/desktop-read-controller.ts',
      'src/main/services/desktop-read-wire.ts',
      'src/main/services/desktop-vision-controller.ts'
    ])
    expect(mentioning('desktop_private')).toEqual([
      'src/main/services/desktop-planning-wire.ts',
      'src/main/services/desktop-read-wire.ts'
    ])
    expect(mentioning('/desktop/actions')).toEqual([
      'src/main/services/agent-runtime-supervisor.ts',
      'src/main/services/desktop-action-controller.ts'
    ])
    expect(mentioning('/desktop/action-plans')).toEqual([
      'src/main/services/agent-runtime-supervisor.ts',
      'src/main/services/desktop-planning-controller.ts'
    ])
    expect(mentioning('/desktop/read-tasks')).toEqual([
      'src/main/services/agent-runtime-supervisor.ts',
      'src/main/services/desktop-read-controller.ts'
    ])
    // The planning panel reuses the existing `listDesktopSurfaces` bridge method (S2); the planning
    // controller itself never calls `/desktop/surfaces` directly.
    expect(mentioning('/desktop/surfaces')).toEqual(['src/main/services/desktop-read-controller.ts'])
    expect(mentioning('/desktop/captures')).toEqual([
      'src/main/services/agent-runtime-supervisor.ts',
      'src/main/services/desktop-vision-controller.ts'
    ])
  })

  it('has exactly one reviewed path from a claimed disclosure to a provider', () => {
    expect(mentioning('DesktopReader')).toEqual([
      'src/main/agent/desktop-reader.ts',
      'src/main/index.ts',
      'src/main/services/desktop-read-controller.ts'
    ])
    expect(mentioning('desktop_planning')).toEqual([
      'src/main/agent/desktop-reader.ts',
      'src/main/models/model-router.ts',
      'src/main/models/scripted-provider.ts',
      'src/shared/model-contracts.ts'
    ])
    // Only the controller calls `.read(`, and only after the runtime claim (proved in its own test).
    const callers = production.filter((path) => /\breader\.read\(/.test(source(path))).map(rel)
    expect(callers).toEqual(['src/main/services/desktop-read-controller.ts'])
  })

  it('has exactly one reviewed path from a claimed plan to a provider (S4, parallel to S2)', () => {
    expect(mentioning('DesktopPlanner')).toEqual([
      'src/main/agent/desktop-planner.ts',
      'src/main/index.ts',
      'src/main/services/desktop-planning-controller.ts'
    ])
    expect(mentioning('desktop_action_planning')).toEqual([
      'src/main/agent/desktop-planner.ts',
      'src/main/models/model-router.ts',
      'src/main/models/scripted-provider.ts',
      'src/shared/model-contracts.ts'
    ])
    // Only the controller calls `.plan(`, and only after the runtime claim (proved in its own test).
    const callers = production.filter((path) => /\bplanner\.plan\(/.test(source(path))).map(rel)
    expect(callers).toEqual(['src/main/services/desktop-planning-controller.ts'])
    // A plan's output is a validated proposal, never execution authority: the planning controller never
    // calls the execution approval routes itself.
    const planningController = source(join(ROOT, 'src', 'main', 'services', 'desktop-planning-controller.ts'))
    expect(planningController).not.toMatch(/approveDesktopAction|\/desktop\/actions\/(?!from-plan)/)
  })

  it('has exactly one reviewed path from a claimed vision disclosure to a provider (S5, parallel to S2/S4)', () => {
    expect(mentioning('DesktopVisionReasoner')).toEqual([
      'src/main/agent/desktop-vision.ts',
      'src/main/index.ts',
      'src/main/services/desktop-vision-controller.ts'
    ])
    // Unlike `desktop_planning` (S2) and `desktop_action_planning` (S4), `desktop_vision` is also a
    // PREFIX of unrelated identifiers this slice adds on purpose (the two grant kinds
    // `desktop_vision_capture`/`desktop_vision_disclose`, the two error codes
    // `desktop_vision_refused`/`desktop_vision_state_changed`, and the `task.desktop_vision_*` event
    // names), so this list is wider than S2/S4's equivalent check for the same router task class.
    expect(mentioning('desktop_vision')).toEqual([
      'src/main/agent/desktop-vision.ts',
      'src/main/models/model-router.ts',
      'src/main/services/desktop-vision-wire.ts',
      'src/renderer/src/agent-task-view.ts',
      'src/shared/agent-contracts.ts',
      'src/shared/agent-runtime-contract.json',
      'src/shared/model-contracts.ts'
    ])
    // Only the controller calls `.reason(`, and only after the runtime claim (proved in its own test).
    const callers = production.filter((path) => /\breasoner\.reason\(/.test(source(path))).map(rel)
    expect(callers).toEqual(['src/main/services/desktop-vision-controller.ts'])
    // The ONE class in the whole router allowed to carry an image, and no other private class gains it.
    expect(mentioning('PRIVATE_VISION_TASK_CLASSES', files)).toEqual([
      'src/main/agent/authenticated-planner.test.ts',
      'src/main/agent/desktop-vision.ts',
      'src/main/models/model-router.ts'
    ])
  })

  it('gives the model context builder, memory, planners and voice no path to a desktop record', () => {
    const untouched = [
      'src/main/models/context-builder.ts',
      'src/main/agent/agent-memory.ts',
      'src/main/agent/research-planner.ts',
      'src/main/agent/research-answer.ts',
      'src/main/agent/authenticated-planner.ts',
      'src/main/agent/authenticated-answer.ts',
      'src/main/agent/form-planner.ts',
      'src/main/agent/page-answer.ts',
      'src/main/agent/task-request-interpreter.ts',
      'src/main/services/voice-task-controller.ts',
      'src/main/services/agent-tasks.ts',
      'src/shared/voice-task-contracts.ts',
      'src/renderer/src/voice-task-tools.ts'
    ]
    for (const name of untouched) {
      expect(source(join(ROOT, name)), name).not.toMatch(/desktop[_ ]?(observation|surface|uia|read|disclos|snapshot|capture|vision)|DesktopRead|desktop_planning/i)
    }
  })

  it('keeps the desktop controller out of voice and the conversation', () => {
    const controller = source(join(ROOT, 'src', 'main', 'services', 'desktop-read-controller.ts'))
    // It must not import the interpreter, memory, the conversation window or any voice module.
    expect(controller).not.toMatch(/from '.*(task-request-interpreter|agent-memory|voice|context-builder|conversation)/)
    const voice = source(join(ROOT, 'src', 'main', 'services', 'voice-task-controller.ts'))
    expect(voice).not.toMatch(/DesktopReadController|desktop-read/)
  })

  it('keeps the desktop planning controller out of voice and the conversation (S4)', () => {
    const controller = source(join(ROOT, 'src', 'main', 'services', 'desktop-planning-controller.ts'))
    expect(controller).not.toMatch(/from '.*(task-request-interpreter|agent-memory|voice|context-builder|conversation)/)
    const voice = source(join(ROOT, 'src', 'main', 'services', 'voice-task-controller.ts'))
    expect(voice).not.toMatch(/DesktopPlanningController|desktop-planning/)
    expect(mentioning('DesktopPlanningController')).toEqual([
      'src/main/index.ts',
      'src/main/services/agent-ipc.ts',
      'src/main/services/desktop-planning-controller.ts'
    ])
  })

  it('keeps the desktop vision controller out of voice and the conversation (S5)', () => {
    const controller = source(join(ROOT, 'src', 'main', 'services', 'desktop-vision-controller.ts'))
    expect(controller).not.toMatch(/from '.*(task-request-interpreter|agent-memory|voice|context-builder|conversation)/)
    const voice = source(join(ROOT, 'src', 'main', 'services', 'voice-task-controller.ts'))
    expect(voice).not.toMatch(/DesktopVisionController|desktop-vision/)
    expect(mentioning('DesktopVisionController')).toEqual([
      'src/main/index.ts',
      'src/main/services/agent-ipc.ts',
      'src/main/services/desktop-vision-controller.ts'
    ])
  })

  const S2_METHODS = [
    'createDesktopRead', 'declineDesktopDisclosure', 'getDesktopRead',
    'grantDesktopDisclosure', 'listDesktopSurfaces', 'runDesktopRead'
  ]
  const S3_METHODS = [
    'approveDesktopAction', 'declineDesktopAction', 'findDesktopScrollTargets', 'getDesktopAction',
    'listDesktopApps', 'proposeDesktopFocus', 'proposeDesktopLaunch', 'proposeDesktopScroll'
  ]
  // S4 adds one execution method (opens the SECOND card from a validated plan; it never sets an
  // operation, target or value itself), one reconciliation method (records only what the person
  // observed), and the six planning-disclosure methods parallel to S2's.
  const S4_EXECUTION_METHODS = ['proposeDesktopActionFromPlan', 'reconcileDesktopAction']
  const S4_PLANNING_METHODS = [
    'createDesktopPlan', 'declineDesktopPlan', 'getDesktopPlan', 'grantDesktopPlan', 'runDesktopPlan'
  ]
  // S5: a capture card (five methods) and a SEPARATE vision-disclosure card (four methods, no "get" --
  // it is read back through the same task the capture card is on).
  const S5_METHODS = [
    'createDesktopCapture', 'getDesktopCapture', 'grantDesktopCapture', 'declineDesktopCapture', 'runDesktopCapture',
    'createDesktopVisionDisclosure', 'grantDesktopVisionDisclosure', 'declineDesktopVisionDisclosure', 'runDesktopVisionDisclosure'
  ]
  // Milestone 12 S4: ONE new renderer-bridge method -- a trusted attach action that mints an opaque
  // `desktop_target_ref` resource from the SAME S1 surface listing. It performs no desktop verb of its
  // own (no observe, focus, scroll or launch); `attachApprovedApp`/`attachApprovedProject` do not match
  // `/Desktop/` and so are not counted here, but are reviewed alongside it in `docs/reviews/milestone-12-s4.md`.
  const M12_S4_METHODS = ['attachApprovedDesktopTarget']
  const ALL_DESKTOP_METHODS = [
    ...S2_METHODS, ...S3_METHODS, ...S4_EXECUTION_METHODS, ...S4_PLANNING_METHODS, ...S5_METHODS, ...M12_S4_METHODS
  ].sort()

  it('exposes exactly the six S2, eight S3, seven S4, nine S5 and one M12 S4 typed desktop methods on the renderer bridge and no other verb', () => {
    const bridge = source(join(ROOT, 'src', 'preload', 'index.ts'))
    const methods = [...bridge.matchAll(/^\s{2}(\w*Desktop\w*):/gm)].map((match) => match[1]).sort()
    expect(methods).toEqual(ALL_DESKTOP_METHODS)
    // Nothing that clicks, types, invokes, sets a value, selects, drags, or takes a handle/path/coordinate.
    // (`proposeDesktopActionFromPlan` and `reconcileDesktopAction` name no operation of their own: the
    // first takes only a plan id, and the second only a revision and what the person observed.)
    expect(bridge).not.toMatch(/invoke(Desktop|Control)|setDesktopValue|clickDesktop|typeDesktop|selectDesktop|dragDesktop|keyDesktop|mouseDesktop|pressDesktop|hotkey/i)
    // Focus, scroll and launch exist ONLY as the three reviewed proposals (a card, never an effect by itself).
    const effectNames = methods.filter((name) => /focus|scroll|launch/i.test(name)).sort()
    expect(effectNames).toEqual(['findDesktopScrollTargets', 'proposeDesktopFocus', 'proposeDesktopLaunch', 'proposeDesktopScroll'])
    // No generic pass-through: nothing takes a method name, a route, a channel or a command object.
    expect(bridge).not.toMatch(/desktop\s*\(\s*(command|method|tool|route|channel)/i)
    expect(bridge).not.toMatch(/(executeDesktop|computer)\s*[:(]/)
  })

  it('registers exactly the six S2, eight S3, seven S4, nine S5 and one M12 S4 desktop channels, none of which can click, type or run anything', () => {
    const channels = Object.keys(AGENT_IPC_CHANNELS).filter((name) => /desktop/i.test(name)).sort()
    expect(channels).toEqual(ALL_DESKTOP_METHODS)
    for (const name of channels) {
      // `reconcileDesktopAction` legitimately contains none of these; it is listed to make the
      // allowance explicit rather than accidental.
      expect(name).not.toMatch(/invoke|type(?!s)|select|click|execute|key|mouse|drag|value/i)
    }
  })

  it('keeps the desktop action controller out of voice, the conversation and every provider', () => {
    const controller = source(join(ROOT, 'src', 'main', 'services', 'desktop-action-controller.ts'))
    expect(controller).not.toMatch(/from '.*(task-request-interpreter|agent-memory|voice|context-builder|conversation|model-router|desktop-reader|models\/)/)
    expect(controller).not.toMatch(/router|provider|prompt/i)
    expect(source(join(ROOT, 'src', 'main', 'services', 'voice-task-controller.ts'))).not.toMatch(/DesktopActionController|desktop-action/)
    expect(mentioning('DesktopActionController')).toEqual([
      'src/main/index.ts',
      'src/main/services/agent-ipc.ts',
      'src/main/services/desktop-action-controller.ts'
    ])
    // Focus, scroll and launch are named nowhere in Electron main except the controller, wire, IPC and
    // contracts, plus (Milestone 12 S4) the two files that compose them into the general orchestrator:
    // `index.ts` wires the dependency, and `orchestration-coordinator.ts` is the one new, reviewed
    // caller -- it still only ever proposes the SAME three S3 cards, never a fourth verb.
    expect(mentioning(/proposeDesktop(Focus|Scroll|Launch)/)).toEqual([
      'src/main/index.ts',
      'src/main/services/agent-ipc.ts',
      'src/main/services/desktop-action-controller.ts',
      'src/main/services/orchestration-coordinator.ts',
      'src/preload/index.ts',
      'src/renderer/src/components/DesktopActionPanel.tsx',
      'src/shared/agent-contracts.ts'
    ])
  })

  it('has no path from the desktop action panel or controller to a click, key, coordinate, value, shell or path', () => {
    for (const name of [
      // The planning disclosure side (`desktop-planner.ts`, `desktop-planning-wire.ts`) is deliberately
      // NOT checked here, exactly like S2's `desktop-reader.ts`/`desktop-read-wire.ts` never were: it
      // builds a model prompt and parses a proposal, and legitimately uses ordinary `RegExp.exec` in its
      // scripted stand-in. `DesktopPlanningPanel.tsx` IS checked: it renders the second, execution-side
      // card and calls the same approve/decline/reconcile methods `DesktopActionPanel.tsx` does.
      'src/main/services/desktop-action-controller.ts', 'src/main/services/desktop-action-wire.ts',
      'src/main/services/desktop-planning-controller.ts',
      'src/renderer/src/components/DesktopActionPanel.tsx', 'src/renderer/src/components/DesktopPlanningPanel.tsx'
    ]) {
      const text = source(join(ROOT, name))
      // (User-facing sentences may say "mouse" or "keyboard"; what may never appear is code that could act.)
      expect(text, name).not.toMatch(/sendInput|sendKeys|clientX|clientY|coordinate|hotkey|clipboard|child_process|spawn\(|exec\(|powershell|cmd\.exe|executable|dangerouslySetInnerHTML|innerHTML|ipcRenderer|require\(/i)
      expect(text, name).not.toMatch(/desktopCapturer|screenshot|thumbnail|getUserMedia|\bocr\b/i)
    }
  })

  it('has no path from the desktop vision controller/wire or a candidate to a click, key, coordinate or executable action (S5)', () => {
    for (const name of [
      // The reasoning side (`desktop-vision.ts`) is deliberately NOT checked here, exactly like S2's
      // `desktop-reader.ts` and S4's `desktop-planner.ts` never were: it builds a model prompt and
      // parses a candidate list. `desktop-vision-controller.ts`, `desktop-vision-wire.ts` and the
      // renderer panel ARE checked: they are the trusted domain client, its strict parser, and the
      // only place a candidate's `region` ever reaches a rendered page.
      'src/main/services/desktop-vision-controller.ts', 'src/main/services/desktop-vision-wire.ts',
      'src/renderer/src/components/DesktopVisionPanel.tsx'
    ]) {
      const text = source(join(ROOT, name))
      expect(text, name).not.toMatch(/sendInput|sendKeys|clientX|clientY|hotkey|clipboard|child_process|spawn\(|exec\(|powershell|cmd\.exe|dangerouslySetInnerHTML|innerHTML|ipcRenderer|require\(/i)
      // A vision candidate is evidence only: nothing here may read or forward a coordinate as
      // anything other than the closed, normalised `region` field a candidate carries, and nothing
      // may turn a candidate into a click, a focus target or a dispatch.
      expect(text, name).not.toMatch(/\bclick\b|mouse_event|MOUSEEVENTF|dispatchAction|approveDesktopAction|\/desktop\/actions\//i)
    }
    // Nothing anywhere in production code converts a vision candidate into an action-ledger call.
    expect(mentioning(/candidatesToWire|AgentVisionCandidate/).sort()).toEqual([
      'src/main/agent/desktop-vision.ts',
      'src/main/services/desktop-vision-controller.ts',
      'src/main/services/desktop-vision-wire.ts',
      'src/shared/agent-contracts.ts'
    ])
  })

  it('lets main reach only the reviewed desktop routes, never a raw observation or a verb', () => {
    const id = '11111111-2222-4333-8444-555555555555'
    const allowed: Array<['GET' | 'POST', string]> = [
      ['GET', '/desktop/surfaces'],
      ['POST', '/desktop/read-tasks'],
      ['GET', '/desktop/read-tasks/latest'],
      ['GET', `/desktop/read-tasks/${id}`],
      ['POST', `/desktop/read-tasks/${id}/grant`],
      ['POST', `/desktop/read-tasks/${id}/revoke`],
      ['POST', `/desktop/read-tasks/${id}/disclosure`],
      ['POST', `/desktop/read-tasks/${id}/result`],
      // S3: three proposals that only open a card, the scroll-target read, and the two card decisions.
      ['GET', '/desktop/actions/apps'],
      ['GET', '/desktop/actions/latest'],
      ['POST', '/desktop/actions/focus'],
      ['POST', '/desktop/actions/scroll'],
      ['POST', '/desktop/actions/launch'],
      ['POST', '/desktop/actions/scroll-targets'],
      ['POST', `/desktop/actions/${id}/approve`],
      ['POST', `/desktop/actions/${id}/decline`],
      // S4: the SECOND, separate execution card built only from a validated plan id, and the only way
      // out of an unresolved mutation -- a report of what the person observed, never a retry.
      ['POST', '/desktop/actions/from-plan'],
      ['POST', `/desktop/actions/${id}/reconcile`],
      // S4: exact desktop action-planning disclosure. `claim`/`result` are internal (main calls them
      // after the trusted click and the one provider attempt; the renderer never calls them directly).
      ['POST', '/desktop/action-plans'],
      ['GET', '/desktop/action-plans/latest'],
      ['GET', `/desktop/action-plans/${id}`],
      ['POST', `/desktop/action-plans/${id}/grant`],
      ['POST', `/desktop/action-plans/${id}/revoke`],
      ['POST', `/desktop/action-plans/${id}/claim`],
      ['POST', `/desktop/action-plans/${id}/result`],
      // S5: a capture card, then a SEPARATE vision-disclosure card for the same task. `claim`/`result`
      // are internal, exactly like S2/S4's own claim/result routes.
      ['POST', '/desktop/captures'],
      ['GET', `/desktop/captures/${id}`],
      ['POST', `/desktop/captures/${id}/grant`],
      ['POST', `/desktop/captures/${id}/revoke`],
      ['POST', `/desktop/captures/${id}/claim`],
      ['POST', `/desktop/captures/${id}/disclosure`],
      ['POST', `/desktop/captures/${id}/disclosure/grant`],
      ['POST', `/desktop/captures/${id}/disclosure/revoke`],
      ['POST', `/desktop/captures/${id}/disclosure/claim`],
      ['POST', `/desktop/captures/${id}/disclosure/result`]
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path), path).toBe(true)
    const rejected: Array<['GET' | 'POST', string]> = [
      ['POST', '/desktop/observations'],
      ['GET', '/desktop/observations'],
      ['POST', '/desktop/surfaces'],
      ['GET', `/desktop/read-tasks/${id}/disclosure`],
      ['POST', `/desktop/read-tasks/${id}/focus`],
      ['POST', `/desktop/read-tasks/${id}/invoke`],
      ['POST', `/desktop/read-tasks/${id}/click`],
      ['POST', '/desktop/focus'],
      ['POST', '/desktop/invoke'],
      ['POST', '/desktop/execute'],
      ['POST', '/desktop/read-tasks/latest'],
      // Still no verb: nothing that clicks, types, sets a value, selects, invokes, runs a command or names a path.
      // In particular, S4's three mutations are NEVER directly-proposable routes: only `from-plan` reaches them.
      ['POST', '/desktop/actions/click'],
      ['POST', '/desktop/actions/type'],
      ['POST', '/desktop/actions/invoke'],
      ['POST', '/desktop/actions/set-value'],
      ['POST', '/desktop/actions/select'],
      ['POST', '/desktop/actions/execute'],
      ['POST', '/desktop/actions/launch-path'],
      ['POST', '/desktop/actions/shell'],
      ['POST', `/desktop/actions/${id}/execute`],
      ['POST', `/desktop/actions/${id}/retry`],
      ['GET', `/desktop/actions/${id}/approve`],
      ['GET', `/desktop/actions/${id}/reconcile`],
      ['GET', '/desktop/actions/focus'],
      ['POST', '/desktop/actions/latest'],
      ['POST', '/desktop/actions/apps'],
      ['GET', '/desktop/actions/from-plan'],
      // S4 planning: no direct action route, no raw observation, no verb.
      ['POST', '/desktop/action-plans/latest'],
      ['GET', `/desktop/action-plans/${id}/grant`],
      ['POST', `/desktop/action-plans/${id}/approve`],
      ['POST', `/desktop/action-plans/${id}/set-value`],
      ['POST', `/desktop/action-plans/${id}/invoke`],
      ['POST', '/desktop/action-plans/observations'],
      // S5: no direct action route, no raw pixel route, no verb.
      ['GET', '/desktop/captures'],
      ['POST', `/desktop/captures/${id}`],
      ['POST', '/desktop/captures/latest'],
      ['GET', `/desktop/captures/${id}/claim`],
      ['POST', `/desktop/captures/${id}/click`],
      ['POST', `/desktop/captures/${id}/focus`],
      ['POST', `/desktop/captures/${id}/disclosure/click`],
      ['GET', `/desktop/captures/${id}/disclosure`]
    ]
    for (const [method, path] of rejected) expect(isAllowedRuntimeRoute(method, path), `${method} ${path}`).toBe(false)
  })

  it('names the desktop error codes only in the generated contract and the reviewed wire projection', () => {
    const contract = production.filter((path) => source(path).includes('desktop_refused')).map(rel).sort()
    expect(contract).toEqual([
      'src/main/services/desktop-action-wire.ts',
      'src/main/services/desktop-planning-wire.ts',
      'src/main/services/desktop-read-wire.ts',
      'src/main/services/desktop-vision-wire.ts',
      'src/shared/agent-contracts.ts',
      'src/shared/agent-runtime-contract.json'
    ])
  })

  it('has no main or renderer path that opens a capture, image or vision call for a desktop read', () => {
    for (const name of [
      'src/main/agent/desktop-reader.ts', 'src/main/services/desktop-read-controller.ts',
      'src/main/services/desktop-read-wire.ts', 'src/renderer/src/components/DesktopReadPanel.tsx',
      'src/main/agent/desktop-planner.ts', 'src/main/services/desktop-planning-controller.ts',
      'src/main/services/desktop-planning-wire.ts', 'src/renderer/src/components/DesktopPlanningPanel.tsx'
    ]) {
      const text = source(join(ROOT, name))
      expect(text, name).not.toMatch(/desktopCapturer|capture\.ts|screenReasoning|screen-reasoning|\bimage\s*:|thumbnail|getUserMedia|\bocr\b/i)
    }
  })
})
