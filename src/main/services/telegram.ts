import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { Api, TelegramClient } from 'telegram'
import { Logger, LogLevel } from 'telegram/extensions/Logger'
import { StringSession } from 'telegram/sessions'
import { getDisplayName } from 'telegram/Utils'
import type { TelegramAccount, TelegramRecipient, TelegramStatus } from '../../shared/contracts'

const SESSION_FILE = 'telegram-session.bin'
const MAX_MESSAGE_LENGTH = 4_096
const MAX_RECIPIENTS = 10
const DIALOG_SCAN_LIMIT = 100

export interface SafeStoragePort {
  isEncryptionAvailable: () => boolean
  encryptString: (value: string) => Buffer
  decryptString: (value: Buffer) => string
}

interface TelegramClientPort {
  session: { save: () => unknown }
  connect: () => Promise<boolean>
  disconnect: () => Promise<void>
  checkAuthorization: () => Promise<boolean>
  getMe: () => Promise<unknown>
  signInUserWithQrCode: (
    credentials: { apiId: number; apiHash: string },
    params: {
      qrCode?: (code: { token: Buffer; expires: number }) => Promise<void>
      password?: () => Promise<string>
      onError: (error: Error) => void | Promise<boolean>
    }
  ) => Promise<unknown>
  getDialogs: (params: { limit: number }) => Promise<Array<DialogLike>>
  sendMessage: (peer: any, params: any) => Promise<{ id?: number }>
  invoke?: (request: any) => Promise<unknown>
}

interface DialogLike {
  entity?: object
  inputEntity: unknown
  name?: string
  title?: string
  isUser?: boolean
  isGroup?: boolean
  isChannel?: boolean
}

interface RecipientCandidate {
  peer: unknown
  displayName: string
  username?: string
  kind: TelegramRecipient['kind']
  recentRank: number
}

interface PendingPassword {
  resolve: (password: string) => void
  reject: (error: Error) => void
}

type ClientFactory = (session: string) => TelegramClientPort

export class TelegramService {
  private client: TelegramClientPort | undefined
  private readonly recipients = new Map<string, RecipientCandidate>()
  private readonly handledCallIds = new Set<string>()
  private pendingPassword: PendingPassword | undefined
  private loginGeneration = 0
  private authTask: Promise<void> | undefined
  private status: TelegramStatus = { state: 'disconnected' }

  constructor(
    private readonly userDataPath: string,
    private readonly secureStorage: SafeStoragePort,
    private readonly emitStatus: (status: TelegramStatus) => void,
    private readonly credentials = readCredentials(),
    private readonly createClient: ClientFactory = createGramJsClient
  ) {}

  async initialize(): Promise<TelegramStatus> {
    if (!this.credentials) {
      return this.updateStatus({ state: 'disconnected' })
    }

    if (!this.secureStorage.isEncryptionAvailable()) {
      return this.updateStatus({ state: 'disconnected', message: 'Telegram session storage is unavailable on this device.' })
    }

    let encrypted: Buffer
    try {
      encrypted = await readFile(this.sessionPath())
    } catch {
      return this.updateStatus({ state: 'disconnected' })
    }

    try {
      const session = this.secureStorage.decryptString(encrypted)
      const client = this.createClient(session)
      await client.connect()
      if (!await client.checkAuthorization()) {
        await client.disconnect()
        await this.deletePersistedSession()
        return this.updateStatus({ state: 'disconnected' })
      }

      this.client = client
      return this.authorizedStatus(await client.getMe())
    } catch {
      await this.disconnectClient()
      await this.deletePersistedSession()
      return this.updateStatus({ state: 'disconnected', message: 'Saved Telegram session could not be read. Connect again.' })
    }
  }

  getStatus(): TelegramStatus {
    return this.status
  }

  async connect(): Promise<TelegramStatus> {
    const credentials = this.requireCredentials()
    if (this.status.state === 'connected') {
      return this.status
    }

    await this.cancelLogin()
    const generation = ++this.loginGeneration
    const client = this.createClient('')
    this.client = client

    try {
      await client.connect()
      if (await client.checkAuthorization()) {
        return this.finishAuthorization(client, await client.getMe())
      }
    } catch (error) {
      await this.disconnectClient()
      return this.updateStatus({ state: 'error', message: mapTelegramError(error) })
    }

    this.updateStatus({ state: 'connecting', message: 'Waiting for Telegram QR code.' })
    this.authTask = client.signInUserWithQrCode(credentials, {
      qrCode: async ({ token, expires }) => {
        if (generation !== this.loginGeneration) {
          throw new Error('Telegram login was cancelled.')
        }
        this.updateStatus({
          state: 'connecting',
          qrUrl: `tg://login?token=${token.toString('base64url')}`,
          expiresAt: new Date(expires * 1_000).toISOString(),
          message: 'Scan this code in Telegram on your phone.'
        })
      },
      password: async () => {
        if (generation !== this.loginGeneration) {
          throw new Error('Telegram login was cancelled.')
        }
        this.updateStatus({ state: 'awaiting_2fa', message: 'Enter your Telegram two-step verification password.' })
        return this.waitForPassword(generation)
      },
      onError: async (error) => {
        if (generation === this.loginGeneration) {
          this.updateStatus({ state: 'error', message: mapTelegramError(error) })
        }
        return true
      }
    }).then(async (account) => {
      if (generation === this.loginGeneration) {
        await this.finishAuthorization(client, account)
      }
    }).catch(async (error) => {
      if (generation === this.loginGeneration) {
        await this.disconnectClient()
        this.updateStatus({ state: 'error', message: mapTelegramError(error) })
      }
    }).finally(() => {
      if (generation === this.loginGeneration) {
        this.pendingPassword = undefined
        this.authTask = undefined
      }
    })

    return this.status
  }

  async cancelLogin(): Promise<TelegramStatus> {
    this.loginGeneration += 1
    this.pendingPassword?.reject(new Error('Telegram login was cancelled.'))
    this.pendingPassword = undefined
    this.authTask = undefined
    this.recipients.clear()
    if (this.status.state !== 'connected') {
      await this.disconnectClient()
      return this.updateStatus({ state: 'disconnected' })
    }
    return this.status
  }

  submitPassword(password: string): TelegramStatus {
    if (typeof password !== 'string' || password.length === 0 || password.length > 1_000 || !this.pendingPassword) {
      throw new Error('Telegram is not currently waiting for a two-step verification password.')
    }

    const pending = this.pendingPassword
    this.pendingPassword = undefined
    pending.resolve(password)
    return this.updateStatus({ state: 'connecting', message: 'Finishing Telegram sign-in.' })
  }

  async logout(): Promise<TelegramStatus> {
    this.loginGeneration += 1
    this.pendingPassword?.reject(new Error('Telegram login was cancelled.'))
    this.pendingPassword = undefined
    this.authTask = undefined
    this.recipients.clear()
    this.handledCallIds.clear()

    const client = this.client
    this.client = undefined
    if (client) {
      try {
        await client.invoke?.(new Api.auth.LogOut())
      } catch {
        // Local session removal and disconnect still protect this device if revoke fails.
      }
      try {
        await client.disconnect()
      } catch {
        // Nothing else can be safely done during shutdown/logout.
      }
    }
    await this.deletePersistedSession()
    return this.updateStatus({ state: 'disconnected' })
  }

  async shutdown(): Promise<void> {
    this.recipients.clear()
    this.handledCallIds.clear()
    await this.cancelLogin()
    await this.disconnectClient()
  }

  async searchRecipients(query: string): Promise<TelegramRecipient[]> {
    this.assertAuthorized()
    const normalized = normalizeQuery(query)
    const dialogs = await this.client!.getDialogs({ limit: DIALOG_SCAN_LIMIT })
    const matches: RecipientCandidate[] = []
    for (const [recentRank, dialog] of dialogs.entries()) {
      const candidate = toRecipientCandidate(dialog, recentRank)
      if (!candidate || !matchesQuery(candidate, normalized)) {
        continue
      }
      matches.push(candidate)
    }

    matches.sort((left, right) => compareRecipients(left, right, normalized))
    this.recipients.clear()
    return matches.slice(0, MAX_RECIPIENTS).map((candidate) => {
      const resultId = crypto.randomUUID()
      this.recipients.set(resultId, candidate)
      return {
        resultId,
        displayName: candidate.displayName,
        username: candidate.username,
        kind: candidate.kind,
        recentRank: candidate.recentRank
      }
    })
  }

  getRecipient(resultId: string): TelegramRecipient | undefined {
    const candidate = this.recipients.get(resultId)
    return candidate && {
      resultId,
      displayName: candidate.displayName,
      username: candidate.username,
      kind: candidate.kind,
      recentRank: candidate.recentRank
    }
  }

  async sendConfirmed(callId: string | undefined, resultId: string, message: string): Promise<void> {
    this.assertAuthorized()
    if (callId && this.handledCallIds.has(callId)) {
      throw new Error('This Telegram message was already handled.')
    }
    if (message.length === 0 || message.length > MAX_MESSAGE_LENGTH) {
      throw new Error(`Telegram messages must be between 1 and ${MAX_MESSAGE_LENGTH} characters.`)
    }
    const recipient = this.recipients.get(resultId)
    if (!recipient) {
      throw new Error('That Telegram recipient is no longer available. Search again first.')
    }
    if (recipient.kind === 'channel') {
      throw new Error('LifeLens can send personal messages and groups, not channel posts.')
    }

    if (callId) {
      this.handledCallIds.add(callId)
    }
    try {
      await this.client!.sendMessage(recipient.peer, { message, parseMode: undefined, linkPreview: false })
    } catch (error) {
      throw new Error(mapTelegramError(error))
    }
  }

  private async finishAuthorization(client: TelegramClientPort, account: unknown): Promise<TelegramStatus> {
    this.client = client
    const session = client.session.save()
    if (typeof session !== 'string' || session.length === 0) {
      throw new Error('Telegram could not create a local session.')
    }
    await this.persistSession(session)
    return this.authorizedStatus(account)
  }

  private authorizedStatus(account: unknown): TelegramStatus {
    return this.updateStatus({ state: 'connected', account: toAccount(account) })
  }

  private async persistSession(session: string): Promise<void> {
    if (!this.secureStorage.isEncryptionAvailable()) {
      throw new Error('Telegram session storage is unavailable on this device.')
    }
    const encrypted = this.secureStorage.encryptString(session)
    await mkdir(dirname(this.sessionPath()), { recursive: true })
    await writeFile(this.sessionPath(), encrypted)
  }

  private async deletePersistedSession(): Promise<void> {
    try {
      await unlink(this.sessionPath())
    } catch {
      // The absence of a session is the desired final state.
    }
  }

  private sessionPath(): string {
    return join(this.userDataPath, SESSION_FILE)
  }

  private async disconnectClient(): Promise<void> {
    const client = this.client
    this.client = undefined
    if (client) {
      try {
        await client.disconnect()
      } catch {
        // A failed disconnect must not retain trusted maps or session state.
      }
    }
  }

  private waitForPassword(generation: number): Promise<string> {
    return new Promise((resolve, reject) => {
      this.pendingPassword = {
        resolve: (password) => generation === this.loginGeneration ? resolve(password) : reject(new Error('Telegram login was cancelled.')),
        reject
      }
    })
  }

  private requireCredentials(): { apiId: number; apiHash: string } {
    if (!this.credentials) {
      throw new Error('Telegram developer credentials are not configured.')
    }
    return this.credentials
  }

  private assertAuthorized(): void {
    if (!this.client || this.status.state !== 'connected') {
      throw new Error('Telegram is not connected. Connect your personal account first.')
    }
  }

  private updateStatus(status: TelegramStatus): TelegramStatus {
    this.status = status
    this.emitStatus(status)
    return status
  }
}

export async function executeTelegramAfterConfirmation(
  service: TelegramService,
  confirmed: boolean,
  callId: string | undefined,
  recipientResultId: string,
  message: string
): Promise<{ ok: boolean; message: string; telegramSent?: boolean }> {
  if (!confirmed) {
    return { ok: false, message: 'Action cancelled. Nothing was sent.' }
  }
  await service.sendConfirmed(callId, recipientResultId, message)
  return { ok: true, message: 'Telegram message sent.', telegramSent: true }
}

function createGramJsClient(session: string): TelegramClientPort {
  const credentials = readCredentials()
  if (!credentials) {
    throw new Error('Telegram developer credentials are not configured.')
  }
  return new TelegramClient(new StringSession(session), credentials.apiId, credentials.apiHash, {
    connectionRetries: 1,
    baseLogger: new Logger(LogLevel.NONE)
  })
}

function readCredentials(): { apiId: number; apiHash: string } | undefined {
  const apiId = Number.parseInt(process.env.TELEGRAM_API_ID ?? '', 10)
  const apiHash = process.env.TELEGRAM_API_HASH?.trim()
  return Number.isSafeInteger(apiId) && apiId > 0 && apiHash ? { apiId, apiHash } : undefined
}

function toAccount(value: unknown): TelegramAccount {
  const entity = value as Record<string, unknown>
  const displayName = displayNameOf(entity) || 'Connected Telegram account'
  const username = optionalString(entity.username)
  return username ? { displayName, username } : { displayName }
}

function toRecipientCandidate(dialog: DialogLike, recentRank: number): RecipientCandidate | undefined {
  const entity = dialog.entity as Record<string, unknown> | undefined
  if (!entity || entity.deleted === true || !dialog.inputEntity) {
    return undefined
  }
  const kind = dialog.isUser ? 'user' : dialog.isGroup ? 'group' : dialog.isChannel ? 'channel' : undefined
  if (!kind) {
    return undefined
  }
  const displayName = entity.self === true ? 'Saved Messages' : displayNameOf(entity) || optionalString(dialog.title) || optionalString(dialog.name)
  if (!displayName) {
    return undefined
  }
  const username = optionalString(entity.username)
  return { peer: dialog.inputEntity, displayName, username, kind, recentRank }
}

function displayNameOf(entity: Record<string, unknown>): string {
  try {
    const displayName = getDisplayName(entity as never).trim()
    if (displayName) {
      return displayName
    }
  } catch {
    // Fall through to the structural fallback used by mocked and future entities.
  }
  return [optionalString(entity.firstName), optionalString(entity.lastName), optionalString(entity.title)].filter(Boolean).join(' ').trim()
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function normalizeQuery(query: string): string {
  if (typeof query !== 'string' || query.trim().length === 0 || query.length > 250) {
    throw new Error('Enter a short recipient name to search Telegram.')
  }
  return query.trim().toLocaleLowerCase()
}

function matchesQuery(candidate: RecipientCandidate, query: string): boolean {
  return candidate.displayName.toLocaleLowerCase().includes(query) || candidate.username?.toLocaleLowerCase().includes(query) === true
}

function compareRecipients(left: RecipientCandidate, right: RecipientCandidate, query: string): number {
  const matchRank = (candidate: RecipientCandidate): number => {
    const displayName = candidate.displayName.toLocaleLowerCase()
    const username = candidate.username?.toLocaleLowerCase()
    if (displayName === query || username === query) return 0
    if (displayName.startsWith(query) || username?.startsWith(query)) return 1
    return 2
  }
  // Recent dialogs are the deterministic tie breaker after the best visible match.
  return matchRank(left) - matchRank(right) || left.recentRank - right.recentRank || left.displayName.localeCompare(right.displayName) || left.kind.localeCompare(right.kind)
}

export function mapTelegramError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (/FLOOD_WAIT|FLOOD/i.test(message)) return 'Telegram asked you to wait before trying again.'
  if (/AUTH|SESSION_REVOKED|UNAUTHORIZED/i.test(message)) return 'Your Telegram authorisation expired. Connect again.'
  if (/PEER_ID_INVALID|PEER/i.test(message)) return 'The selected Telegram recipient is no longer available. Search again.'
  if (/USER_PRIVACY_RESTRICTED|PRIVACY/i.test(message)) return 'Telegram privacy settings prevent sending this message.'
  if (/PREMIUM|PAID/i.test(message)) return 'Telegram requires a paid or Premium action for this message.'
  if (/network|connect|timeout|socket/i.test(message)) return 'Telegram network connection failed. Check your connection and try again.'
  return 'Telegram could not complete that request. Try again from the Telegram section.'
}
