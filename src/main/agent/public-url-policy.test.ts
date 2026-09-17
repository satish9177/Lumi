import { describe, expect, it } from 'vitest'
import { PublicUrlPolicy, UrlPolicyError, parseAllowedHosts, parseTestOrigins } from './public-url-policy'

const policy = new PublicUrlPolicy({
  allowedHosts: ['github.com', '*.example.org', 'example.com'],
  testOrigins: ['http://127.0.0.1:8811']
})

function refusal(run: () => unknown): string {
  try {
    run()
  } catch (error) {
    if (error instanceof UrlPolicyError) return error.code
    throw error
  }
  throw new Error('expected a refusal')
}

describe('main destination policy (mirrors the runtime)', () => {
  it.each([
    ['https://github.com/satish9177/Lumi', 'https://github.com/satish9177/Lumi', 'github.com'],
    ['HTTPS://GitHub.com/a?b=1#frag', 'https://github.com/a?b=1', 'github.com'],
    ['github.com/satish9177/Lumi', 'https://github.com/satish9177/Lumi', 'github.com'],
    ['https://github.com', 'https://github.com/', 'github.com'],
    ['https://github.com:443/x', 'https://github.com/x', 'github.com'],
    ['https://docs.example.org/page', 'https://docs.example.org/page', 'docs.example.org'],
    ['http://127.0.0.1:8811/profiles/rated', 'http://127.0.0.1:8811/profiles/rated', '127.0.0.1']
  ])('canonicalizes %s', (input, url, host) => {
    expect(policy.canonicalize(input)).toMatchObject({ url, host })
    // The canonical form is a fixed point, exactly as the runtime requires.
    expect(policy.check(url).url).toBe(url)
  })

  it.each([
    ['file:///C:/Windows/win.ini', 'scheme_not_allowed'],
    ['javascript:alert(1)', 'scheme_not_allowed'],
    ['data:text/html,<b>x</b>', 'scheme_not_allowed'],
    ['chrome://settings', 'scheme_not_allowed'],
    ['about:blank', 'scheme_not_allowed'],
    ['ms-settings:privacy', 'scheme_not_allowed'],
    ['vscode://file/C:/x', 'scheme_not_allowed'],
    ['http://github.com/', 'https_required'],
    ['https://user:pass@github.com/', 'credentials_in_url'],
    ['https://github.com@evil.com/', 'credentials_in_url'],
    ['https://localhost/', 'local_host'],
    ['https://127.0.0.1/', 'ip_literal'],
    ['https://127.1/', 'ip_literal'],
    ['https://0x7f000001/', 'ip_literal'],
    ['https://2130706433/', 'ip_literal'],
    ['https://169.254.169.254/latest/meta-data/', 'ip_literal'],
    ['https://[::1]/', 'invalid_host'],
    ['https://metadata.google.internal/', 'local_host'],
    ['https://printer.local/', 'local_host'],
    ['https://%67ithub.com/', 'invalid_host'],
    ['https://github.com:8443/', 'port_not_allowed'],
    ['https://evil.com/', 'destination_not_allowed'],
    ['https://github.com.evil.com/', 'destination_not_allowed'],
    ['https://gist.github.com/', 'destination_not_allowed'],
    ['https://leetcode.com/u/someone/', 'destination_not_allowed'],
    ['https://github.com\\@evil.com/', 'invalid_url'],
    ['http://127.0.0.1:8812/', 'https_required'],
    ['http://localhost:8811/', 'https_required']
  ])('refuses %s with %s', (input, code) => {
    expect(refusal(() => policy.canonicalize(input))).toBe(code)
  })

  it('refuses everything when nothing is configured', () => {
    const empty = new PublicUrlPolicy()
    expect(empty.configured).toBe(false)
    expect(refusal(() => empty.canonicalize('https://github.com/'))).toBe('destination_not_allowed')
  })

  it('refuses a configuration that names a local host or a URL', () => {
    for (const entry of ['localhost', '127.0.0.1', '*.local', 'https://github.com', '*']) {
      expect(() => parseAllowedHosts(['github.com', entry])).toThrow()
    }
    for (const entry of ['http://localhost:1', 'https://127.0.0.1:1', 'http://127.0.0.1']) {
      expect(() => parseTestOrigins([entry])).toThrow()
    }
  })
})
