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
})
