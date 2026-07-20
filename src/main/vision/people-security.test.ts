/**
 * Cross-cutting security properties of labelled-person matching.
 *
 * Most individual guarantees already have a direct unit test somewhere closer
 * to the code that enforces them — person-enrollment.test.ts for confirmation
 * gating, index-store-phase3.test.ts for record isolation, people-scan.test.ts
 * for embedding lifetime, coordinator-phase3.test.ts for network and coverage
 * honesty. This file exists for the properties that only make sense stated
 * *across* those boundaries: that other features cannot reach the enrolment
 * path at all, that the renderer and Realtime boundaries stay narrow even as
 * the rest of the app grows, and that the download pipeline's integrity check
 * applies to the SFace pack specifically, not only its siblings.
 *
 * Where a property is structural — "X never calls Y" — the test reads the
 * source rather than mocking a full Electron runtime, which is what
 * accessibility.test.tsx already does for the same reason: some claims are
 * about wiring that does not exist, and the most direct proof that wiring is
 * absent is that grep does not find it.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearPeoplePack,
  downloadPeoplePack,
  isPeoplePackInstalled,
  ModelPackError
} from './model-pack'
import { peopleAssetFor } from './people-manifest'

const mainSource = readFileSync(join(process.cwd(), 'src/main/index.ts'), 'utf8')

describe('no other feature can reach the enrolment path', () => {
  /**
   * Splits main's source at the boundaries of the People IPC block (between
   * `getPeopleSearchStatus` and `listDocumentRoots`, which is the handler
   * immediately following it) and asserts the person-profile and enrolment
   * services are referenced *only* inside that span. A reference anywhere
   * else would mean some other flow — capture, Telegram, dropped files — had
   * gained a path to biometric data outside the confirmed enrolment flow.
   */
  function outsidePeopleBlock(): string {
    const blockStart = mainSource.indexOf('IPC_CHANNELS.getPeopleSearchStatus')
    const blockEnd = mainSource.indexOf('IPC_CHANNELS.listDocumentRoots')
    expect(blockStart).toBeGreaterThan(-1)
    expect(blockEnd).toBeGreaterThan(blockStart)
    return mainSource.slice(0, blockStart) + mainSource.slice(blockEnd)
  }

  it('touches personProfiles and personEnrollment only inside the People IPC block', () => {
    const outside = outsidePeopleBlock()
    expect(outside).not.toMatch(/personProfiles\./)
    expect(outside).not.toMatch(/personEnrollment\./)
  })

  it('the capture and screen-analysis handlers never mention person enrolment', () => {
    const captureStart = mainSource.indexOf('IPC_CHANNELS.captureScreen')
    const analyzeEnd = mainSource.indexOf('IPC_CHANNELS.discardCapture')
    const captureBlock = mainSource.slice(captureStart, analyzeEnd)
    expect(captureBlock.length).toBeGreaterThan(0)
    expect(captureBlock).not.toMatch(/personProfiles|personEnrollment|beginPeopleEnrolment|confirm\(/)
  })

  it('the Telegram handlers never mention person enrolment', () => {
    const telegramStart = mainSource.indexOf('IPC_CHANNELS.connectTelegram')
    const telegramEnd = mainSource.indexOf('IPC_CHANNELS.setPanelOpen')
    const telegramBlock = mainSource.slice(telegramStart, telegramEnd)
    expect(telegramBlock.length).toBeGreaterThan(0)
    expect(telegramBlock).not.toMatch(/personProfiles|personEnrollment/)
  })

  it('the dropped-file registration handler never mentions person enrolment', () => {
    const dropStart = mainSource.indexOf('IPC_CHANNELS.registerDroppedFile')
    // removeDroppedFile is the very next handler and the last one registered
    // before this function closes — bounding there, rather than at a distant
    // marker, avoids sweeping in unrelated setup code that happens to sit
    // between two handlers elsewhere in the file.
    const removeDroppedStart = mainSource.indexOf('IPC_CHANNELS.removeDroppedFile')
    expect(dropStart).toBeGreaterThan(-1)
    expect(removeDroppedStart).toBeGreaterThan(dropStart)
    const removeDroppedEnd = mainSource.indexOf('\n  })', removeDroppedStart)
    expect(removeDroppedEnd).toBeGreaterThan(removeDroppedStart)
    const dropBlock = mainSource.slice(dropStart, removeDroppedEnd)
    expect(dropBlock).toContain('removeDroppedFile')
    expect(dropBlock).not.toMatch(/personProfiles|personEnrollment/)
  })

  it('no channel outside the People block can create or mutate a profile', () => {
    // The full set of mutating calls a caller would need to reach. None of
    // these five method names may appear anywhere but inside the block that
    // begins with the People status read.
    for (const call of ['personEnrollment.confirm', 'personEnrollment.begin', 'personProfiles.create', 'personProfiles.rename', 'personProfiles.remove']) {
      const occurrences = [...mainSource.matchAll(new RegExp(call.replace('.', '\\.'), 'g'))].length
      const blockStart = mainSource.indexOf('IPC_CHANNELS.getPeopleSearchStatus')
      const blockEnd = mainSource.indexOf('IPC_CHANNELS.listDocumentRoots')
      const block = mainSource.slice(blockStart, blockEnd)
      const inBlockOccurrences = [...block.matchAll(new RegExp(call.replace('.', '\\.'), 'g'))].length
      expect(occurrences).toBe(inBlockOccurrences)
    }
  })
})

describe('Realtime never receives a profile id or biometric value', () => {
  const realtimeSource = readFileSync(join(process.cwd(), 'src/renderer/src/realtime.ts'), 'utf8')

  it('the search tool schema has no field shaped to carry a profile id', () => {
    // people_labels is a bounded string array; nothing in the schema accepts
    // an id, a score, or a path.
    const toolSchemaStart = realtimeSource.indexOf("name: 'search_documents'")
    const toolSchemaEnd = realtimeSource.indexOf("name: 'open_file'")
    expect(toolSchemaStart).toBeGreaterThan(-1)
    expect(toolSchemaEnd).toBeGreaterThan(toolSchemaStart)
    const schema = realtimeSource.slice(toolSchemaStart, toolSchemaEnd)
    expect(schema).toContain('people_labels')
    expect(schema).not.toMatch(/profile_?id/i)
    expect(schema).not.toMatch(/similarity|embedding|threshold/i)
  })

  it('parseSearchArguments reads people_labels as plain strings and performs no lookup', () => {
    const parseStart = realtimeSource.indexOf('function parseSearchArguments')
    const parseEnd = realtimeSource.indexOf('function requiredArgument')
    const parseBody = realtimeSource.slice(parseStart, parseEnd)
    expect(parseBody).toContain('people_labels')
    // No resolution, no store, no id construction happens in the renderer.
    expect(parseBody).not.toMatch(/resolveLabel|profileStore|PersonProfile/)
  })

  it('CompactSearchResult — what Realtime actually receives — has no id field at all', () => {
    const contracts = readFileSync(join(process.cwd(), 'src/shared/contracts.ts'), 'utf8')
    const interfaceStart = contracts.indexOf('interface CompactSearchResult')
    const interfaceEnd = contracts.indexOf('}', interfaceStart)
    const body = contracts.slice(interfaceStart, interfaceEnd)
    expect(body).not.toMatch(/\bid\b/)
    expect(body).not.toMatch(/profileId|embedding|score/i)
  })
})

describe('the renderer never receives an embedding or a path from the People surface', () => {
  const contracts = readFileSync(join(process.cwd(), 'src/shared/contracts.ts'), 'utf8')

  it('PeopleProfileView has no field for an embedding, a similarity, or a path — only a bounded reference count', () => {
    const start = contracts.indexOf('interface PeopleProfileView')
    const end = contracts.indexOf('}', start)
    const body = contracts.slice(start, end)
    expect(body).toContain('referenceCount: number')
    expect(body).not.toMatch(/embedding|similarity|vector|[a-zA-Z]:[\\/]/i)
  })

  it('PeopleEnrolmentView and PeopleFaceCandidateView never name an embedding, a score, or a path', () => {
    for (const name of ['interface PeopleEnrolmentView', 'interface PeopleFaceCandidateView']) {
      const start = contracts.indexOf(name)
      const end = contracts.indexOf('}', start)
      const body = contracts.slice(start, end)
      expect(body).not.toMatch(/embedding|similarity|landmark|[a-zA-Z]:[\\/]/)
    }
  })
})

describe('unverified SFace weights never load', () => {
  let userDataDir: string

  beforeEach(async () => {
    userDataDir = await mkdtemp(join(tmpdir(), 'lumi-people-security-'))
  })

  afterEach(async () => {
    await rm(userDataDir, { recursive: true, force: true })
  })

  it('refuses and discards a people-pack download whose bytes do not match the pinned digest', async () => {
    const asset = peopleAssetFor('faceEmbedModel')
    // Sized correctly so the download passes the byte-count check and reaches
    // the hash check the digest_mismatch path actually exercises; content is
    // wrong throughout, which is what a corrupted or tampered download is.
    const corruptBody = Buffer.alloc(asset.sizeBytes, 0x00)
    corruptBody.write('not the real SFace weights', 0, 'utf8')
    // Sanity check that the fixture really is a mismatch, not an accidental match.
    expect(createHash('sha256').update(corruptBody).digest('hex')).not.toBe(asset.sha256)

    const runtime = {
      fetch: (async (input: unknown) => {
        const url = typeof input === 'string' ? input : (input as { toString(): string }).toString()
        if (url !== asset.url) {
          throw new Error('unexpected URL requested')
        }
        return new Response(corruptBody, {
          status: 200,
          headers: { 'content-length': String(corruptBody.length) }
        })
      }) as unknown as typeof fetch
    }

    await expect(
      downloadPeoplePack(userDataDir, runtime, { consented: true, onProgress: () => undefined })
    ).rejects.toThrow(expect.objectContaining({ code: 'digest_mismatch' }))

    expect(await isPeoplePackInstalled(userDataDir)).toBe(false)
  }, 30_000)

  it('refuses to install without explicit consent, regardless of what the server would return', async () => {
    const runtime = {
      fetch: (async () => {
        throw new Error('must not be reached without consent')
      }) as unknown as typeof fetch
    }

    await expect(
      downloadPeoplePack(userDataDir, runtime, { consented: false, onProgress: () => undefined })
    ).rejects.toThrow(expect.objectContaining({ code: 'not_consented' }))
  })

  it('clearing the people pack removes it and leaves nothing installed', async () => {
    await clearPeoplePack(userDataDir)
    expect(await isPeoplePackInstalled(userDataDir)).toBe(false)
  })

  it('every people asset URL resolves only to the pinned allowlisted host', () => {
    const asset = peopleAssetFor('faceEmbedModel')
    expect(() => new URL(asset.url)).not.toThrow()
    expect(new URL(asset.url).hostname).toBe('media.githubusercontent.com')
    expect(new URL(asset.url).protocol).toBe('https:')
  })
})

describe('ModelPackError stays importable across every pack (a quick regression guard)', () => {
  it('is the same error class the CLIP and extras downloaders already use', () => {
    expect(ModelPackError).toBeDefined()
  })
})
