import { mkdir, mkdtemp, symlink, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import { assertRegularFile, DroppedFileStore, validateDroppedFile, DROPPED_FILE_TTL_MS } from './dropped-files'

let dir: string

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), 'lumi-dropped-'))
})

const SAFE_IMAGE = () => ({ width: 800, height: 600 })

/** Minimal byte prefixes that satisfy the existing magic-byte sniffing. */
const FIXTURES: Record<string, Buffer> = {
  'photo.jpg': Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]),
  'shot.png': Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00]),
  'art.webp': Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBP'), Buffer.alloc(8)]),
  'paper.pdf': Buffer.from('%PDF-1.7\nbody'),
  'legacy.doc': Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1, 0x00]),
  'modern.docx': Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00]),
  'notes.txt': Buffer.from('plain text, no NUL bytes')
}

async function write(name: string, contents: Buffer): Promise<string> {
  const path = join(dir, name)
  await writeFile(path, contents)
  return path
}

describe('accepted types', () => {
  it.each(Object.keys(FIXTURES))('accepts %s', async (name) => {
    const path = await write(name, FIXTURES[name])

    const result = await validateDroppedFile(path, SAFE_IMAGE)

    expect(result.fileName).toBe(name)
    expect(result.canonicalPath).toContain(name)
  })

  it('classifies images as photos and everything else as documents', async () => {
    const photo = await validateDroppedFile(await write('photo.jpg', FIXTURES['photo.jpg']), SAFE_IMAGE)
    const document = await validateDroppedFile(await write('paper.pdf', FIXTURES['paper.pdf']), SAFE_IMAGE)

    expect(photo.mediaKind).toBe('photo')
    expect(document.mediaKind).toBe('document')
  })
})

describe('rejections', () => {
  it('rejects an empty path from a virtual file', async () => {
    await expect(validateDroppedFile('', SAFE_IMAGE)).rejects.toThrow(/isn't saved on your computer/)
  })

  it('rejects a directory without following it', async () => {
    const folder = join(dir, 'a-folder')
    await mkdir(folder)

    await expect(validateDroppedFile(folder, SAFE_IMAGE)).rejects.toThrow(/one file, not a folder/)
  })

  it('rejects a .lnk shortcut by extension before reading it', async () => {
    // Contents deliberately look like a valid PDF; the extension must win.
    const path = await write('trap.lnk', Buffer.from('%PDF-1.7'))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/not shortcuts/)
  })

  it('rejects a .url shortcut', async () => {
    const path = await write('bookmark.url', Buffer.from('[InternetShortcut]'))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/not shortcuts/)
  })

  it('rejects a file whose contents contradict its extension', async () => {
    const path = await write('fake.png', Buffer.from('%PDF-1.7 not really a png'))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/can't take this file type/)
  })

  it('rejects an unsupported extension', async () => {
    const path = await write('script.exe', Buffer.from([0x4d, 0x5a, 0x90]))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/can't take this file type/)
  })

  it('rejects a file over the 50 MB attachment limit', async () => {
    const path = await write('huge.pdf', Buffer.concat([Buffer.from('%PDF-1.7'), Buffer.alloc(51 * 1024 * 1024)]))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/up to 50 MB/)
  })

  it('rejects an image over the 10 MB photo limit', async () => {
    const path = await write('big.jpg', Buffer.concat([FIXTURES['photo.jpg'], Buffer.alloc(11 * 1024 * 1024)]))

    await expect(validateDroppedFile(path, SAFE_IMAGE)).rejects.toThrow(/photos up to 10 MB/)
  })

  it('rejects an image with unsafe dimensions', async () => {
    const path = await write('skinny.jpg', FIXTURES['photo.jpg'])

    await expect(validateDroppedFile(path, () => ({ width: 20_000, height: 5 }))).rejects.toThrow(/safely/)
  })

  it('rejects a missing file', async () => {
    await expect(validateDroppedFile(join(dir, 'nope.pdf'), SAFE_IMAGE)).rejects.toThrow(/can't find that file/)
  })

  it('rejects a real symbolic link without dereferencing it', async () => {
    const target = await write('real.pdf', FIXTURES['paper.pdf'])
    const link = join(dir, 'link.pdf')
    let created = true
    try {
      await symlink(target, link, 'file')
    } catch {
      // Creating a symlink needs elevation on Windows. The rule itself is
      // covered unconditionally by the link-type tests below.
      created = false
    }
    if (!created) {
      return
    }

    await expect(validateDroppedFile(link, SAFE_IMAGE)).rejects.toThrow(/not shortcuts/)
  })
})

/**
 * These run everywhere, including on hosts that cannot create a symbolic link,
 * so the reject-links rule is never silently unverified.
 */
describe('link and directory rejection rule', () => {
  const stats = (kind: 'link' | 'dir' | 'file' | 'other') => ({
    isSymbolicLink: () => kind === 'link',
    isDirectory: () => kind === 'dir',
    isFile: () => kind === 'file'
  })

  it('rejects a symbolic link or Windows junction', () => {
    expect(() => assertRegularFile(stats('link'))).toThrow(/not shortcuts/)
  })

  it('rejects a directory', () => {
    expect(() => assertRegularFile(stats('dir'))).toThrow(/one file, not a folder/)
  })

  it('rejects a device, socket or other non-regular entry', () => {
    expect(() => assertRegularFile(stats('other'))).toThrow(/Drop a single file/)
  })

  it('accepts a regular file', () => {
    expect(() => assertRegularFile(stats('file'))).not.toThrow()
  })

  it('treats a link that also reports as a file as a link', () => {
    // Order matters: the link check must win, or a crafted entry slips through.
    expect(() => assertRegularFile({ isSymbolicLink: () => true, isDirectory: () => false, isFile: () => true }))
      .toThrow(/not shortcuts/)
  })
})

describe('DroppedFileStore lifetime', () => {
  it('exposes no path to the renderer', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)

    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    // An exact allowlist, so a new field cannot quietly become a path leak.
    expect(Object.keys(descriptor).sort()).toEqual([
      'droppedId',
      'expiresAt',
      'fileName',
      'fileTypeLabel',
      'mediaKind',
      'sizeBytes'
    ])
    expect(JSON.stringify(descriptor)).not.toContain(dir)
  })

  it('holds one file: a second drop replaces the first', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)

    const first = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))
    const second = await store.register(await write('notes.txt', FIXTURES['notes.txt']))

    expect(await store.resolve(first.droppedId)).toBeUndefined()
    expect(await store.resolve(second.droppedId)).toBeDefined()
    expect(store.current()?.droppedId).toBe(second.droppedId)
  })

  it('resolves a live entry to its canonical path', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    const path = await write('paper.pdf', FIXTURES['paper.pdf'])

    const descriptor = await store.register(path)

    expect(await store.resolve(descriptor.droppedId)).toContain('paper.pdf')
  })

  it('refuses an unknown identifier', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    expect(await store.resolve('11111111-2222-3333-4444-555555555555')).toBeUndefined()
  })

  it('fails closed and clears the entry after the file changes', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    const path = await write('paper.pdf', FIXTURES['paper.pdf'])
    const descriptor = await store.register(path)

    await writeFile(path, Buffer.from('%PDF-1.7 edited after review'))

    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
    expect(store.current()).toBeUndefined()
  })

  it('fails closed after only the mtime changes', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    const path = await write('paper.pdf', FIXTURES['paper.pdf'])
    const descriptor = await store.register(path)

    const later = new Date(Date.now() + 60_000)
    await utimes(path, later, later)

    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
  })

  it('expires after the idle TTL', async () => {
    let now = 1_000
    const store = new DroppedFileStore(SAFE_IMAGE, () => now)
    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    now += DROPPED_FILE_TTL_MS + 1

    expect(store.current()).toBeUndefined()
    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
  })

  it('keeps a fixed expiry that using the entry does not extend', async () => {
    let now = 1_000
    const store = new DroppedFileStore(SAFE_IMAGE, () => now)
    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    // Using the entry just before expiry must not buy it another window.
    now += DROPPED_FILE_TTL_MS - 1
    expect(await store.resolve(descriptor.droppedId)).toBeDefined()

    now += 2
    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
  })

  it('reports the fixed expiry to the renderer', async () => {
    let now = 1_000
    const store = new DroppedFileStore(SAFE_IMAGE, () => now)

    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    expect(Date.parse(descriptor.expiresAt)).toBe(now + DROPPED_FILE_TTL_MS)
  })

  it('clears on explicit removal', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    expect(store.remove(descriptor.droppedId)).toBe(true)
    expect(store.current()).toBeUndefined()
    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
  })

  it('ignores removal of an identifier it does not hold', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    expect(store.remove('unknown-id')).toBe(false)
    expect(store.current()).toBeDefined()
  })

  it('clears everything on shutdown', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)
    const descriptor = await store.register(await write('paper.pdf', FIXTURES['paper.pdf']))

    store.clear()

    expect(await store.resolve(descriptor.droppedId)).toBeUndefined()
  })

  it('keeps nothing when registration is rejected', async () => {
    const store = new DroppedFileStore(SAFE_IMAGE)

    await expect(store.register(join(dir, 'missing.pdf'))).rejects.toThrow()

    expect(store.current()).toBeUndefined()
  })
})
