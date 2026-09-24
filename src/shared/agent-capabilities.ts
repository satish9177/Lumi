/**
 * Milestone 11 S1: the closed, controller-authored general-capability catalog.
 *
 * This is vocabulary, not authority. Every entry names a capability M1-M10
 * already built, reviewed and gated behind its own approval, grant or effect
 * lock; nothing here grants anything by itself, and nothing here is new
 * authority. A later orchestration planner (Milestone 11 S2+) may choose an
 * `id` from this list; it can never invent one, and choosing an id is a
 * request to use that capability's own existing boundary, never a bypass of
 * it. The model is shown ids and descriptions; it never sees or edits this
 * table's other fields, which exist for Lumi's own controller and cockpit UI.
 *
 * Deliberately absent, matching Milestone 10's hard no-go list and Milestone
 * 9 S4's bounded-mutation boundary: shell/terminal/command execution,
 * arbitrary filesystem writes, generic upload/submit/send/purchase, Git
 * mutation, dependency installation, raw mouse/keyboard/coordinate control,
 * and the semantic desktop mutations themselves (`DESKTOP_SET_VALUE` /
 * `DESKTOP_SELECT` / `DESKTOP_INVOKE`) -- composing those through a second,
 * more general planner is a materially different risk than the single
 * reviewed plan M9 S4 already gates them behind, and Milestone 11 does not
 * reopen that boundary.
 */

export const AGENT_CAPABILITY_IDS = [
  'public_research',
  'inspect_public_page',
  'account_read',
  'document_read',
  'document_compare',
  'download_document',
  'place_downloaded_file',
  'desktop_observe',
  'desktop_reason',
  'desktop_safe_action',
  'launch_registered_app',
  'project_status',
  'project_start',
  'project_stop',
  'form_prepare',
  'workflow_prepare'
] as const

export type AgentCapabilityId = typeof AGENT_CAPABILITY_IDS[number]

export interface AgentCapabilityDescriptor {
  readonly id: AgentCapabilityId
  /** Shown to the model and, unabridged, to the user's cockpit view. */
  readonly description: string
  /** Coarse, descriptive only: what kind of reference this capability's step consumes. */
  readonly inputClasses: readonly string[]
  /** Coarse, descriptive only: what kind of reference this capability's step produces. */
  readonly outputClasses: readonly string[]
  /** Whether using this capability shows the user a new trusted approval/grant card. */
  readonly requiresApproval: boolean
  /** Whether this capability may, in its existing design, send content to a model provider. */
  readonly mayDiscloseToProvider: boolean
  /** Whether this capability changes anything outside Lumi's own read-only records. */
  readonly hasSideEffects: boolean
  /** Whether an unresolved global-tier effect (`app/domain/effects.py`) blocks this capability. */
  readonly blockedByEffectLock: boolean
  /**
   * Milestone 12 S1: whether this capability's own result could carry content from a source the general
   * public does not already have -- an account, a local document, a desktop window, a saved form detail --
   * as opposed to `'public'` (public web content) or `'none'` (a Lumi-authored status/control fact with no
   * meaningful content either way). Combined with `mayDiscloseToProvider`, this is the structural signal
   * `model-router.ts`'s `orchestration_planning` privacy requirement is derived from, so a future capability
   * addition cannot silently reopen that boundary without also failing a test.
   */
  readonly resultPrivacyClass: 'public' | 'private' | 'none'
}

/**
 * The catalog is a `Record`, not an array, so a lookup by id can never be a
 * linear scan that silently accepts a near-miss spelling: `AGENT_CAPABILITY_CATALOG[id]`
 * is `undefined` for anything not in {@link AGENT_CAPABILITY_IDS}.
 */
export const AGENT_CAPABILITY_CATALOG: Readonly<Record<AgentCapabilityId, AgentCapabilityDescriptor>> = {
  public_research: {
    id: 'public_research',
    description: 'Search and read public web pages to answer a question, within a scope the user approves once.',
    inputClasses: ['objective'],
    outputClasses: ['research_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'public'
  },
  inspect_public_page: {
    id: 'inspect_public_page',
    description: 'Open one user-provided public page and answer a question about it.',
    inputClasses: ['url', 'objective'],
    outputClasses: ['research_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'public'
  },
  account_read: {
    id: 'account_read',
    description: "Read a signed-in account's own pages, under a profile the user already approved, to answer a question.",
    inputClasses: ['objective'],
    outputClasses: ['account_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  document_read: {
    id: 'document_read',
    description: 'Read and locally extract text from a file in an already-approved document root.',
    inputClasses: ['document_ref'],
    outputClasses: ['document_result'],
    requiresApproval: false,
    mayDiscloseToProvider: false,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  document_compare: {
    id: 'document_compare',
    description: 'Locally compare two already-read documents, with an optional single provider disclosure of redacted excerpts.',
    inputClasses: ['document_ref', 'document_ref'],
    outputClasses: ['document_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  download_document: {
    id: 'download_document',
    description: 'Download one approved source URL into a quarantine, as the first step of a controlled transfer.',
    inputClasses: ['url', 'document_ref'],
    outputClasses: ['transfer_result'],
    requiresApproval: true,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: true,
    resultPrivacyClass: 'none'
  },
  place_downloaded_file: {
    id: 'place_downloaded_file',
    description: 'Move one quarantined download into an approved folder, atomically and without overwrite.',
    inputClasses: ['transfer_result'],
    outputClasses: ['document_ref'],
    requiresApproval: true,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: true,
    resultPrivacyClass: 'none'
  },
  desktop_observe: {
    id: 'desktop_observe',
    description: 'Read the current state of an already-approved Windows application window.',
    inputClasses: ['desktop_ref'],
    outputClasses: ['desktop_result'],
    requiresApproval: false,
    mayDiscloseToProvider: false,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  desktop_reason: {
    id: 'desktop_reason',
    description: 'Answer a question about one approved, redacted snapshot of a Windows application window.',
    inputClasses: ['desktop_ref', 'objective'],
    outputClasses: ['desktop_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  desktop_safe_action: {
    id: 'desktop_safe_action',
    description: 'Focus a visible window or scroll a list by a closed step, with an exact per-action approval.',
    inputClasses: ['desktop_ref'],
    outputClasses: ['desktop_result'],
    requiresApproval: true,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: false,
    resultPrivacyClass: 'none'
  },
  launch_registered_app: {
    id: 'launch_registered_app',
    description: 'Launch one application the user has already registered, with an exact per-launch approval.',
    inputClasses: ['app_id'],
    outputClasses: ['desktop_result'],
    requiresApproval: true,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: false,
    resultPrivacyClass: 'none'
  },
  project_status: {
    id: 'project_status',
    description: "Check whether a registered project's supervised run is alive and ready.",
    inputClasses: ['project_id'],
    outputClasses: ['project_status'],
    requiresApproval: false,
    mayDiscloseToProvider: false,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'none'
  },
  project_start: {
    id: 'project_start',
    description: "Start a registered project's pinned recipe, with an exact per-run approval and native warning.",
    inputClasses: ['project_id'],
    outputClasses: ['project_status'],
    requiresApproval: true,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: true,
    resultPrivacyClass: 'none'
  },
  project_stop: {
    id: 'project_stop',
    description: "Stop a run this Lumi installation owns, ending only that run's own supervised process job.",
    inputClasses: ['project_id'],
    outputClasses: ['project_status'],
    requiresApproval: false,
    mayDiscloseToProvider: false,
    hasSideEffects: true,
    blockedByEffectLock: false,
    resultPrivacyClass: 'none'
  },
  form_prepare: {
    id: 'form_prepare',
    description: 'Propose which already-approved detail belongs in which field of one observed form. Never submits.',
    inputClasses: ['objective', 'document_ref'],
    outputClasses: ['form_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: false,
    blockedByEffectLock: false,
    resultPrivacyClass: 'private'
  },
  workflow_prepare: {
    id: 'workflow_prepare',
    description: 'Run the existing download -> extract -> compare -> form-preparation workflow as one composed step, stopping before submit.',
    inputClasses: ['objective'],
    outputClasses: ['workflow_result'],
    requiresApproval: true,
    mayDiscloseToProvider: true,
    hasSideEffects: true,
    blockedByEffectLock: true,
    resultPrivacyClass: 'private'
  }
} as const

const CATALOG_IDS: ReadonlySet<string> = new Set(AGENT_CAPABILITY_IDS)

/** Exact membership only: a near-miss spelling is not a catalog id. */
export function isAgentCapabilityId(value: unknown): value is AgentCapabilityId {
  return typeof value === 'string' && CATALOG_IDS.has(value)
}

/**
 * Looks up a descriptor by id. Re-validates at runtime rather than trusting
 * the `AgentCapabilityId` type: a future caller (Milestone 11 S2+) resolves a
 * planner's own output through here, and that output is untrusted until this
 * function itself has checked it -- never merely cast to the type and handed
 * in. Throws rather than returning `undefined` for an unknown id, so a caller
 * cannot forget to handle "not found" and fall through to executing nothing
 * as if it had found something.
 */
export function agentCapability(id: unknown): AgentCapabilityDescriptor {
  if (!isAgentCapabilityId(id)) throw new Error('Unknown agent capability id.')
  return AGENT_CAPABILITY_CATALOG[id]
}

/** The lines a planner is shown: id and description only, nothing else in the descriptor. */
export function agentCapabilityLines(ids: readonly AgentCapabilityId[] = AGENT_CAPABILITY_IDS): string[] {
  return ids.map((id) => `${id}: ${AGENT_CAPABILITY_CATALOG[id].description}`)
}
