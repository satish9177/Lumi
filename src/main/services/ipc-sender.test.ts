import { describe, expect, it } from 'vitest'
import { isTrustedRendererUrl, isTrustedSenderFrame, type SenderFrameLike } from './ipc-sender'
import { PRODUCTION_CONTENT_SECURITY_POLICY, developmentContentSecurityPolicy } from './content-security-policy'

const packaged = { fileUrl: 'file:///C:/Program%20Files/Lumi/resources/app.asar/out/renderer/index.html' }
const development = { developmentUrl: 'http://localhost:5173', fileUrl: packaged.fileUrl }

describe('isTrustedRendererUrl', () => {
  it('accepts only Lumi’s own renderer document', () => {
    expect(isTrustedRendererUrl(packaged.fileUrl, packaged)).toBe(true)
    expect(isTrustedRendererUrl(`${packaged.fileUrl}#settings`, packaged)).toBe(true)
    expect(isTrustedRendererUrl('file:///C:/Users/x/evil/index.html', packaged)).toBe(false)
    expect(isTrustedRendererUrl(`${packaged.fileUrl}/../../../../evil.html`, packaged)).toBe(false)
    expect(isTrustedRendererUrl('https://example.com/', packaged)).toBe(false)
    expect(isTrustedRendererUrl('not a url', packaged)).toBe(false)

    expect(isTrustedRendererUrl('http://localhost:5173/', development)).toBe(true)
    expect(isTrustedRendererUrl('http://localhost:5173/index.html?x=1', development)).toBe(true)
    // A prefix check would accept both of these.
    expect(isTrustedRendererUrl('http://localhost:5173.evil.example/', development)).toBe(false)
    expect(isTrustedRendererUrl('http://localhost:51730/', development)).toBe(false)
    expect(isTrustedRendererUrl('http://localhost:5173/other.html', development)).toBe(false)
    expect(isTrustedRendererUrl('http://user:pw@localhost:5173/', development)).toBe(false)
    expect(isTrustedRendererUrl(packaged.fileUrl, development)).toBe(false)
  })
})

describe('isTrustedSenderFrame', () => {
  const main: SenderFrameLike = { url: packaged.fileUrl, processId: 7, routingId: 1, parent: null }

  it('accepts the top frame of the trusted document', () => {
    expect(isTrustedSenderFrame({ senderFrame: main, mainFrame: main, location: packaged })).toBe(true)
    expect(isTrustedSenderFrame({ senderFrame: { ...main }, mainFrame: main, location: packaged })).toBe(true)
  })

  it('refuses subframes, other frames, missing frames and navigated documents', () => {
    expect(isTrustedSenderFrame({ senderFrame: { ...main, parent: main }, mainFrame: main, location: packaged })).toBe(false)
    expect(isTrustedSenderFrame({ senderFrame: { ...main, routingId: 2 }, mainFrame: main, location: packaged })).toBe(false)
    expect(isTrustedSenderFrame({ senderFrame: { ...main, processId: 8 }, mainFrame: main, location: packaged })).toBe(false)
    expect(isTrustedSenderFrame({ senderFrame: null, mainFrame: main, location: packaged })).toBe(false)
    expect(isTrustedSenderFrame({ senderFrame: main, mainFrame: undefined, location: packaged })).toBe(false)
    const navigated = { ...main, url: 'https://attacker.example/' }
    expect(isTrustedSenderFrame({ senderFrame: navigated, mainFrame: navigated, location: packaged })).toBe(false)
  })
})

describe('renderer content security policy', () => {
  it('never allows the renderer to reach loopback services', () => {
    for (const policy of [PRODUCTION_CONTENT_SECURITY_POLICY, developmentContentSecurityPolicy('http://localhost:5173')]) {
      expect(policy).toContain("default-src 'none'")
      expect(policy).toContain("object-src 'none'")
      expect(policy).toContain("frame-src 'none'")
      expect(policy).not.toContain('127.0.0.1')
      expect(policy).not.toMatch(/\*/)
      expect(policy).not.toContain("'unsafe-eval'")
    }
    expect(PRODUCTION_CONTENT_SECURITY_POLICY).toContain("script-src 'self';")
    expect(PRODUCTION_CONTENT_SECURITY_POLICY).not.toContain('localhost')
    expect(PRODUCTION_CONTENT_SECURITY_POLICY).toContain("connect-src 'self' https://api.openai.com;")
    const dev = developmentContentSecurityPolicy('http://localhost:5173')
    expect(dev).toContain('connect-src \'self\' http://localhost:5173 ws://localhost:5173 https://api.openai.com')
  })
})
