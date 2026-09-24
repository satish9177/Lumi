import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AGENT_IPC_CHANNELS } from '../../shared/agent-contracts'
import { isAllowedRuntimeRoute } from '../services/agent-runtime-supervisor'

/**
 * Milestone 10 S1: document text reaches exactly one provider path, and nothing else in main or the
 * renderer can read it, remember it, speak it, plan with it or send a path for it.
 */

const SRC = join(__dirname, '..', '..')

function files(folder: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(folder)) {
    const path = join(folder, name)
    if (statSync(path).isDirectory()) out.push(...files(path))
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(path)
  }
  return out
}

const source = (path: string): string => readFileSync(path, 'utf8')
const UUID = '11111111-2222-4333-8444-555555555555'

describe('M10 S1 document firewall', () => {
  it('only the document controller calls the comparer, and only once', () => {
    const callers = files(SRC).filter((path) => /\.compare\(\{/.test(source(path)))
    expect(callers.map((path) => relative(SRC, path).replace(/\\/g, '/'))).toEqual(['main/services/document-controller.ts'])
    expect(source(join(SRC, 'main/services/document-controller.ts')).match(/\.compare\(\{/g)).toHaveLength(1)
  })

  it('only the comparer uses the document_compare task class', () => {
    const users = files(SRC).filter((path) => /taskClass: 'document_compare'/.test(source(path)))
    expect(users.map((path) => relative(SRC, path).replace(/\\/g, '/'))).toEqual(['main/agent/document-comparer.ts'])
  })

  it('memory, voice, planners and the conversation never import the document modules', () => {
    const forbidden = /document-(wire|controller|comparer|contracts)/
    const guarded = [
      'main/agent/agent-memory.ts', 'main/services/voice-task-controller.ts', 'main/agent/research-planner.ts',
      'main/agent/authenticated-planner.ts', 'main/agent/form-planner.ts', 'main/agent/desktop-planner.ts',
      'main/agent/desktop-reader.ts', 'main/agent/task-request-interpreter.ts', 'main/models/context-builder.ts',
      'renderer/src/realtime.ts', 'renderer/src/voice-task-tools.ts'
    ]
    for (const path of guarded) expect(source(join(SRC, path))).not.toMatch(forbidden)
  })

  it('the renderer panel has no path, shell or IPC capability of its own', () => {
    const panel = source(join(SRC, 'renderer/src/components/DocumentPanel.tsx'))
    expect(panel).not.toMatch(/ipcRenderer|shell\.|require\(|node:fs|webUtils|getPathForFile|dangerouslySetInnerHTML/)
  })

  it('no document channel takes a path: the preload passes ids, revisions, labels, booleans and a purpose', () => {
    const preload = source(join(SRC, 'preload/index.ts'))
    const start = preload.indexOf('listFileRoots:')
    const end = preload.indexOf('runDocumentDisclosure:')
    const bridge = preload.slice(start, preload.indexOf('\n', end))
    expect(bridge).not.toMatch(/\bpath\s*:|absolutePath|filePath|getPathForFile|folderPath/)
    // M10 S4's `startWorkflowDocuments` takes only a workflow id; it is pinned in workflow-controller.test.ts.
    // M12 S2's `attachApprovedDocument` is the orchestration system's own channel, not a new document
    // capability: it takes exactly `addDocumentFromRoot`'s own (orchestrationId, rootId, relativePath) shape
    // and forwards to that same existing, already-reviewed entry point -- pinned in
    // orchestration-coordinator.test.ts.
    expect(Object.keys(AGENT_IPC_CHANNELS).filter((key) => /Document|FileRoot/.test(key) && !/Workflow/.test(key)).sort()).toEqual([
      'addDocumentFromRoot', 'addDroppedDocument', 'addFileRoot', 'attachApprovedDocument', 'compareDocumentsLocally',
      'createDocumentDisclosure', 'createDocumentTask', 'declineDocumentDisclosure', 'extractDocument', 'getDocumentTask',
      'grantDocumentDisclosure', 'listFileRootFiles', 'listFileRoots', 'revokeFileRoot', 'runDocumentDisclosure'
    ])
  })

  it('the runtime allowlist admits exactly the S1 document routes', () => {
    const allowed: Array<['GET' | 'POST', string]> = [
      ['GET', '/file-roots'], ['POST', '/file-roots'], ['POST', `/file-roots/${UUID}/revoke`], ['GET', `/file-roots/${UUID}/files`],
      ['POST', '/document-tasks'], ['GET', '/document-tasks/latest'], ['GET', `/document-tasks/${UUID}`],
      ['POST', `/document-tasks/${UUID}/files`], ['POST', `/document-tasks/${UUID}/dropped-files`], ['POST', `/document-tasks/${UUID}/extract`],
      ['POST', `/document-tasks/${UUID}/compare`], ['POST', `/document-tasks/${UUID}/disclosure`],
      ['POST', `/document-tasks/${UUID}/disclosure/grant`], ['POST', `/document-tasks/${UUID}/disclosure/claim`]
    ]
    for (const [method, path] of allowed) expect(isAllowedRuntimeRoute(method, path)).toBe(true)
    const refused: Array<['GET' | 'POST', string]> = [
      ['POST', `/file-roots/${UUID}/delete`], ['POST', `/file-roots/${UUID}/files`], ['GET', '/files/read?path=C:\\x'],
      ['POST', `/document-tasks/${UUID}/files/write`], ['POST', `/document-tasks/${UUID}/copy`], ['POST', `/document-tasks/${UUID}/open`],
      ['DELETE' as 'GET', `/document-tasks/${UUID}`], ['POST', '/document-tasks/../file-roots'], ['GET', `/document-tasks/${UUID}/text`]
    ]
    for (const [method, path] of refused) expect(isAllowedRuntimeRoute(method, path)).toBe(false)
  })
})
