/**
 * The Milestone 7a destination policy, as Electron main applies it.
 *
 * Main checks a typed URL before anything is stored so the user gets an
 * immediate, specific refusal. It is not the authority: the Python runtime
 * applies the same rules when the task is created and again before execution,
 * and the browser worker applies them to the approved URL, every redirect hop
 * and every request the page makes. The rules and refusal codes mirror
 * `services/agent/app/domain/public_url.py`; tests on both sides pin them.
 *
 * Only `https:` public host names on a trusted allowlist, default port, no
 * credentials, no IP literal, no local or reserved suffix. Exact loopback test
 * origins exist for deterministic fixtures and are configured, never inferred.
 */

export const POLICY_VERSION = 'public-url-v1'
/**
 * Milestone 7b research: the same shape and the same refusals, with the host
 * allowlist replaced by "any host that is not local, private or reserved". A
 * research task cannot know its destinations in advance. Layers 1 and 3 are
 * untouched, and the Milestone 7a inspection policy keeps its allowlist.
 */
export const RESEARCH_POLICY_VERSION = 'public-research-v1'
const MAX_URL_LENGTH = 2_048
const MAX_ALLOWED_HOSTS = 64

const LOCAL_SUFFIXES = [
  'localhost', 'local', 'localdomain', 'internal', 'intranet', 'private', 'corp', 'home', 'lan',
  'home.arpa', 'arpa', 'test', 'example', 'invalid', 'onion'
]
const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/
const NUMERIC_LABEL = /^(0x[0-9a-f]*|[0-9]+)$/
const TEST_ORIGIN = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})$/
const URL_CHARACTERS = /^[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%|^{}]+$/
const IPV4 = /^\d{1,3}(\.\d{1,3}){3}$/

export class UrlPolicyError extends Error {
  constructor(readonly code: string) {
    super(`The destination was refused (${code}).`)
    this.name = 'UrlPolicyError'
  }
}

export interface CheckedUrl {
  url: string
  host: string
  testOrigin: boolean
}

function checkHostName(host: string): void {
  if (!host || host.length > 253 || host.endsWith('.')) throw new UrlPolicyError('invalid_host')
  const labels = host.split('.')
  if (labels.length < 2) throw new UrlPolicyError('local_host')
  if (labels.some((label) => !LABEL.test(label))) throw new UrlPolicyError('invalid_host')
  if (NUMERIC_LABEL.test(labels[labels.length - 1])) throw new UrlPolicyError('ip_literal')
  for (const suffix of LOCAL_SUFFIXES) {
    if (host === suffix || host.endsWith(`.${suffix}`)) throw new UrlPolicyError('local_host')
  }
}

function splitEntries(raw: string | readonly string[]): string[] {
  return (typeof raw === 'string' ? raw.split(',') : [...raw]).map((entry) => entry.trim()).filter(Boolean)
}

export function parseAllowedHosts(raw: string | readonly string[]): string[] {
  const entries = splitEntries(raw)
  if (entries.length > MAX_ALLOWED_HOSTS) throw new Error(`At most ${MAX_ALLOWED_HOSTS} inspection hosts may be configured.`)
  return [...new Set(entries.map((entry) => {
    const value = entry.toLowerCase()
    const wildcard = value.startsWith('*.')
    const host = wildcard ? value.slice(2) : value
    try {
      checkHostName(host)
    } catch {
      throw new Error('An inspection host entry is invalid.')
    }
    return wildcard ? `*.${host}` : host
  }))]
}

export function parseTestOrigins(raw: string | readonly string[]): string[] {
  return [...new Set(splitEntries(raw).map((entry) => {
    const match = TEST_ORIGIN.exec(entry)
    if (!match || Number(match[1]) < 1 || Number(match[1]) > 65_535) {
      throw new Error('Inspection test origins must be http://127.0.0.1:<port>.')
    }
    return entry
  }))]
}

function hostAllowed(host: string, allowed: ReadonlySet<string>): boolean {
  if (allowed.has(host)) return true
  const labels = host.split('.')
  for (let index = 1; index < labels.length - 1; index += 1) {
    if (allowed.has(`*.${labels.slice(index).join('.')}`)) return true
  }
  return false
}

export class PublicUrlPolicy {
  private readonly allowedHosts: ReadonlySet<string>
  private readonly testOrigins: ReadonlySet<string>
  private readonly allowAnyPublicHost: boolean
  readonly version: string

  constructor(options: {
    allowedHosts?: readonly string[]
    testOrigins?: readonly string[]
    /** Milestone 7b research. Never set on the inspection policy. */
    allowAnyPublicHost?: boolean
    version?: string
  } = {}) {
    this.allowedHosts = new Set(parseAllowedHosts(options.allowedHosts ?? []))
    this.testOrigins = new Set(parseTestOrigins(options.testOrigins ?? []))
    this.allowAnyPublicHost = options.allowAnyPublicHost === true
    this.version = options.version ?? POLICY_VERSION
  }

  get configured(): boolean {
    return this.allowedHosts.size > 0 || this.testOrigins.size > 0 || this.allowAnyPublicHost
  }

  /**
   * A URL as the user typed it -> the one canonical spelling, or a refusal.
   * A bare host ("github.com/x") is read as https. The fragment is dropped.
   */
  canonicalize(input: string): CheckedUrl {
    const trimmed = input.trim()
    if (!trimmed || trimmed.length > MAX_URL_LENGTH || /[\s\\]/.test(trimmed)) throw new UrlPolicyError('invalid_url')
    const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(trimmed) ? trimmed : `https://${trimmed}`
    const scheme = withScheme.slice(0, withScheme.indexOf(':')).toLowerCase()
    if (scheme !== 'http' && scheme !== 'https') throw new UrlPolicyError('scheme_not_allowed')
    // Checked on the raw authority: the WHATWG parser would quietly decode or
    // normalise these into something that looks harmless.
    const authority = /^[a-z]+:\/\/([^/?#]*)/i.exec(withScheme)?.[1] ?? ''
    if (authority.includes('@')) throw new UrlPolicyError('credentials_in_url')
    if (authority.includes('%') || authority.includes('[')) throw new UrlPolicyError('invalid_host')
    let parsed: URL
    try {
      parsed = new URL(withScheme)
    } catch {
      throw new UrlPolicyError('invalid_url')
    }
    const port = parsed.port ? `:${parsed.port}` : ''
    return this.check(`${parsed.protocol}//${parsed.hostname}${port}${parsed.pathname}${parsed.search}`)
  }

  /** The same rules as the runtime, applied to an already canonical URL. */
  check(raw: string): CheckedUrl {
    if (typeof raw !== 'string' || !raw || raw.length > MAX_URL_LENGTH) throw new UrlPolicyError('invalid_url')
    if (raw !== raw.trim() || !URL_CHARACTERS.test(raw)) throw new UrlPolicyError('invalid_url')
    const match = /^([a-z][a-z0-9+.-]*):(.*)$/i.exec(raw)
    if (!match) throw new UrlPolicyError('invalid_url')
    const scheme = match[1].toLowerCase()
    if (scheme !== 'http' && scheme !== 'https') throw new UrlPolicyError('scheme_not_allowed')
    const rest = /^\/\/([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$/.exec(match[2])
    if (!rest) throw new UrlPolicyError('invalid_url')
    const authority = rest[1]
    if (authority.includes('@')) throw new UrlPolicyError('credentials_in_url')
    if (authority.includes('%') || authority.includes('[') || authority.includes(']')) throw new UrlPolicyError('invalid_host')
    const path = rest[2] || '/'
    const query = rest[3] && rest[3].length > 1 ? rest[3] : ''
    if (scheme === 'http') {
      const origin = `http://${authority.toLowerCase()}`
      if (this.testOrigins.has(origin)) return { url: `${origin}${path}${query}`, host: '127.0.0.1', testOrigin: true }
      throw new UrlPolicyError('https_required')
    }
    const separator = authority.indexOf(':')
    const hostPart = separator >= 0 ? authority.slice(0, separator) : authority
    const port = separator >= 0 ? authority.slice(separator + 1) : ''
    if (port && port !== '443') throw new UrlPolicyError('port_not_allowed')
    const host = hostPart.toLowerCase()
    if (IPV4.test(host)) throw new UrlPolicyError('ip_literal')
    checkHostName(host)
    if (!this.allowAnyPublicHost && !hostAllowed(host, this.allowedHosts)) {
      throw new UrlPolicyError('destination_not_allowed')
    }
    return { url: `https://${host}${path}${query}`, host, testOrigin: false }
  }
}

/** Plain-language reasons for the refusal codes, for main to show. */
export function describeRefusal(code: string): string {
  switch (code) {
    case 'scheme_not_allowed': return 'Only https web pages can be inspected.'
    case 'https_required': return 'Only https web pages can be inspected.'
    case 'credentials_in_url': return 'Remove the user name or password from the address.'
    case 'ip_literal': return 'Addresses that are raw IP numbers cannot be inspected.'
    case 'local_host': return 'Local and private network addresses cannot be inspected.'
    case 'port_not_allowed': return 'Only standard https addresses can be inspected.'
    case 'destination_not_allowed': return 'That website is not on Lumi’s list of sites it may inspect.'
    default: return 'That address cannot be inspected.'
  }
}
