import { describe, expect, it } from 'vitest'
import {
  AGENT_CAPABILITY_CATALOG,
  AGENT_CAPABILITY_IDS,
  agentCapability,
  agentCapabilityLines,
  isAgentCapabilityId,
  type AgentCapabilityId
} from './agent-capabilities'

/**
 * Milestone 11 S1: the closed capability catalog is app-authored data, not
 * model output. These tests pin its two safety properties: every id is real
 * and unique, and the M10 hard no-go list has no representative in it.
 */

// Terms from Milestone 10's hard no-go list and Milestone 9 S4's bounded
// mutations, in the id-shaped spelling a careless addition might use.
const FORBIDDEN_ID_FRAGMENTS = [
  'shell', 'cmd', 'powershell', 'terminal', 'exec', 'command', 'subprocess',
  'arbitrary_file', 'generic_file', 'file_write', 'recursive_delete', 'delete_file',
  'upload', 'submit', 'send_message', 'purchase', 'payment', 'pay',
  'git', 'install', 'dependency',
  'mouse', 'keyboard', 'coordinate', 'click', 'hotkey', 'drag',
  'set_value', 'select_control', 'invoke_control', 'desktop_set_value', 'desktop_select', 'desktop_invoke'
]

describe('the closed capability catalog', () => {
  it('has one descriptor per id, and no id outside the closed list', () => {
    expect(new Set(AGENT_CAPABILITY_IDS).size).toBe(AGENT_CAPABILITY_IDS.length)
    for (const id of AGENT_CAPABILITY_IDS) {
      expect(AGENT_CAPABILITY_CATALOG[id].id).toBe(id)
    }
    expect(Object.keys(AGENT_CAPABILITY_CATALOG).sort()).toEqual([...AGENT_CAPABILITY_IDS].sort())
  })

  it('gives every descriptor every required field, explicitly', () => {
    for (const id of AGENT_CAPABILITY_IDS) {
      const descriptor = AGENT_CAPABILITY_CATALOG[id]
      expect(descriptor.description.length).toBeGreaterThan(0)
      expect(descriptor.inputClasses.length).toBeGreaterThan(0)
      expect(descriptor.outputClasses.length).toBeGreaterThan(0)
      expect(typeof descriptor.requiresApproval).toBe('boolean')
      expect(typeof descriptor.mayDiscloseToProvider).toBe('boolean')
      expect(typeof descriptor.hasSideEffects).toBe('boolean')
      expect(typeof descriptor.blockedByEffectLock).toBe('boolean')
      expect(['public', 'private', 'none']).toContain(descriptor.resultPrivacyClass)
    }
  })

  it('never contains a no-go capability, by id substring', () => {
    for (const id of AGENT_CAPABILITY_IDS) {
      for (const forbidden of FORBIDDEN_ID_FRAGMENTS) {
        expect(id.toLowerCase()).not.toContain(forbidden)
      }
    }
  })

  it('marks every effect-lock-bearing capability as having a side effect', () => {
    // A capability the cross-executor lock can block always changes something
    // outside Lumi's own records; the two booleans must never disagree.
    for (const id of AGENT_CAPABILITY_IDS) {
      const descriptor = AGENT_CAPABILITY_CATALOG[id]
      if (descriptor.blockedByEffectLock) expect(descriptor.hasSideEffects).toBe(true)
    }
  })

  it('marks the read-only S2 capabilities as having no side effect', () => {
    const readOnly: AgentCapabilityId[] = [
      'public_research', 'inspect_public_page', 'account_read', 'document_read',
      'document_compare', 'desktop_observe', 'desktop_reason', 'project_status'
    ]
    for (const id of readOnly) {
      expect(AGENT_CAPABILITY_CATALOG[id].hasSideEffects).toBe(false)
    }
  })

  it('rejects a near-miss spelling as not a capability id', () => {
    expect(isAgentCapabilityId('public_research')).toBe(true)
    expect(isAgentCapabilityId('Public_Research')).toBe(false)
    expect(isAgentCapabilityId('public_research ')).toBe(false)
    expect(isAgentCapabilityId('desktop_action')).toBe(false)
    expect(isAgentCapabilityId('shell')).toBe(false)
    expect(isAgentCapabilityId(42)).toBe(false)
    expect(isAgentCapabilityId(undefined)).toBe(false)
  })

  it('agentCapability looks up an exact descriptor', () => {
    expect(agentCapability('project_status').description).toContain('registered project')
  })

  it('agentCapability re-validates at runtime rather than trusting its caller', () => {
    expect(() => agentCapability('shell')).toThrow()
    expect(() => agentCapability('toString')).toThrow()
    expect(() => agentCapability('__proto__')).toThrow()
    expect(() => agentCapability(undefined)).toThrow()
    expect(() => agentCapability(42)).toThrow()
  })

  it('agentCapabilityLines shows only id and description, never the other fields', () => {
    const lines = agentCapabilityLines(['document_read', 'project_start'])
    expect(lines).toHaveLength(2)
    expect(lines[0]).toBe(`document_read: ${AGENT_CAPABILITY_CATALOG.document_read.description}`)
    expect(lines[1]).toBe(`project_start: ${AGENT_CAPABILITY_CATALOG.project_start.description}`)
    for (const line of lines) {
      expect(line).not.toMatch(/true|false/)
    }
  })

  it('agentCapabilityLines defaults to the whole catalog, in catalog order', () => {
    expect(agentCapabilityLines()).toHaveLength(AGENT_CAPABILITY_IDS.length)
  })
})
