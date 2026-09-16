/**
 * Exact checks on who is invoking IPC and what Lumi's window may navigate to.
 *
 * Owning-window checks alone accept a subframe or a navigated-away document in
 * that window. These helpers additionally require the top frame and Lumi's own
 * renderer URL, compared structurally rather than by string prefix (a prefix
 * test accepts `http://localhost:5173.evil.example`).
 */

export interface RendererLocation {
  /** `ELECTRON_RENDERER_URL` in development, otherwise undefined. */
  developmentUrl?: string
  /** The packaged renderer entry as a file URL. */
  fileUrl: string
}

export function isTrustedRendererUrl(candidate: string, location: RendererLocation): boolean {
  let url: URL
  try {
    url = new URL(candidate)
  } catch {
    return false
  }
  if (location.developmentUrl !== undefined) {
    let expected: URL
    try {
      expected = new URL(location.developmentUrl)
    } catch {
      return false
    }
    return (
      (url.protocol === 'http:' || url.protocol === 'https:') &&
      url.origin === expected.origin &&
      (url.pathname === '/' || url.pathname === '/index.html') &&
      url.username === '' &&
      url.password === ''
    )
  }
  const expected = new URL(location.fileUrl)
  return url.protocol === 'file:' && url.host === expected.host && decodeURIComponent(url.pathname).toLowerCase() === decodeURIComponent(expected.pathname).toLowerCase()
}

export interface SenderFrameLike {
  url: string
  processId: number
  routingId: number
  parent: unknown
}

export interface SenderCheckInput<Frame extends SenderFrameLike> {
  senderFrame: Frame | null | undefined
  mainFrame: Frame | undefined
  location: RendererLocation
}

/** True only for the top-level frame of Lumi's window showing Lumi's renderer. */
export function isTrustedSenderFrame<Frame extends SenderFrameLike>({ senderFrame, mainFrame, location }: SenderCheckInput<Frame>): boolean {
  if (!senderFrame || !mainFrame || senderFrame.parent !== null) return false
  const sameFrame = senderFrame === mainFrame ||
    (senderFrame.processId === mainFrame.processId && senderFrame.routingId === mainFrame.routingId)
  return sameFrame && isTrustedRendererUrl(senderFrame.url, location)
}
