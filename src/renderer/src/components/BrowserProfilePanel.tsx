import { useEffect, useRef, useState } from 'react'
import type {
  AgentApi,
  AgentBrowserProfileView,
  AgentLoginTakeoverView
} from '../../../shared/agent-contracts'
import './components.css'

export interface BrowserProfilePanelProps {
  agent: AgentApi
  onClose: () => void
  /** Injectable for tests. */
  pollIntervalMs?: number
}

/**
 * Milestone 8a S2: manual login and human takeover.
 *
 * Every word here is app-authored. Nothing from a website ever becomes a
 * label, a control or a line of text in this panel -- the runtime never
 * sends this component page content in the first place, only ids, a site
 * name it already owned, a status and a closed reason code.
 *
 * Polling only reads. The only things that change durable state are the
 * three trusted clicks: "Sign in manually", "I'm signed in" and "Cancel
 * login"/"Cancel", each of which names an id and the revision this panel
 * last saw on screen.
 */
export function BrowserProfilePanel({ agent, onClose, pollIntervalMs = 2_000 }: BrowserProfilePanelProps) {
  const [profiles, setProfiles] = useState<AgentBrowserProfileView[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string>()
  const [busy, setBusy] = useState<string>()
  const [confirmDialogFor, setConfirmDialogFor] = useState<string>()
  const [takeovers, setTakeovers] = useState<Record<string, AgentLoginTakeoverView>>({})
  const [statusMessage, setStatusMessage] = useState<Record<string, string>>({})
  const busyRef = useRef<string | undefined>(undefined)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function poll(): Promise<void> {
      const result = await agent.listBrowserProfiles()
      if (cancelled || !mounted.current) return
      if (result.ok) {
        setProfiles(result.value)
        setError(undefined)
      } else {
        setError(result.error.message)
      }
      setLoaded(true)
    }
    void poll()
    const timer = setInterval(() => void poll(), pollIntervalMs)
    return () => { cancelled = true; clearInterval(timer) }
  }, [agent, pollIntervalMs])

  // Poll every open takeover so expiry (the runtime's own watchdog) and an
  // interruption from a restart are both reflected here without a click.
  useEffect(() => {
    const open = Object.entries(takeovers).filter(([, view]) => view.attempt.status === 'OPEN' || view.attempt.status === 'UNCONFIRMED')
    if (open.length === 0) return
    let cancelled = false
    const timer = setInterval(() => {
      void Promise.all(
        open.map(async ([profileId, view]) => {
          const result = await agent.getLoginTakeover(profileId, view.attempt.attemptId)
          if (cancelled || !mounted.current || !result.ok) return
          setTakeovers((current) => {
            const existing = current[profileId]
            if (!existing) return current
            return { ...current, [profileId]: { ...existing, attempt: result.value } }
          })
        })
      )
    }, pollIntervalMs)
    return () => { cancelled = true; clearInterval(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-derives `open` from `takeovers` each render on purpose.
  }, [agent, pollIntervalMs, takeovers])

  async function run(key: string, work: () => Promise<void>): Promise<void> {
    if (busyRef.current) return
    busyRef.current = key
    setBusy(key)
    try {
      await work()
    } finally {
      busyRef.current = undefined
      if (mounted.current) setBusy(undefined)
    }
  }

  function startTakeover(profile: AgentBrowserProfileView): void {
    setConfirmDialogFor(undefined)
    void run(`open:${profile.profileId}`, async () => {
      const result = await agent.openLoginWindow(profile.profileId, profile.revision)
      if (!mounted.current) return
      if (result.ok) {
        setTakeovers((current) => ({ ...current, [profile.profileId]: result.value }))
        setStatusMessage((current) => ({ ...current, [profile.profileId]: '' }))
      } else {
        setStatusMessage((current) => ({ ...current, [profile.profileId]: result.error.message }))
      }
    })
  }

  function confirmSignedIn(profileId: string): void {
    const view = takeovers[profileId]
    if (!view) return
    void run(`confirm:${profileId}`, async () => {
      const result = await agent.confirmSignedIn(profileId, view.attempt.attemptId, view.profile.revision)
      if (!mounted.current) return
      if (result.ok) {
        setTakeovers((current) => { const next = { ...current }; delete next[profileId]; return next })
        setStatusMessage((current) => ({ ...current, [profileId]: describeOutcome(result.value) }))
      } else {
        setStatusMessage((current) => ({ ...current, [profileId]: result.error.message }))
      }
    })
  }

  function cancelLogin(profileId: string): void {
    const view = takeovers[profileId]
    if (!view) return
    void run(`cancel:${profileId}`, async () => {
      const result = await agent.cancelLogin(profileId, view.attempt.attemptId, view.profile.revision)
      if (!mounted.current) return
      setTakeovers((current) => { const next = { ...current }; delete next[profileId]; return next })
      setStatusMessage((current) => ({
        ...current,
        [profileId]: result.ok ? 'Sign-in cancelled.' : result.error.message
      }))
    })
  }

  return (
    <div className="agent-task-panel" data-testid="browser-profile-panel">
      <header className="settings-header">
        <h2>Sign-in profiles</h2>
        <button className="icon-button" type="button" aria-label="Close sign-in profiles" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
      {!loaded && <p className="workspace-note">Loading…</p>}
      {error && <p className="workspace-note" role="alert">{error}</p>}
      {loaded && profiles.length === 0 && !error && (
        <p className="workspace-note">Lumi has no managed sign-in profiles yet.</p>
      )}
      {profiles.map((profile) => {
        const takeover = takeovers[profile.profileId]
        const attemptOpen = takeover && (takeover.attempt.status === 'OPEN' || takeover.attempt.status === 'UNCONFIRMED')
        const tone = profile.status === 'AUTHENTICATED' ? 'tone-success' : attemptOpen ? 'tone-uncertain' : ''
        return (
          <article key={profile.profileId} className={`agent-booking-card ${tone}`} role="group"
            aria-label={profile.label} data-testid="browser-profile-card"
            data-profile-id={profile.profileId} data-profile-status={profile.status}>
            <p className="lifelens-card-eyebrow">SIGN-IN PROFILE</p>
            <h3 className="lifelens-card-heading">{profile.label}</h3>
            <dl className="agent-booking-details">
              <dt>Site</dt><dd>{profile.site}</dd>
              <dt>Status</dt><dd data-testid="browser-profile-status">{describeStatus(profile.status)}</dd>
            </dl>
            {statusMessage[profile.profileId] && (
              <p className="workspace-note" role="status">{statusMessage[profile.profileId]}</p>
            )}

            {!attemptOpen && confirmDialogFor !== profile.profileId && profile.status !== 'AUTHENTICATED' && profile.status !== 'DELETED' && (
              <button className="primary-button" type="button" disabled={Boolean(busy)}
                onClick={() => setConfirmDialogFor(profile.profileId)}>
                Sign in manually
              </button>
            )}

            {confirmDialogFor === profile.profileId && (
              <div className="agent-booking-details" data-testid="sign-in-confirm-dialog">
                <p><strong>SIGN IN TO {profile.label.toUpperCase()}</strong></p>
                <p>Lumi needs you to sign in yourself.</p>
                <p>Lumi will not:</p>
                <ul className="agent-evidence">
                  <li>see your password</li>
                  <li>see your OTP</li>
                  <li>solve CAPTCHA</li>
                  <li>use your passkey</li>
                  <li>send the login page to an AI model</li>
                </ul>
                <p>A separate Lumi browser window will open.<br />You control it until you return here.</p>
                <button className="text-button" type="button" disabled={Boolean(busy)}
                  onClick={() => setConfirmDialogFor(undefined)}>
                  Cancel
                </button>
                <button className="primary-button" type="button" disabled={Boolean(busy)}
                  onClick={() => startTakeover(profile)}>
                  Sign in manually
                </button>
              </div>
            )}

            {attemptOpen && (
              <div className="agent-booking-details" data-testid="takeover-banner" data-attempt-id={takeover.attempt.attemptId}
                data-attempt-status={takeover.attempt.status}>
                <p><strong>YOU ARE CONTROLLING THE BROWSER</strong></p>
                <p>Finish signing in yourself.</p>
                <p>Lumi is not reading or controlling this browser right now.</p>
                <button className="text-button" type="button" disabled={Boolean(busy)}
                  onClick={() => cancelLogin(profile.profileId)}>
                  Cancel login
                </button>
                <button className="primary-button" type="button" disabled={Boolean(busy)}
                  onClick={() => confirmSignedIn(profile.profileId)}>
                  I'm signed in
                </button>
              </div>
            )}
          </article>
        )
      })}
      </div>
    </div>
  )
}

function describeStatus(status: AgentBrowserProfileView['status']): string {
  switch (status) {
    case 'AUTHENTICATED': return 'Signed in'
    case 'NEEDS_LOGIN': return 'Needs sign-in'
    case 'NEW': return 'Not yet opened'
    case 'DELETED': return 'Deleted'
    default: return status
  }
}

/** Controller-authored outcome text for a completed confirmation. Never
 * page text: `refusalReason` is one of a closed set of stable codes. */
function describeOutcome(view: AgentLoginTakeoverView): string {
  if (!view.refusalReason) return 'Signed in.'
  switch (view.refusalReason) {
    case 'login_credential_surface_present':
      return "Lumi still sees a sign-in step. Finish signing in yourself."
    case 'login_not_on_profile_site':
      return `Finish signing in on ${view.profile.site}. The browser ended somewhere else.`
    case 'login_no_page':
      return 'The sign-in window closed before Lumi could check. Try again.'
    default:
      return 'Sign-in did not complete. Try again.'
  }
}
