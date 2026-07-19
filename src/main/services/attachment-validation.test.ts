import { mkdtemp, rm, truncate, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { LocalStore } from './store'
import {
  isTelegramSafeDimensions,
  MAX_ATTACHMENT_BYTES,
  MAX_PHOTO_BYTES,
  MAX_TEXT_BYTES,
  sniffAttachmentType,
  validateTrustedAttachment,
  revalidateTrustedAttachment
} from './attachment-validation'

const folders: string[] = []

afterEach(async () => {
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

describe('attachment validation', () => {
  it('accepts every supported extension only with its required signature or content', () => {
    expect(sniffAttachmentType('.jpg', Buffer.from([0xff, 0xd8, 0xff]))).toBe('jpeg')
    expect(sniffAttachmentType('.jpeg', Buffer.from([0xff, 0xd8, 0xff]))).toBe('jpeg')
    expect(sniffAttachmentType('.png', Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))).toBe('png')
    expect(sniffAttachmentType('.webp', Buffer.from('RIFF0000WEBP'))).toBe('webp')
    expect(sniffAttachmentType('.pdf', Buffer.from('%PDF-1.7'))).toBe('pdf')
    expect(sniffAttachmentType('.docx', Buffer.from([0x50, 0x4b, 0x03, 0x04]))).toBe('docx')
    expect(sniffAttachmentType('.doc', Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]))).toBe('doc')
    expect(sniffAttachmentType('.txt', Buffer.from('plain text'))).toBe('txt')
  })

  it('rejects extension/content mismatches, NUL text, and unsupported types', () => {
    expect(() => sniffAttachmentType('.jpg', Buffer.from('%PDF-'))).toThrow(/do not match/i)
    expect(() => sniffAttachmentType('.txt', Buffer.from([65, 0, 66]))).toThrow(/do not match/i)
    expect(() => sniffAttachmentType('.rtf', Buffer.from('{\\rtf1'))).toThrow(/not supported/i)
  })

  it('enforces Telegram-safe photo dimensions', () => {
    expect(isTelegramSafeDimensions(4_000, 3_000)).toBe(true)
    expect(isTelegramSafeDimensions(10_001, 3_000)).toBe(false)
    expect(isTelegramSafeDimensions(2_100, 100)).toBe(false)
    expect(isTelegramSafeDimensions(0, 100)).toBe(false)
  })

  it('accepts exact size boundaries and rejects one byte above each limit', async () => {
    const fixture = await createFixture()
    const text = await fixture.result('large.txt', Buffer.alloc(MAX_TEXT_BYTES, 65))
    await expect(validateTrustedAttachment(fixture.store, text.id)).resolves.toMatchObject({ sizeBytes: MAX_TEXT_BYTES, sniffedType: 'txt' })
    await truncate(text.path, MAX_TEXT_BYTES + 1)
    await expect(validateTrustedAttachment(fixture.store, text.id)).rejects.toThrow(/2 MB/i)

    const photo = await fixture.result('large.jpg', Buffer.from([0xff, 0xd8, 0xff]))
    await truncate(photo.path, MAX_PHOTO_BYTES)
    await expect(validateTrustedAttachment(fixture.store, photo.id, () => ({ width: 100, height: 100 }))).resolves.toMatchObject({ sizeBytes: MAX_PHOTO_BYTES, mediaKind: 'photo' })
    await truncate(photo.path, MAX_PHOTO_BYTES + 1)
    await expect(validateTrustedAttachment(fixture.store, photo.id, () => ({ width: 100, height: 100 }))).rejects.toThrow(/10 MB/i)

    const document = await fixture.result('huge.pdf', Buffer.from('%PDF-'))
    await truncate(document.path, MAX_ATTACHMENT_BYTES)
    await expect(validateTrustedAttachment(fixture.store, document.id)).resolves.toMatchObject({ sizeBytes: MAX_ATTACHMENT_BYTES, sniffedType: 'pdf' })
    await truncate(document.path, MAX_ATTACHMENT_BYTES + 1)
    await expect(validateTrustedAttachment(fixture.store, document.id)).rejects.toThrow(/50 MB/i)
  })

  it('rejects a real approved file after its reviewed bytes or mtime change', async () => {
    const fixture = await createFixture()
    const document = await fixture.result('resume.pdf', Buffer.from('%PDF-reviewed'))
    const reviewed = await validateTrustedAttachment(fixture.store, document.id)

    await writeFile(document.path, Buffer.from('%PDF-changed!'))
    const changedTime = new Date(reviewed.mtimeMs + 2_000)
    await utimes(document.path, changedTime, changedTime)

    await expect(revalidateTrustedAttachment(fixture.store, reviewed)).rejects.toThrow(/changed since you reviewed/i)
  })
})

async function createFixture(): Promise<{
  store: LocalStore
  result: (name: string, bytes: Buffer) => Promise<{ id: string; path: string }>
}> {
  const rootPath = await mkdtemp(join(tmpdir(), 'lifelens-attachment-validation-'))
  const statePath = await mkdtemp(join(tmpdir(), 'lifelens-attachment-state-'))
  folders.push(rootPath, statePath)
  const store = new LocalStore(statePath)
  const root = await store.addDocumentRoot(rootPath, 'Approved')
  return {
    store,
    result: async (name, bytes) => {
      const path = join(rootPath, name)
      await writeFile(path, bytes)
      const [result] = await store.saveSearchResults([{
        rootId: root.id,
        name: basename(path),
        relativePath: name,
        modifiedAt: new Date().toISOString(),
        kind: name.endsWith('.jpg') ? 'photo' : 'document',
        absolutePath: path
      }])
      return { id: result!.id, path }
    }
  }
}
