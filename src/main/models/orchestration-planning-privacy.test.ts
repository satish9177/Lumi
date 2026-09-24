import { describe, expect, it } from 'vitest'
import { AGENT_CAPABILITY_CATALOG, AGENT_CAPABILITY_IDS } from '../../shared/agent-capabilities'
import { PRIVATE_TASK_CLASSES } from './model-router'

/**
 * Milestone 12 S1: the structural regression test the M11 final review called for.
 *
 * `orchestration_planning`'s context is built from every composed capability's own bounded result summary
 * (`orchestrationResultLines` in `orchestration-planner.ts`). Today that summary is always controller-
 * authored template text (see `services/orchestration.py`'s `_capability_safe_label`), but the *catalog*
 * already contains capabilities -- `account_read`, `desktop_reason`, `document_compare`, `form_prepare`,
 * `workflow_prepare` -- whose own result can carry account-, document-, desktop- or form-private content
 * once a later slice composes one of them. This test derives that fact from the catalog's own
 * `mayDiscloseToProvider` + `resultPrivacyClass` fields, rather than hand-maintaining a second list, and
 * asserts the one precondition the M11 review flagged: `orchestration_planning` must already be private.
 *
 * If a future catalog edit ever makes this set empty again, the second assertion below would pass
 * vacuously -- the `toBeGreaterThan(0)` guard below exists so that can never happen silently.
 */
describe('orchestration_planning privacy precondition', () => {
  const privacySensitiveDisclosingCapabilities = AGENT_CAPABILITY_IDS.filter((id) => {
    const descriptor = AGENT_CAPABILITY_CATALOG[id]
    return descriptor.mayDiscloseToProvider && descriptor.resultPrivacyClass === 'private'
  })

  it('the catalog already contains at least one privacy-sensitive disclosing capability (the test is not vacuous)', () => {
    expect(privacySensitiveDisclosingCapabilities.length).toBeGreaterThan(0)
    expect(privacySensitiveDisclosingCapabilities).toEqual(
      expect.arrayContaining(['account_read', 'desktop_reason', 'document_compare', 'form_prepare', 'workflow_prepare'])
    )
  })

  it('requires orchestration_planning to be a private, single-recipient, zero-failover task class', () => {
    expect(PRIVATE_TASK_CLASSES).toContain('orchestration_planning')
  })
})
