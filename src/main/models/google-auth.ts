import { createSign } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

/**
 * Google Cloud access tokens for Vertex AI, minted in Electron main only.
 *
 * Supports the two Application Default Credential shapes a developer or a
 * deployment normally has:
 *
 * - `authorized_user` (`gcloud auth application-default login`): a refresh
 *   token exchanged at Google's token endpoint.
 * - `service_account`: a signed JWT assertion (RS256) exchanged for a token.
 *
 * Nothing here is ever sent to the renderer, logged, or written anywhere. The
 * credential file is read on demand and the resulting short-lived token is
 * cached in memory until shortly before it expires.
 */

const TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
const SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
const REFRESH_MARGIN_MS = 5 * 60_000
const REQUEST_TIMEOUT_MS = 15_000

export class GoogleCredentialError extends Error {
  constructor(readonly reason: 'missing' | 'unsupported' | 'exchange_failed' | 'no_project') {
    super(`Google credentials are not usable (${reason}).`)
    this.name = 'GoogleCredentialError'
  }
}

interface AuthorizedUser {
  type: 'authorized_user'
  client_id: string
  client_secret: string
  refresh_token: string
  quota_project_id?: string
}

interface ServiceAccount {
  type: 'service_account'
  client_email: string
  private_key: string
  project_id?: string
}

type Credential = AuthorizedUser | ServiceAccount

export interface GoogleTokenSource {
  accessToken(): Promise<string>
  projectId(): Promise<string>
}

export interface GoogleAuthOptions {
  environment?: NodeJS.ProcessEnv
  fetch?: typeof globalThis.fetch
  now?: () => number
  readFile?: (path: string) => Promise<string>
}

export function defaultCredentialPath(environment: NodeJS.ProcessEnv): string {
  const explicit = environment.GOOGLE_APPLICATION_CREDENTIALS?.trim()
  if (explicit) return explicit
  if (process.platform === 'win32') {
    return join(environment.APPDATA ?? join(homedir(), 'AppData', 'Roaming'), 'gcloud', 'application_default_credentials.json')
  }
  return join(homedir(), '.config', 'gcloud', 'application_default_credentials.json')
}

function parseCredential(raw: string): Credential {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    throw new GoogleCredentialError('unsupported')
  }
  const record = value as Record<string, unknown>
  if (record?.type === 'authorized_user' && typeof record.client_id === 'string' &&
      typeof record.client_secret === 'string' && typeof record.refresh_token === 'string') {
    return record as unknown as AuthorizedUser
  }
  if (record?.type === 'service_account' && typeof record.client_email === 'string' && typeof record.private_key === 'string') {
    return record as unknown as ServiceAccount
  }
  throw new GoogleCredentialError('unsupported')
}

function base64url(value: string | Buffer): string {
  return Buffer.from(value).toString('base64url')
}

export function serviceAccountAssertion(account: ServiceAccount, nowSeconds: number): string {
  const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))
  const claims = base64url(JSON.stringify({
    iss: account.client_email,
    scope: SCOPE,
    aud: TOKEN_ENDPOINT,
    iat: nowSeconds,
    exp: nowSeconds + 3_600
  }))
  const signer = createSign('RSA-SHA256')
  signer.update(`${header}.${claims}`)
  return `${header}.${claims}.${base64url(signer.sign(account.private_key))}`
}

export class ApplicationDefaultCredentials implements GoogleTokenSource {
  private cached?: { token: string; expiresAt: number }
  private pending?: Promise<string>
  private readonly environment: NodeJS.ProcessEnv
  private readonly fetchImpl: typeof globalThis.fetch
  private readonly now: () => number
  private readonly read: (path: string) => Promise<string>

  constructor(options: GoogleAuthOptions = {}) {
    this.environment = options.environment ?? process.env
    this.fetchImpl = options.fetch ?? globalThis.fetch
    this.now = options.now ?? Date.now
    this.read = options.readFile ?? ((path) => readFile(path, 'utf8'))
  }

  private async credential(): Promise<Credential> {
    let raw: string
    try {
      raw = await this.read(defaultCredentialPath(this.environment))
    } catch {
      throw new GoogleCredentialError('missing')
    }
    return parseCredential(raw)
  }

  async projectId(): Promise<string> {
    const configured = (this.environment.LUMI_VERTEX_PROJECT ?? this.environment.GOOGLE_CLOUD_PROJECT)?.trim()
    if (configured && /^[a-z][a-z0-9-]{4,61}[a-z0-9]$/.test(configured)) return configured
    const credential = await this.credential()
    const project = credential.type === 'authorized_user' ? credential.quota_project_id : credential.project_id
    if (project && /^[a-z][a-z0-9-]{4,61}[a-z0-9]$/.test(project)) return project
    throw new GoogleCredentialError('no_project')
  }

  async accessToken(): Promise<string> {
    if (this.cached && this.cached.expiresAt - REFRESH_MARGIN_MS > this.now()) return this.cached.token
    // One exchange at a time, however many callers ask.
    this.pending ??= this.exchange().finally(() => { this.pending = undefined })
    return this.pending
  }

  private async exchange(): Promise<string> {
    const credential = await this.credential()
    const body = credential.type === 'authorized_user'
      ? new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: credential.client_id,
        client_secret: credential.client_secret,
        refresh_token: credential.refresh_token
      })
      : new URLSearchParams({
        grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        assertion: serviceAccountAssertion(credential, Math.floor(this.now() / 1_000))
      })
    let response: Response
    try {
      // Always Google's fixed endpoint: a credential file cannot redirect the exchange.
      response = await this.fetchImpl(TOKEN_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
        redirect: 'error',
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
      })
    } catch {
      throw new GoogleCredentialError('exchange_failed')
    }
    if (!response.ok) throw new GoogleCredentialError('exchange_failed')
    const value = await response.json().catch(() => undefined) as Record<string, unknown> | undefined
    const token = value?.access_token
    const expiresIn = value?.expires_in
    if (typeof token !== 'string' || token.length < 10 || typeof expiresIn !== 'number') {
      throw new GoogleCredentialError('exchange_failed')
    }
    this.cached = { token, expiresAt: this.now() + expiresIn * 1_000 }
    return token
  }
}
