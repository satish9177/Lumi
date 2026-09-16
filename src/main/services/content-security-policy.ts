/**
 * The renderer Content Security Policy.
 *
 * The renderer loads only its own bundle, talks to exactly one remote origin
 * (the OpenAI Realtime call negotiation, authorised by an ephemeral credential
 * minted in main), and never needs frames, plugins, workers or form posts.
 * Loopback is deliberately absent from `connect-src`: the renderer has no
 * business reaching the agent runtime or the browser worker, even if it could
 * guess a port. WebRTC media is not governed by `connect-src`.
 *
 * Kept free of Electron imports so the build config can embed it as well.
 */

const SHARED_DIRECTIVES = [
  "default-src 'none'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "media-src 'self' blob: mediastream:",
  "font-src 'self' data:",
  "worker-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "manifest-src 'none'"
]

export const PRODUCTION_CONTENT_SECURITY_POLICY = [
  "script-src 'self'",
  "connect-src 'self' https://api.openai.com",
  ...SHARED_DIRECTIVES
].join('; ')

/**
 * The dev server needs the React refresh preamble (inline) and its own HMR
 * socket. Both are limited to the exact dev origin; no wildcard loopback.
 */
export function developmentContentSecurityPolicy(developmentUrl: string): string {
  const origin = new URL(developmentUrl)
  const socketOrigin = `${origin.protocol === 'https:' ? 'wss:' : 'ws:'}//${origin.host}`
  return [
    `script-src 'self' 'unsafe-inline' ${origin.origin}`,
    `connect-src 'self' ${origin.origin} ${socketOrigin} https://api.openai.com`,
    ...SHARED_DIRECTIVES
  ].join('; ')
}
