import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { executeTelegramAfterConfirmation, TelegramService, mapTelegramError, type SafeStoragePort } from './telegram'

const folders: string[] = []
const credentials = { apiId: 12345, apiHash: 'test-hash' }

afterEach(async () => {
  await Promise.all(folders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createFolder(): Promise<string> {
  const folder = await mkdtemp(join(tmpdir(), 'lifelens-telegram-'))
  folders.push(folder)
  return folder
}

function createSafeStorage(): SafeStoragePort {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`encrypted:${Buffer.from(value).toString('base64')}`),
    decryptString: (value) => {
      const serialized = value.toString()
      if (!serialized.startsWith('encrypted:')) throw new Error('corrupt')
      return Buffer.from(serialized.slice('encrypted:'.length), 'base64').toString()
    }
  }
}

class FakeClient {
  readonly session = { save: () => this.sessionValue }
  connected = false
  authorized = true
  sessionValue = 'telegram-session-secret'
  sent: Array<{ peer: unknown; message: string }> = []
  sentFiles: Array<{ peer: unknown; params: any }> = []
  sendFileHandler: ((peer: unknown, params: any) => Promise<{ id: number }>) | undefined
  dialogs: Array<any> = []
  dialogCalls = 0
  qrParams: any
  constructor(readonly loadedSession = '') {}

  async connect(): Promise<boolean> { this.connected = true; return true }
  async disconnect(): Promise<void> { this.connected = false }
  async checkAuthorization(): Promise<boolean> { return this.authorized }
  async getMe(): Promise<unknown> { return { firstName: 'Lumi', username: 'lifelens_test' } }
  async signInUserWithQrCode(_credentials: unknown, params: any): Promise<unknown> {
    this.qrParams = params
    await params.qrCode?.({ token: Buffer.from('short-lived-token'), expires: 1_800_000_000 })
    return new Promise(() => undefined)
  }
  async getDialogs(): Promise<any[]> { this.dialogCalls += 1; return this.dialogs }
  async sendMessage(peer: unknown, params: { message: string }): Promise<{ id: number }> {
    this.sent.push({ peer, message: params.message })
    return { id: this.sent.length }
  }
  async sendFile(peer: unknown, params: any): Promise<{ id: number }> {
    this.sentFiles.push({ peer, params })
    return this.sendFileHandler ? this.sendFileHandler(peer, params) : { id: this.sentFiles.length }
  }
  async invoke(): Promise<void> {}
}

function createService(folder: string, client: FakeClient, statusUpdates: unknown[] = [], storedCredentials = credentials): TelegramService {
  return new TelegramService(folder, createSafeStorage(), (status) => statusUpdates.push(status), storedCredentials, () => client as any)
}

function dialog(name: string, rank: number, options: { username?: string; kind?: 'user' | 'group' | 'channel'; deleted?: boolean } = {}): any {
  const kind = options.kind ?? 'user'
  return {
    entity: { firstName: name, username: options.username, deleted: options.deleted },
    inputEntity: { trustedPeer: rank },
    isUser: kind === 'user',
    isGroup: kind === 'group',
    isChannel: kind === 'channel'
  }
}

describe('TelegramService', () => {
  it('fails clearly when developer credentials are absent', async () => {
    const service = createService(await createFolder(), new FakeClient(), [], null as unknown as undefined)
    await expect(service.connect()).rejects.toThrow('Telegram developer credentials are not configured.')
  })

  it('persists only encrypted session data and restores an authorised session', async () => {
    const folder = await createFolder()
    const first = new FakeClient()
    const service = createService(folder, first)
    await expect(service.connect()).resolves.toMatchObject({ state: 'connected', account: { displayName: 'Lumi' } })
    const serialized = await readFile(join(folder, 'telegram-session.bin'), 'utf8')
    expect(serialized).toContain('encrypted:')
    expect(serialized).not.toContain('telegram-session-secret')

    const restored = new FakeClient('telegram-session-secret')
    const restarted = createService(folder, restored)
    await expect(restarted.initialize()).resolves.toMatchObject({ state: 'connected' })
    expect(restored.loadedSession).toBe('telegram-session-secret')
  })

  it('recovers safely from a corrupted encrypted session', async () => {
    const folder = await createFolder()
    await writeFile(join(folder, 'telegram-session.bin'), 'not-an-encrypted-session')
    const service = createService(folder, new FakeClient())
    await expect(service.initialize()).resolves.toMatchObject({ state: 'disconnected', message: expect.stringMatching(/could not be read/i) })
  })

  it('publishes a short-lived QR state and clears it when cancelled', async () => {
    const client = new FakeClient()
    client.authorized = false
    const updates: any[] = []
    const service = createService(await createFolder(), client, updates)
    await expect(service.connect()).resolves.toMatchObject({ state: 'connecting', qrUrl: expect.stringMatching(/^tg:\/\/login\?token=/) })
    await expect(service.cancelLogin()).resolves.toEqual({ state: 'disconnected' })
    expect(updates.some((status) => status.qrUrl)).toBe(true)
  })

  it('bounds and deterministically orders local dialog metadata without exposing peers', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi Later', 0), dialog('Ravi', 1, { username: 'ravi' }), ...Array.from({ length: 12 }, (_, index) => dialog(`Ravi ${index}`, index + 2)), dialog('Deleted Ravi', 20, { deleted: true })]
    const service = createService(await createFolder(), client)
    await service.connect()
    const results = await service.searchRecipients('ravi')
    expect(results).toHaveLength(10)
    expect(results[0]).toMatchObject({ displayName: 'Ravi', username: 'ravi', kind: 'user' })
    expect(JSON.stringify(results)).not.toContain('trustedPeer')
    expect(client.dialogCalls).toBe(1)
  })

  it('rejects unknown result IDs, duplicate calls, and messages beyond the plain-text limit', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    const service = createService(await createFolder(), client)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    await expect(service.sendConfirmed('call-1', 'stale', 'hello')).rejects.toThrow(/no longer available/i)
    await expect(service.sendConfirmed('call-1', recipient!.resultId, 'x'.repeat(4_097))).rejects.toThrow(/4096/i)
    await service.sendConfirmed('call-1', recipient!.resultId, 'hello')
    await expect(service.sendConfirmed('call-1', recipient!.resultId, 'hello again')).rejects.toThrow(/already handled/i)
    expect(client.sent).toEqual([{ peer: { trustedPeer: 0 }, message: 'hello' }])
  })

  it('does not send when the explicit Telegram confirmation is rejected', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    const service = createService(await createFolder(), client)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    await expect(executeTelegramAfterConfirmation(service, false, 'call-2', recipient!.resultId, 'do not send')).resolves.toMatchObject({ ok: false, message: expect.stringMatching(/nothing was sent/i) })
    expect(client.sent).toEqual([])
  })

  it('clears encrypted state and recipient maps on logout', async () => {
    const folder = await createFolder()
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    const service = createService(folder, client)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    await service.logout()
    await expect(readFile(join(folder, 'telegram-session.bin'))).rejects.toThrow()
    await expect(service.sendConfirmed('call', recipient!.resultId, 'hello')).rejects.toThrow(/not connected/i)
  })

  it('maps Telegram failures to safe user-visible errors', () => {
    expect(mapTelegramError(new Error('FLOOD_WAIT_30'))).toMatch(/wait/i)
    expect(mapTelegramError(new Error('USER_PRIVACY_RESTRICTED'))).toMatch(/privacy/i)
    expect(mapTelegramError(new Error('socket timeout'))).toMatch(/network/i)
  })

  it('sends trusted photos and documents once with verbatim captions and the correct mode', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    const service = createService(await createFolder(), client)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    const snapshot = service.snapshotRecipient(recipient!.resultId)!

    await service.sendConfirmedAttachment('photo-call', snapshot, {
      canonicalPath: 'C:\\approved\\life.jpg', fileName: 'life.jpg', sizeBytes: 123,
      mediaKind: 'photo', caption: '  best picture  '
    })
    await service.sendConfirmedAttachment('document-call', snapshot, {
      canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456,
      mediaKind: 'document', caption: 'updated resume'
    })
    await service.sendConfirmedAttachment('caption-boundary', snapshot, {
      canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456,
      mediaKind: 'document', caption: 'x'.repeat(1_024)
    })
    await expect(service.sendConfirmedAttachment('caption-too-long', snapshot, {
      canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456,
      mediaKind: 'document', caption: 'x'.repeat(1_025)
    })).rejects.toThrow(/1024/i)

    expect(client.sentFiles).toHaveLength(3)
    expect(client.sentFiles[0]!.peer).toEqual({ trustedPeer: 0 })
    expect(client.sentFiles[0]!.params).toMatchObject({ caption: '  best picture  ', forceDocument: false, fileSize: 123, workers: 1 })
    expect(client.sentFiles[0]!.params.file).toMatchObject({ name: 'life.jpg', size: 123, path: 'C:\\approved\\life.jpg' })
    expect(client.sentFiles[1]!.params).toMatchObject({ caption: 'updated resume', forceDocument: true, fileSize: 456, workers: 1 })
    expect(client.sentFiles[2]!.params.caption).toHaveLength(1_024)
    await expect(service.sendConfirmedAttachment('photo-call', snapshot, {
      canonicalPath: 'C:\\approved\\life.jpg', fileName: 'life.jpg', sizeBytes: 123, mediaKind: 'photo'
    })).rejects.toThrow(/already handled/i)
    expect(client.sentFiles).toHaveLength(3)
  })

  it('supports Saved Messages and rejects channels, disconnects, and concurrent sends', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Saved Messages', 0), dialog('News', 1, { kind: 'channel' })]
    const service = createService(await createFolder(), client)
    await service.connect()
    const [saved] = await service.searchRecipients('saved')
    await service.sendConfirmedAttachment('saved-call', service.snapshotRecipient(saved!.resultId)!, {
      canonicalPath: 'C:\\approved\\note.txt', fileName: 'note.txt', sizeBytes: 10, mediaKind: 'document'
    })
    const [channel] = await service.searchRecipients('news')
    await expect(service.sendConfirmedAttachment('channel-call', service.snapshotRecipient(channel!.resultId)!, {
      canonicalPath: 'C:\\approved\\note.txt', fileName: 'note.txt', sizeBytes: 10, mediaKind: 'document'
    })).rejects.toThrow(/channel/i)

    let release!: () => void
    client.sendFileHandler = () => new Promise((resolve) => { release = () => resolve({ id: 2 }) })
    const [savedAgain] = await service.searchRecipients('saved')
    const first = service.sendConfirmedAttachment('in-flight-1', service.snapshotRecipient(savedAgain!.resultId)!, {
      canonicalPath: 'C:\\approved\\note.txt', fileName: 'note.txt', sizeBytes: 10, mediaKind: 'document'
    })
    await expect(service.sendConfirmedAttachment('in-flight-2', service.snapshotRecipient(savedAgain!.resultId)!, {
      canonicalPath: 'C:\\approved\\note.txt', fileName: 'note.txt', sizeBytes: 10, mediaKind: 'document'
    })).rejects.toThrow(/already being sent/i)
    release()
    await first
    await service.logout()
    await expect(service.sendConfirmedAttachment('disconnected', service.snapshotRecipient(savedAgain!.resultId)!, {
      canonicalPath: 'C:\\approved\\note.txt', fileName: 'note.txt', sizeBytes: 10, mediaKind: 'document'
    })).rejects.toThrow(/not connected/i)
  })

  it('marks a timeout after upload begins uncertain and cooperatively cancels without retrying', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    client.sendFileHandler = async (_peer, params) => {
      params.progressCallback(0.25)
      return new Promise(() => undefined)
    }
    const service = new TelegramService(await createFolder(), createSafeStorage(), () => undefined, credentials, () => client as any, () => 5)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    const snapshot = service.snapshotRecipient(recipient!.resultId)!
    const attachment = { canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456, mediaKind: 'document' as const }
    await expect(service.sendConfirmedAttachment('timeout-call', snapshot, attachment)).rejects.toMatchObject({ uncertain: true })
    expect(client.sentFiles[0]!.params.progressCallback.isCanceled).toBe(true)
    await expect(service.sendConfirmedAttachment('timeout-call', snapshot, attachment)).rejects.toThrow(/already handled/i)
    expect(client.sentFiles).toHaveLength(1)
  })

  it('classifies FloodWait as definitive and does not retry the handled call', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    client.sendFileHandler = async () => { throw new Error('FLOOD_WAIT_30') }
    const service = createService(await createFolder(), client)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    const snapshot = service.snapshotRecipient(recipient!.resultId)!
    const attachment = { canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456, mediaKind: 'document' as const }

    await expect(service.sendConfirmedAttachment('flood-call', snapshot, attachment)).rejects.toMatchObject({
      uncertain: false,
      message: expect.stringMatching(/wait/i)
    })
    await expect(service.sendConfirmedAttachment('flood-call', snapshot, attachment)).rejects.toThrow(/already handled/i)
    expect(client.sentFiles).toHaveLength(1)
  })

  it('classifies a timeout before upload progress as definitely not sent and does not retry', async () => {
    const client = new FakeClient()
    client.dialogs = [dialog('Ravi', 0)]
    client.sendFileHandler = async () => new Promise(() => undefined)
    const service = new TelegramService(await createFolder(), createSafeStorage(), () => undefined, credentials, () => client as any, () => 5)
    await service.connect()
    const [recipient] = await service.searchRecipients('ravi')
    const snapshot = service.snapshotRecipient(recipient!.resultId)!
    const attachment = { canonicalPath: 'C:\\approved\\resume.pdf', fileName: 'resume.pdf', sizeBytes: 456, mediaKind: 'document' as const }

    await expect(service.sendConfirmedAttachment('pre-progress-timeout', snapshot, attachment)).rejects.toMatchObject({
      uncertain: false,
      message: expect.stringMatching(/before it started.*nothing was sent/i)
    })
    expect(client.sentFiles[0]!.params.progressCallback.isCanceled).toBe(true)
    await expect(service.sendConfirmedAttachment('pre-progress-timeout', snapshot, attachment)).rejects.toThrow(/already handled/i)
    expect(client.sentFiles).toHaveLength(1)
  })
})
