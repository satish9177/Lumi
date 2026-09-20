import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import {
  BOOKING_DAYS,
  TERMINAL_TASK_STATUSES,
  type AgentActionView,
  type AgentApi,
  type AgentAuthenticatedView,
  type AgentBrowserProfileView,
  type AgentDisclosureRecipient,
  type AgentDoctorProfileView,
  type AgentBookingCriteria,
  type AgentError,
  type AgentEventView,
  type AgentFormPlanView,
  type AgentInspectionView,
  type AgentProtectedDataKind,
  type AgentResearchView,
  type AgentResult,
  type AgentRuntimeView,
  type AgentSlotView,
  type AgentTaskSnapshot,
  type AgentTaskView
} from '../../../shared/agent-contracts'
import type { VoiceTaskFocus, VoiceTaskOutcome } from '../../../shared/voice-task-contracts'
import type { AgentPreferenceView, ModelDiagnosticView } from '../../../shared/model-contracts'
import {
  currentBooking,
  describeBooking,
  authenticatedRedactionCount,
  describeAuthenticated,
  describeAuthenticatedDisclosure,
  describeAuthenticatedProgress,
  describeCriteria,
  describeDisclosureCard,
  describeEvent,
  describeFormPlan,
  describeInspection,
  describeOutcome,
  describeRecipients,
  describeResearch,
  describeResearchProgress,
  describeScopeEntry,
  formatAppointmentTime,
  formatPrice,
  latestSearchResults,
  mergeEvents,
  RECIPIENT_LABELS,
  researchSources,
  topicLabel,
  type AuthenticatedControl,
  type BookingControl,
  type FormPlanControl,
  type InspectionControl,
  type ResearchControl
} from '../agent-task-view'
import './components.css'

export interface AgentTaskPanelProps {
  agent: AgentApi
  onClose: () => void
  /** A voice step asked the panel to re-read durable state and draw attention. */
  focusRequest?: { target: VoiceTaskFocus; serial: number }
  /** A trusted inspection control returned this view; the app may show its outcome in the conversation. */
  onInspectionResult?: (inspection: AgentInspectionView) => void
  /** A trusted research control finished; the app may show its outcome in the conversation. */
  onResearchResult?: (research: AgentResearchView) => void
  /** A trusted account-reading control finished; the app may show its outcome in the conversation. */
  onAuthenticatedResult?: (authenticated: AgentAuthenticatedView) => void
  /** Injectable for tests. */
  pollIntervalMs?: number
}

interface TaskState {
  generation: string
  task: AgentTaskView
  actions: AgentActionView[]
  events: AgentEventView[]
  inspection?: AgentInspectionView
  research?: AgentResearchView
  authenticated?: AgentAuthenticatedView
  formPlan?: AgentFormPlanView
}

const RUNTIME_LABELS: Record<AgentRuntimeView['state'], string> = {
  running: 'Agent runtime connected',
  starting: 'Agent runtime starting…',
  stopping: 'Agent runtime stopping…',
  stopped: 'Agent runtime stopped',
  unavailable: 'Agent runtime unavailable — restarting…',
  failed: 'Agent runtime is not running',
  not_installed: 'Agent runtime is not available in this build',
  not_configured: 'Agent runtime needs setup: add agent-runtime.json to the Lumi profile folder (see docs/PACKAGING.md)'
}

/**
 * The durable Lumi agent task (appointment booking, clinic information or
 * page inspection): explicit controls, the persisted approval preview, and
 * the task timeline.
 *
 * Nothing here changes state on mount, on polling or on reconnect: polling
 * only reads. Every mutation is a button the user pressed, and approval sends
 * only the action id and the revision that was on screen.
 */
export function AgentTaskPanel({ agent, onClose, focusRequest, onInspectionResult, onResearchResult, onAuthenticatedResult, pollIntervalMs = 2_000 }: AgentTaskPanelProps) {
  const [runtime, setRuntime] = useState<AgentRuntimeView>({ state: 'starting' })
  const [state, setState] = useState<TaskState | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [slots, setSlots] = useState<AgentSlotView[]>()
  const [busy, setBusy] = useState<string>()
  const [error, setError] = useState<string>()
  const [criteria, setCriteria] = useState<AgentBookingCriteria>({ specialty: '', day: '' })
  const [now, setNow] = useState(() => Date.now())
  const stateRef = useRef<TaskState | null>(null)
  const loadSerial = useRef(0)
  const mounted = useRef(true)
  // Synchronous guard: two clicks in one frame must not start two mutations.
  const busyRef = useRef<string | undefined>(undefined)
  const bookingRegion = useRef<HTMLDivElement>(null)
  const [focusedSerial, setFocusedSerial] = useState<number>()
  const [pendingCardFocus, setPendingCardFocus] = useState<number>()
  const [request, setRequest] = useState('')
  const [requestOutcome, setRequestOutcome] = useState<VoiceTaskOutcome>()
  const [preferences, setPreferences] = useState<AgentPreferenceView[]>([])
  const [diagnostics, setDiagnostics] = useState<ModelDiagnosticView[]>()
  const [inspectUrl, setInspectUrl] = useState('')
  const [inspectQuestion, setInspectQuestion] = useState('')
  const [objective, setObjective] = useState('')
  const [authenticatedProfiles, setAuthenticatedProfiles] = useState<AgentBrowserProfileView[]>([])
  const [authenticatedRecipients, setAuthenticatedRecipients] = useState<AgentDisclosureRecipient[]>([])
  const [authenticatedProfile, setAuthenticatedProfile] = useState('')
  const [authenticatedRecipient, setAuthenticatedRecipient] = useState('')
  const [authenticatedQuestion, setAuthenticatedQuestion] = useState('')
  // Which saved details the user chose to let a planner see (masked). Closed ids only.
  const [formRefs, setFormRefs] = useState<AgentProtectedDataKind[] | undefined>(undefined)

  stateRef.current = state

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const refresh = useCallback(async (full = false): Promise<void> => {
    const serial = ++loadSerial.current
    const previous = stateRef.current
    const after = full || !previous ? 0 : previous.task.lastEventSequence
    const result = await agent.loadActiveTask(after)
    // A newer load started meanwhile: this answer is stale.
    if (!mounted.current || serial !== loadSerial.current) return
    setLoaded(true)
    if (!result.ok) {
      if (result.error.code !== 'runtime_unavailable') setError(result.error.message)
      return
    }
    const snapshot = result.value
    if (!snapshot) {
      setState(null)
      return
    }
    const current = stateRef.current
    const sameTask = current && current.task.taskId === snapshot.task.taskId && current.generation === snapshot.runtimeGeneration
    if (!sameTask && after > 0) {
      // A different task or a new runtime generation: rebuild from durable truth.
      void refresh(true)
      return
    }
    const next: TaskState = {
      generation: snapshot.runtimeGeneration,
      task: snapshot.task,
      actions: snapshot.actions,
      events: sameTask && !full ? mergeEvents(current.events, snapshot.events) : mergeEvents([], snapshot.events),
      ...(snapshot.inspection ? { inspection: snapshot.inspection } : {}),
      ...(snapshot.research ? { research: snapshot.research } : {}),
      ...(snapshot.authenticated ? { authenticated: snapshot.authenticated } : {}),
      ...(snapshot.formPlan ? { formPlan: snapshot.formPlan } : {})
    }
    stateRef.current = next
    setState(next)
  }, [agent])

  useEffect(() => {
    let active = true
    void agent.getRuntimeStatus().then((status) => { if (active) setRuntime(status) })
    const unsubscribe = agent.onRuntimeStatus((status) => {
      setRuntime(status)
      if (status.state === 'running') void refresh(true)
    })
    return () => {
      active = false
      unsubscribe()
    }
  }, [agent, refresh])

  useEffect(() => {
    if (runtime.state !== 'running') return
    void refresh()
    const timer = setInterval(() => {
      setNow(Date.now())
      void refresh()
    }, pollIntervalMs)
    return () => clearInterval(timer)
  }, [runtime.state, refresh, pollIntervalMs])

  useEffect(() => {
    if (!focusRequest) return
    let cancelled = false
    // Voice changed durable state elsewhere: re-read it, then point at the
    // card as a region. Focus never lands on a button, so a stray key press
    // cannot approve anything.
    void refresh(true).then(() => {
      if (cancelled || !mounted.current) return
      setFocusedSerial(focusRequest.serial)
      if (focusRequest.target === 'approval_card') setPendingCardFocus(focusRequest.serial)
    })
    return () => { cancelled = true }
  }, [focusRequest, refresh])

  // Runs after the refreshed card has rendered.
  useEffect(() => {
    if (pendingCardFocus === undefined || !bookingRegion.current) return
    bookingRegion.current.focus({ preventScroll: true })
    bookingRegion.current.scrollIntoView?.({ block: 'nearest' })
    setPendingCardFocus(undefined)
  }, [pendingCardFocus, state])

  async function run<T>(label: string, work: () => Promise<AgentResult<T>>): Promise<AgentResult<T> | undefined> {
    if (busyRef.current) return undefined
    busyRef.current = label
    setBusy(label)
    setError(undefined)
    try {
      const result = await work()
      if (!result.ok) setError(errorText(result.error))
      return result
    } finally {
      busyRef.current = undefined
      if (mounted.current) {
        setBusy(undefined)
        await refresh()
      }
    }
  }

  const loadPreferences = useCallback(async (): Promise<void> => {
    const result = await agent.listPreferences()
    if (result.ok && mounted.current) setPreferences(result.value)
  }, [agent])

  useEffect(() => {
    if (runtime.state === 'running') void loadPreferences()
  }, [runtime.state, loadPreferences])

  // Read-only. The signed-in profiles and the providers main is willing to
  // offer, for the selectors on the account-reading form. Neither list is
  // ever sent back except as an opaque id chosen from it.
  useEffect(() => {
    if (runtime.state !== 'running') return
    let active = true
    void Promise.all([agent.listBrowserProfiles(), agent.getAuthenticatedOptions()]).then(([profiles, options]) => {
      if (!active || !mounted.current) return
      const signedIn = profiles.ok ? profiles.value.filter((profile) => profile.status === 'AUTHENTICATED' && !profile.activeTakeover) : []
      const recipients = options.ok ? options.value.recipients : []
      setAuthenticatedProfiles(signedIn)
      setAuthenticatedRecipients(recipients)
      setAuthenticatedProfile((current) => (signedIn.some((profile) => profile.profileId === current) ? current : signedIn[0]?.profileId ?? ''))
      setAuthenticatedRecipient((current) => ((recipients as readonly string[]).includes(current) ? current : recipients[0] ?? ''))
    })
    return () => { active = false }
  }, [runtime.state, agent, state === null])

  /**
   * A typed request. Main interprets it into the same bounded plan voice
   * uses; the result can prepare a booking but never approve one.
   */
  async function submitRequest(): Promise<void> {
    const text = request.trim()
    if (!text) return
    const requestId = `req_${crypto.randomUUID().replaceAll('-', '')}`
    const result = await run('request', () => agent.submitTextRequest(requestId, text))
    if (!result?.ok || !mounted.current) return
    setRequest('')
    setRequestOutcome(result.value)
    setSlots(undefined)
    await refresh(true)
    await loadPreferences()
    if (result.value.focus === 'approval_card') setPendingCardFocus(Date.now())
  }

  /** Shows an approval card; nothing is opened until Approve is pressed. */
  async function createInspection(): Promise<void> {
    const result = await run('inspect_prepare', () => agent.createPageInspection(inspectUrl, inspectQuestion))
    if (result?.ok && mounted.current) {
      setInspectUrl('')
      setInspectQuestion('')
      await refresh(true)
      setPendingCardFocus(Date.now())
    }
  }

  async function onInspectionControl(control: InspectionControl, inspection: AgentInspectionView): Promise<void> {
    // The revision and digest that were on screen when the user pressed.
    const { actionId, revision, proposalDigest } = inspection
    const report = (result: AgentResult<AgentInspectionView> | undefined): void => {
      if (result?.ok && mounted.current) onInspectionResult?.(result.value)
    }
    switch (control) {
      case 'approve_and_inspect':
        report(await run('inspect_execute', async () => {
          const approved = await agent.approveInspection(actionId, revision)
          if (!approved.ok) return approved
          if (approved.value.proposalDigest !== proposalDigest || approved.value.status !== 'APPROVED') {
            return { ok: false, error: { code: 'invalid_response', message: 'The approved inspection did not match what you reviewed. Nothing was opened.' } }
          }
          return agent.executeInspection(actionId, approved.value.revision)
        }))
        return
      case 'inspect_now':
        report(await run('inspect_execute', () => agent.executeInspection(actionId, revision)))
        return
      case 'reject':
        report(await run('reject', () => agent.rejectInspection(actionId, revision)))
        return
      case 'answer_from_observation':
        report(await run('inspect_answer', () => agent.answerInspection(actionId)))
        return
      case 'inspect_again':
        await run('inspect_prepare', () => agent.inspectPageAgain())
    }
  }

  /** Shows the bounded scope card; nothing is searched or opened until Allow. */
  async function createResearch(): Promise<void> {
    const result = await run('research_prepare', () => agent.createResearchTask(objective))
    if (result?.ok && mounted.current) {
      setObjective('')
      await refresh(true)
      setPendingCardFocus(Date.now())
    }
  }

  async function onResearchControl(control: ResearchControl, research: AgentResearchView): Promise<void> {
    // The grant id and revision that were on screen when the user pressed.
    const grant = research.grant
    const report = (result: AgentResult<AgentTaskSnapshot> | undefined): void => {
      if (result?.ok && mounted.current && result.value.research) onResearchResult?.(result.value.research)
    }
    switch (control) {
      case 'allow_research': {
        if (!grant) return
        // Allowing and starting are one press for the user and two calls
        // here: the scope is confirmed by id and revision first, and only a
        // confirmed scope can fund a step.
        report(await run('research_run', async () => {
          const granted = await agent.grantResearchScope(grant.grantId, grant.revision)
          if (!granted.ok) return granted
          const active = granted.value.research?.grant
          if (!active || active.status !== 'ACTIVE' || active.scopeDigest !== grant.scopeDigest) {
            return {
              ok: false,
              error: {
                code: 'invalid_response' as const,
                message: 'The permission Lumi recorded did not match what you reviewed. Nothing was searched or opened.'
              }
            }
          }
          return agent.runResearch()
        }))
        return
      }
      case 'run_research':
        report(await run('research_run', () => agent.runResearch()))
        return
      case 'decline_research':
        if (!grant) return
        report(await run('research_stop', () => agent.declineResearchScope(grant.grantId, grant.revision)))
        return
      case 'stop_research':
        report(await run('research_stop', () => agent.stopResearch()))
    }
  }

  /** Shows the trusted disclosure card; nothing is opened until Allow. */
  async function createAuthenticated(): Promise<void> {
    const result = await run('authenticated_prepare', () =>
      agent.createAuthenticatedTask(authenticatedQuestion, authenticatedProfile, authenticatedRecipient))
    if (result?.ok && mounted.current) {
      setAuthenticatedQuestion('')
      await refresh(true)
      setPendingCardFocus(Date.now())
    }
  }

  async function onAuthenticatedControl(control: AuthenticatedControl, authenticated: AgentAuthenticatedView): Promise<void> {
    // The grant id and revision that were on screen when the user pressed.
    const grant = authenticated.grant
    const report = (result: AgentResult<AgentTaskSnapshot> | undefined): void => {
      if (result?.ok && mounted.current && result.value.authenticated) onAuthenticatedResult?.(result.value.authenticated)
    }
    switch (control) {
      case 'allow_account_reading': {
        if (!grant) return
        // Allowing and starting are one press for the user and two calls
        // here: the scope is confirmed by id and revision first, and only a
        // confirmed scope can fund a step.
        report(await run('authenticated_run', async () => {
          const granted = await agent.grantAuthenticatedScope(grant.grantId, grant.revision)
          if (!granted.ok) return granted
          const active = granted.value.authenticated?.grant
          if (!active || active.status !== 'ACTIVE' || active.scopeDigest !== grant.scopeDigest) {
            return {
              ok: false,
              error: {
                code: 'invalid_response' as const,
                message: 'The permission Lumi recorded did not match what you reviewed. Nothing was opened or sent.'
              }
            }
          }
          return agent.runAuthenticated()
        }))
        return
      }
      case 'run_account_reading':
        report(await run('authenticated_run', () => agent.runAuthenticated()))
        return
      case 'decline_account_reading':
        if (!grant) return
        report(await run('authenticated_stop', () => agent.declineAuthenticatedScope(grant.grantId, grant.revision)))
        return
      case 'stop_account_reading':
        report(await run('authenticated_stop', () => agent.stopAuthenticated()))
    }
  }

  /**
   * The trusted form-planning controls. Each is a button the user pressed and each
   * carries closed ids and the revision that was on screen -- never a manifest, a
   * value, an origin, a field or a provider.
   */
  async function onFormPlanControl(control: FormPlanControl, plan: AgentFormPlanView): Promise<void> {
    const grant = plan.grant
    const disclosure = plan.disclosure
    switch (control) {
      case 'plan_form': {
        const chosen = formRefs ?? plan.savedDetails.map((item) => item.kind)
        if (chosen.length === 0) return
        await run('form_prepare', () => agent.prepareFormPlanning(chosen))
        return
      }
      case 'allow_form_planning': {
        if (!grant) return
        // Allowing and asking for a plan are one press and two calls: the grant is
        // confirmed by id and revision first, and only a confirmed grant may fund a plan.
        await run('form_plan', async () => {
          if (grant.status === 'PENDING') {
            const granted = await agent.grantFormPlanning(grant.grantId, grant.revision)
            if (!granted.ok) return granted
            const active = granted.value.formPlan?.grant
            if (!active || active.status !== 'ACTIVE' || active.grantId !== grant.grantId) {
              return {
                ok: false as const,
                error: { code: 'invalid_response' as const, message: 'The permission Lumi recorded did not match what you reviewed. Nothing was sent.' }
              }
            }
          }
          return agent.runFormPlanning()
        })
        return
      }
      case 'decline_form_planning':
        if (!grant) return
        await run('form_decline', () => agent.declineFormPlanning(grant.grantId, grant.revision))
        return
      case 'approve_disclosure':
        if (!disclosure) return
        await run('form_approve', () => agent.approveFieldDisclosure(disclosure.actionId, disclosure.revision))
        return
      case 'decline_disclosure':
        if (!disclosure) return
        await run('form_reject', () => agent.rejectFieldDisclosure(disclosure.actionId, disclosure.revision))
    }
  }

  async function lookupInfo(): Promise<void> {
    await run('lookup', () => agent.lookupClinicInfo())
  }

  async function forget(key: AgentPreferenceView['key']): Promise<void> {
    const result = await run('forget', () => agent.forgetPreference(key))
    if (result?.ok) setPreferences(result.value)
  }

  async function createTask(): Promise<void> {
    const result = await run('create', () => agent.createBookingTask(criteria))
    if (result?.ok) {
      setSlots(undefined)
      await refresh(true)
    }
  }

  async function closeTask(): Promise<void> {
    const result = await run('close', () => agent.closeActiveTask())
    if (result?.ok) {
      setSlots(undefined)
      setState(null)
    }
  }

  async function search(): Promise<void> {
    const result = await run('search', () => agent.searchAppointments())
    if (result?.ok) setSlots(result.value)
  }

  async function prepare(slotId: string): Promise<void> {
    const result = await run('prepare', () => agent.prepareBooking(slotId))
    if (result?.ok) setSlots(undefined)
  }

  async function onControl(control: BookingControl, action: AgentActionView): Promise<void> {
    // The revision and digest that were on screen when the user pressed.
    const { actionId, revision, proposalDigest } = action
    switch (control) {
      case 'approve_and_book':
        await run('approve', async () => {
          const approved = await agent.approveAction(actionId, revision)
          if (!approved.ok) return approved
          if (approved.value.proposalDigest !== proposalDigest || approved.value.status !== 'APPROVED') {
            return { ok: false, error: { code: 'invalid_response', message: 'The approved booking did not match what you reviewed. Nothing was booked.' } }
          }
          return agent.executeAction(actionId, approved.value.revision)
        })
        return
      case 'book_now':
        await run('execute', () => agent.executeAction(actionId, revision))
        return
      case 'reject':
        await run('reject', () => agent.rejectAction(actionId, revision))
        return
      case 'request_approval':
        await run('request', () => agent.requestApproval(actionId, revision))
        return
      case 'check_booking':
        await run('reconcile', () => agent.reconcileAction(actionId, revision))
        return
      case 'discard_and_review':
        await run('review', async () => {
          const rejected = await agent.rejectAction(actionId, revision)
          if (!rejected.ok) return rejected
          return agent.prepareBooking(action.booking.slotId)
        })
        return
      case 'review_updated':
        await prepare(action.booking.slotId)
        return
      case 'search_again':
        await search()
    }
  }

  const booking = state ? currentBooking(state.actions) : undefined
  const runtimeReady = runtime.state === 'running'
  const canCreate = runtimeReady && !busy
  const taskClosed = state ? TERMINAL_TASK_STATUSES.includes(state.task.status) : false
  const bookingOpen = booking !== undefined && booking.status !== 'REJECTED' && booking.status !== 'FAILED'
  // Durable results from the task timeline, unless a fresher local search is
  // on screen. Hidden while a booking is open or once the task is closed.
  const isInfoTask = state?.task.kind === 'clinic_info'
  const isInspectionTask = state?.task.kind === 'page_inspection'
  const isResearchTask = state?.task.kind === 'public_research'
  const isAuthenticatedTask = state?.task.kind === 'authenticated_read'
  const shownSlots = isInfoTask || isInspectionTask || isResearchTask || isAuthenticatedTask
    ? undefined
    : taskClosed || bookingOpen ? slots : slots ?? (state ? latestSearchResults(state.events) : undefined)
  const profiles = state && isInfoTask ? latestProfiles(state.events) : undefined

  return (
    <div className="agent-task-panel" data-testid="agent-task-panel">
      <header className="settings-header">
        <h2>Lumi agent</h2>
        <button className="icon-button" type="button" aria-label="Close agent" onClick={onClose}>&times;</button>
      </header>
      <div className="settings-scroll">
        <p className={`agent-runtime-state state-${runtime.state}`} role="status" data-testid="agent-runtime-state">
          {RUNTIME_LABELS[runtime.state]}
        </p>
        {(runtime.state === 'failed' || runtime.state === 'stopped') && (
          <button className="secondary-button" type="button" disabled={Boolean(busy)}
            onClick={() => void run('restart', async () => {
              const result = await agent.restartRuntime()
              if (result.ok) setRuntime(result.value)
              return result
            })}>
            Restart agent runtime
          </button>
        )}
        {error && <p className="notice error-notice" role="alert" data-testid="agent-error">{error}</p>}
        {busy && <p className="notice" aria-live="polite">{busyText(busy)}</p>}

        {runtimeReady && (
          <form className="agent-section" aria-label="Ask Lumi" data-testid="agent-request-form"
            onSubmit={(event) => { event.preventDefault(); void submitRequest() }}>
            <label className="agent-field">
              Ask Lumi
              <input value={request} maxLength={1_000} data-testid="agent-request-input"
                placeholder="Find a dermatologist tomorrow evening under ₹1000"
                onChange={(event) => setRequest(event.target.value)} />
            </label>
            <button className="primary-button" type="submit" disabled={!canCreate || !request.trim()}>
              Send request
            </button>
            {requestOutcome && (
              <div className="notice" role="status" data-testid="agent-request-outcome" data-narration={requestOutcome.narration.kind}>
                <p>{describeOutcome(requestOutcome)}</p>
                {requestOutcome.plan && (
                  <ol className="agent-plan" data-testid="agent-plan">
                    {requestOutcome.plan.map((step) => (
                      <li key={step.step} data-step={step.step} data-status={step.status}>
                        {step.step.replaceAll('_', ' ')} · {step.status.replaceAll('_', ' ')}
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            )}
          </form>
        )}

        {runtimeReady && loaded && !state && (
          <section className="agent-section" aria-label="Start a booking task">
            <p className="workspace-note">Lumi searches the reviewed clinic site and asks before booking anything.</p>
            <label className="agent-field">
              Specialty
              <input value={criteria.specialty} maxLength={60} placeholder="Any"
                onChange={(event) => setCriteria({ ...criteria, specialty: event.target.value })} />
            </label>
            <label className="agent-field">
              Day
              <select value={criteria.day}
                onChange={(event) => setCriteria({ ...criteria, day: event.target.value as AgentBookingCriteria['day'] })}>
                <option value="">Any day</option>
                {BOOKING_DAYS.map((day) => <option key={day} value={day}>{day}</option>)}
              </select>
            </label>
            <button className="primary-button" type="button" disabled={!canCreate} onClick={() => void createTask()}>
              Start booking task
            </button>
          </section>
        )}

        {runtimeReady && loaded && !state && (
          <form className="agent-section" aria-label="Inspect a public page" data-testid="agent-inspect-form"
            onSubmit={(event) => { event.preventDefault(); void createInspection() }}>
            <p className="workspace-note">
              Lumi can read one public web page and answer a question from what it shows. You approve the exact address first; nothing is opened before that.
            </p>
            <label className="agent-field">
              Page address
              <input value={inspectUrl} maxLength={2_048} inputMode="url" data-testid="agent-inspect-url"
                placeholder="github.com/owner/repository"
                onChange={(event) => setInspectUrl(event.target.value)} />
            </label>
            <label className="agent-field">
              Question about the page
              <input value={inspectQuestion} maxLength={500} data-testid="agent-inspect-question"
                placeholder="What is this repository for?"
                onChange={(event) => setInspectQuestion(event.target.value)} />
            </label>
            <button className="primary-button" type="submit"
              disabled={!canCreate || !inspectUrl.trim() || !inspectQuestion.trim()}>
              Prepare inspection
            </button>
          </form>
        )}

        {runtimeReady && loaded && !state && authenticatedProfiles.length > 0 && authenticatedRecipients.length > 0 && (
          <form className="agent-section" aria-label="Ask about a signed-in account" data-testid="agent-authenticated-form"
            onSubmit={(event) => { event.preventDefault(); void createAuthenticated() }}>
            <p className="workspace-note">
              Lumi can read a website you signed in to through a Lumi profile and answer a question about your account. You review exactly what it may do and which AI provider receives the text before anything is opened.
            </p>
            <label className="agent-field">
              Signed-in profile
              <select value={authenticatedProfile} data-testid="agent-authenticated-profile-select"
                onChange={(event) => setAuthenticatedProfile(event.target.value)}>
                {authenticatedProfiles.map((profile) => (
                  <option key={profile.profileId} value={profile.profileId}>{profile.label} — {profile.site}</option>
                ))}
              </select>
            </label>
            <label className="agent-field">
              Question about your account
              <input value={authenticatedQuestion} maxLength={500} data-testid="agent-authenticated-question"
                placeholder="Which of my repositories are private?"
                onChange={(event) => setAuthenticatedQuestion(event.target.value)} />
            </label>
            <label className="agent-field">
              AI provider that may receive the text
              <select value={authenticatedRecipient} data-testid="agent-authenticated-recipient-select"
                onChange={(event) => setAuthenticatedRecipient(event.target.value)}>
                {authenticatedRecipients.map((recipient) => (
                  <option key={recipient} value={recipient}>{RECIPIENT_LABELS[recipient]}</option>
                ))}
              </select>
            </label>
            <button className="primary-button" type="submit"
              disabled={!canCreate || !authenticatedQuestion.trim() || !authenticatedProfile || !authenticatedRecipient}>
              Prepare account reading
            </button>
          </form>
        )}

        {runtimeReady && loaded && !state && (
          <form className="agent-section" aria-label="Research public websites" data-testid="agent-research-form"
            onSubmit={(event) => { event.preventDefault(); void createResearch() }}>
            <p className="workspace-note">
              Lumi can search and read public web pages to answer a question, in an isolated browser that is not signed in to anything. You allow it once for the task; nothing is searched or opened before that.
            </p>
            <label className="agent-field">
              What should Lumi find out?
              <input value={objective} maxLength={500} data-testid="agent-research-objective"
                placeholder="Find the Lumi repository on GitHub and tell me what it does"
                onChange={(event) => setObjective(event.target.value)} />
            </label>
            <button className="primary-button" type="submit" disabled={!canCreate || !objective.trim()}>
              Prepare research
            </button>
          </form>
        )}

        {state && (
          <>
            <section className="agent-section"
              aria-label={isInfoTask ? 'Clinic information task' : isInspectionTask ? 'Page inspection task' : isResearchTask ? 'Public research task' : isAuthenticatedTask ? 'Account reading task' : 'Booking task'}
              data-testid="agent-task" data-task-kind={state.task.kind}>
              <p className="eyebrow">{isInfoTask ? 'CLINIC INFO' : isInspectionTask ? 'PAGE INSPECTION' : isResearchTask ? 'PUBLIC RESEARCH' : isAuthenticatedTask ? 'ACCOUNT READING' : 'TASK'} · {state.task.status.replaceAll('_', ' ')}</p>
              <p className="workspace-note" data-testid="agent-task-criteria">
                {isInfoTask && state.task.infoQuery
                  ? `${state.task.infoQuery.doctor || state.task.infoQuery.specialty} · ${topicLabel(state.task.infoQuery.topic)}`
                  : isInspectionTask && state.task.inspection
                    ? `${state.task.inspection.host} · ${state.task.inspection.question}`
                    : isResearchTask && state.task.research
                      ? state.task.research.objective
                      : isAuthenticatedTask && state.task.authenticated
                        ? state.task.authenticated.objective
                        : describeCriteria(state.task.criteria)}
              </p>
              <div className="actions">
                {isInfoTask && !taskClosed ? (
                  <button className="secondary-button" type="button" disabled={!runtimeReady || Boolean(busy)} onClick={() => void lookupInfo()}>
                    Look up again
                  </button>
                ) : null}
                {isInspectionTask && !taskClosed && !state.inspection ? (
                  <button className="secondary-button" type="button" disabled={!runtimeReady || Boolean(busy)}
                    onClick={() => void run('inspect_prepare', () => agent.inspectPageAgain())}>
                    Show approval card
                  </button>
                ) : null}
                {!isInfoTask && !isInspectionTask && !isResearchTask && !isAuthenticatedTask && !taskClosed && !bookingOpen ? (
                  <button className="secondary-button" type="button" disabled={!runtimeReady || Boolean(busy)} onClick={() => void search()}>
                    Search appointments
                  </button>
                ) : null}
                <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void closeTask()}>
                  Close task
                </button>
              </div>
            </section>

            {shownSlots && (
              <section className="agent-section" aria-label="Available appointments" data-testid="agent-slots">
                {shownSlots.length === 0 ? <p className="notice">No appointments matched.</p> : (
                  <ul className="agent-slots">
                    {shownSlots.map((slot) => (
                      <li key={slot.slotId} data-slot-id={slot.slotId}>
                        <div>
                          <strong>{slot.doctor}</strong>
                          <span>{slot.specialty}</span>
                          <span>{formatAppointmentTime(slot.time)} · {formatPrice(slot.price, slot.currency)}</span>
                        </div>
                        <button className="secondary-button" type="button" disabled={!runtimeReady || Boolean(busy)}
                          onClick={() => void prepare(slot.slotId)}>
                          Prepare booking
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}

            {profiles && <ClinicProfiles profiles={profiles} />}

            {state.inspection && (
              <div ref={bookingRegion} tabIndex={-1} className="agent-booking-region"
                data-testid="agent-inspection-region" data-voice-focus={focusedSerial}>
                <InspectionCard
                  inspection={state.inspection}
                  now={now}
                  disabled={!runtimeReady || Boolean(busy)}
                  busy={busy}
                  taskClosed={taskClosed}
                  onControl={(control) => void onInspectionControl(control, state.inspection!)}
                />
              </div>
            )}

            {state.research && (
              <div ref={bookingRegion} tabIndex={-1} className="agent-booking-region"
                data-testid="agent-research-region" data-voice-focus={focusedSerial}>
                <ResearchCard
                  research={state.research}
                  now={now}
                  disabled={!runtimeReady || Boolean(busy)}
                  busy={busy}
                  taskClosed={taskClosed}
                  onControl={(control) => void onResearchControl(control, state.research!)}
                />
              </div>
            )}

            {state.authenticated && (
              <div ref={bookingRegion} tabIndex={-1} className="agent-booking-region"
                data-testid="agent-authenticated-region" data-voice-focus={focusedSerial}>
                <AuthenticatedCard
                  authenticated={state.authenticated}
                  now={now}
                  disabled={!runtimeReady || Boolean(busy)}
                  busy={busy}
                  taskClosed={taskClosed}
                  onControl={(control) => void onAuthenticatedControl(control, state.authenticated!)}
                />
              </div>
            )}

            {state.authenticated && state.formPlan && (
              <FormPlanCard
                plan={state.formPlan}
                accountReadingActive={state.authenticated.grant?.status === 'ACTIVE'}
                now={now}
                disabled={!runtimeReady || Boolean(busy)}
                taskClosed={taskClosed}
                selected={formRefs ?? state.formPlan.savedDetails.map((item) => item.kind)}
                onSelect={setFormRefs}
                onControl={(control) => void onFormPlanControl(control, state.formPlan!)}
              />
            )}

            {booking && (
              <div ref={bookingRegion} tabIndex={-1} className="agent-booking-region"
                data-testid="agent-booking-region" data-voice-focus={focusedSerial}>
                <BookingCard
                  action={booking}
                  events={state.events}
                  now={now}
                  disabled={!runtimeReady || Boolean(busy)}
                  busy={busy}
                  taskClosed={taskClosed}
                  onControl={(control) => void onControl(control, booking)}
                />
              </div>
            )}

            <Timeline events={state.events} />

            <details className="agent-technical" data-testid="agent-technical"
              onToggle={(event) => {
                if ((event.currentTarget as HTMLDetailsElement).open) {
                  void agent.getDiagnostics().then((result) => { if (result.ok && mounted.current) setDiagnostics(result.value) })
                }
              }}>
              <summary>Technical details</summary>
              <dl>
                <dt>Task</dt><dd data-testid="agent-task-id">{state.task.taskId}</dd>
                <dt>Task revision</dt><dd>{state.task.revision}</dd>
                <dt>Last event</dt><dd>{state.task.lastEventSequence}</dd>
                <dt>Runtime generation</dt><dd>{state.generation}</dd>
                {state.actions.map((action) => (
                  <ActionDetails key={action.actionId} action={action} />
                ))}
              </dl>
              {diagnostics && diagnostics.length > 0 && (
                <ol className="agent-event-ids" data-testid="agent-diagnostics">
                  {diagnostics.slice(-20).map((line, index) => (
                    <li key={`${line.at}-${index}`}>
                      {line.kind} · {line.command ?? line.taskClass ?? ''} · {line.provider ?? ''} {line.model ?? ''} · {line.result}
                      {line.latencyMs !== undefined ? ` · ${line.latencyMs} ms` : ''}
                      {line.inputTokens !== undefined ? ` · in ${line.inputTokens}` : ''}{line.outputTokens !== undefined ? ` · out ${line.outputTokens}` : ''}
                    </li>
                  ))}
                </ol>
              )}
              <ol className="agent-event-ids">
                {state.events.map((event) => (
                  <li key={event.sequence}>#{event.sequence} {event.type} · {event.createdAt}{event.actionRevision ? ` · rev ${event.actionRevision}` : ''}</li>
                ))}
              </ol>
            </details>
          </>
        )}

        {runtimeReady && preferences.length > 0 && (
          <section className="agent-section" aria-label="Saved preferences" data-testid="agent-preferences">
            <p className="eyebrow">SAVED PREFERENCES</p>
            <p className="workspace-note">Used only to fill details a new request leaves out. What you ask for now always wins, and the clinic site decides prices and availability.</p>
            <ul className="agent-slots">
              {preferences.map((preference) => (
                <li key={preference.key} data-preference={preference.key}>
                  <div>
                    <strong>{preference.key.replaceAll('_', ' ')}</strong>
                    <span>{String(preference.value)} · said {new Date(preference.provenance.recordedAt).toLocaleDateString()}</span>
                  </div>
                  <button className="text-button" type="button" disabled={Boolean(busy)} onClick={() => void forget(preference.key)}>
                    Forget
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  )
}

function latestProfiles(events: readonly AgentEventView[]): AgentDoctorProfileView[] | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type === 'task.info_lookup_completed') return events[index].profiles
  }
  return undefined
}

/** Public clinic facts read by the reviewed adapter. Rendered as text only. */
function ClinicProfiles({ profiles }: { profiles: AgentDoctorProfileView[] }) {
  return (
    <section className="agent-section" aria-label="Clinic information" data-testid="agent-profiles">
      <p className="eyebrow">FROM THE CLINIC WEBSITE</p>
      {profiles.length === 0 ? <p className="notice">No doctor matched.</p> : (
        <ul className="agent-slots">
          {profiles.map((profile) => (
            <li key={profile.doctorId} data-doctor-id={profile.doctorId}>
              <div>
                <strong>{profile.doctor}</strong>
                <span>{profile.specialty} · {profile.clinic}</span>
                <span>{profile.address}</span>
                <span>{profile.hours} · {formatPrice(profile.consultationFee, profile.currency)}</span>
                <span>Languages: {profile.languages.join(', ')} · {profile.walkIns ? 'Walk-ins welcome' : 'By appointment only'}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function BookingCard({ action, events, now, disabled, busy, taskClosed, onControl }: {
  action: AgentActionView
  events: AgentEventView[]
  now: number
  disabled: boolean
  busy?: string
  taskClosed: boolean
  onControl: (control: BookingControl) => void
}) {
  const described = describeBooking(action, events, now)
  // A closed task accepts no new booking work; checking an unknown outcome
  // stays available because it only reads the site.
  const model = taskClosed
    ? { ...described, controls: described.controls.filter((control) => control === 'check_booking') }
    : described
  const { booking } = action
  return (
    <article className={`agent-booking-card tone-${model.tone}`} role="group" aria-label={model.title}
      data-testid="agent-booking-card" data-action-status={action.status} data-action-id={action.actionId}
      data-action-revision={action.revision}>
      <p className="lifelens-card-eyebrow">{model.eyebrow}</p>
      <h3 className="lifelens-card-heading" data-testid="agent-booking-title">{model.title}</h3>
      {model.showBookingDetails && (
        <dl className="agent-booking-details">
          <dt>Doctor</dt><dd>{booking.doctor}</dd>
          <dt>Time</dt><dd>{formatAppointmentTime(booking.time)}</dd>
          <dt>Price</dt><dd>{formatPrice(booking.price, booking.currency)}</dd>
          <dt>Site</dt><dd>{booking.site === 'appointment_fixture' ? 'Clinic appointment site (test fixture)' : booking.site}</dd>
        </dl>
      )}
      {model.changedFacts && (
        <table className="agent-changed-facts">
          <thead><tr><th scope="col" /><th scope="col">Approved</th><th scope="col">Current</th></tr></thead>
          <tbody>
            {model.changedFacts.map((fact) => (
              <tr key={fact.label}><th scope="row">{fact.label}</th><td>{fact.approved}</td><td>{fact.observed}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {model.lines.map((line) => <p key={line}>{line}</p>)}
      {model.controls.length > 0 && (
        <div className="lifelens-confirmation-actions">
          {model.controls.map((control) => (
            <button key={control} type="button" disabled={disabled}
              className={isPrimary(control) ? 'lifelens-confirm-button' : 'lifelens-dismiss-button'}
              aria-busy={busy !== undefined || undefined}
              onClick={() => onControl(control)}>
              {CONTROL_LABELS[control]}
            </button>
          ))}
        </div>
      )}
    </article>
  )
}

const INSPECTION_CONTROL_LABELS: Record<InspectionControl, string> = {
  approve_and_inspect: 'Approve and inspect',
  inspect_now: 'Inspect now',
  reject: 'Reject',
  answer_from_observation: 'Answer from saved page',
  inspect_again: 'Inspect again (new approval)'
}

/**
 * The trusted approval and result card for one page inspection. Every label
 * is Lumi's. Page-controlled text appears only as plain text in labelled
 * fields: the quoted evidence of a verified answer, the page title and the
 * final URL. It never supplies a label, a control or an instruction.
 */
function InspectionCard({ inspection, now, disabled, busy, taskClosed, onControl }: {
  inspection: AgentInspectionView
  now: number
  disabled: boolean
  busy?: string
  taskClosed: boolean
  onControl: (control: InspectionControl) => void
}) {
  const described = describeInspection(inspection, now)
  const model = taskClosed ? { ...described, controls: [] } : described
  const { proposal, observation, answer } = inspection
  return (
    <article className={`agent-booking-card tone-${model.tone}`} role="group" aria-label={model.title}
      data-testid="agent-inspection-card" data-action-status={inspection.status} data-action-id={inspection.actionId}
      data-action-revision={inspection.revision} data-answer-status={answer?.status}>
      <p className="lifelens-card-eyebrow">{model.eyebrow}</p>
      <h3 className="lifelens-card-heading" data-testid="agent-inspection-title">{model.title}</h3>
      {model.showProposal && (
        <dl className="agent-booking-details" data-testid="agent-inspection-proposal">
          <dt>Website</dt><dd data-testid="agent-inspection-host"><strong>{proposal.host}</strong></dd>
          <dt>Address</dt><dd className="agent-digest" data-testid="agent-inspection-url">{proposal.url}</dd>
          <dt>Lumi will</dt>
          <dd data-testid="agent-inspection-operation">Inspect this public page — no clicks, form submissions, uploads, downloads, or non-GET requests. Lumi opens it once in an isolated browser and reads its visible text and up to {proposal.maxLinks} links.</dd>
          <dt>Your question</dt><dd data-testid="agent-inspection-question">{proposal.question}</dd>
          <dt>Sent to answer</dt>
          <dd data-testid="agent-inspection-disclosure">
            Up to {proposal.maxTextChars.toLocaleString()} characters of the page’s text and your question go to: {describeRecipients(proposal.recipients)}.
          </dd>
        </dl>
      )}
      {answer?.status === 'answered' && (
        <div data-testid="agent-inspection-answer">
          <p><strong>{answer.answer}</strong></p>
          <p className="workspace-note">Quoted from the page:</p>
          <ul className="agent-evidence">
            {answer.evidence.map((item) => (
              <li key={`${item.block}-${item.quote}`}><q>{item.quote}</q></li>
            ))}
          </ul>
        </div>
      )}
      {model.lines.map((line) => <p key={line}>{line}</p>)}
      {observation && (
        <dl className="agent-booking-details" data-testid="agent-inspection-source">
          <dt>Source</dt><dd className="agent-digest">{observation.finalUrl}</dd>
          {observation.redirects.length > 0 && (<><dt>Redirected</dt><dd>{observation.redirects.length} time{observation.redirects.length === 1 ? '' : 's'}</dd></>)}
          <dt>Read at</dt><dd>{new Date(observation.observedAt).toLocaleString()}</dd>
          <dt>Page title</dt><dd>{observation.title || '(none)'}</dd>
          <dt>Observation</dt>
          <dd>
            {observation.blockCount} text blocks, {observation.linkCount} links
            {observation.truncated ? ' · long page, only the first part was read' : ''}
            {observation.settled ? '' : ' · the page was still changing'}
            {answer ? ` · answered by ${describeRecipients([answer.provider])}` : ''}
          </dd>
        </dl>
      )}
      {model.controls.length > 0 && (
        <div className="lifelens-confirmation-actions">
          {model.controls.map((control) => (
            <button key={control} type="button" disabled={disabled}
              className={control === 'approve_and_inspect' || control === 'inspect_now' || control === 'answer_from_observation'
                ? 'lifelens-confirm-button' : 'lifelens-dismiss-button'}
              aria-busy={busy !== undefined || undefined}
              onClick={() => onControl(control)}>
              {INSPECTION_CONTROL_LABELS[control]}
            </button>
          ))}
        </div>
      )}
    </article>
  )
}


const RESEARCH_CONTROL_LABELS: Record<ResearchControl, string> = {
  allow_research: 'Allow research',
  decline_research: 'Cancel',
  run_research: 'Start researching',
  stop_research: 'Stop'
}

/**
 * The trusted permission, progress and answer card for one public-research
 * task. Every label and every line is Lumi's own. Page-controlled text appears
 * only as plain text in labelled fields: the quoted evidence of a verified
 * answer, and each source's title and address. It never becomes a label, a
 * control, a line of instructions or a link.
 */
function ResearchCard({ research, now, disabled, busy, taskClosed, onControl }: {
  research: AgentResearchView
  now: number
  disabled: boolean
  busy?: string
  taskClosed: boolean
  onControl: (control: ResearchControl) => void
}) {
  const described = describeResearch(research, now)
  const model = taskClosed ? { ...described, controls: [] } : described
  const scope = research.grant?.scope
  const answer = research.answer
  const sources = researchSources(research)
  return (
    <article className={`agent-booking-card tone-${model.tone}`} role="group" aria-label={model.title}
      data-testid="agent-research-card" data-grant-status={research.grant?.status}
      data-grant-id={research.grant?.grantId} data-grant-revision={research.grant?.revision}
      data-answer-status={answer?.status} data-stop-reason={answer?.stopReason}>
      <p className="lifelens-card-eyebrow">PUBLIC RESEARCH</p>
      <h3 className="lifelens-card-heading" data-testid="agent-research-title">{model.title}</h3>
      <dl className="agent-booking-details" data-testid="agent-research-goal">
        <dt>Goal</dt><dd>{research.objective}</dd>
      </dl>
      {model.showScope && scope && (
        <div data-testid="agent-research-scope">
          <p className="workspace-note">Allowed for this task:</p>
          <ul className="agent-evidence" data-testid="agent-research-allowed">
            {scope.allowed.map((entry) => (
              <li key={entry} data-scope-entry={entry}>✓ {describeScopeEntry(entry, true)}</li>
            ))}
          </ul>
          <p className="workspace-note">Not allowed:</p>
          <ul className="agent-evidence" data-testid="agent-research-forbidden">
            {scope.forbidden.map((entry) => (
              <li key={entry} data-scope-entry={entry}>✗ {describeScopeEntry(entry, false)}</li>
            ))}
          </ul>
          <dl className="agent-booking-details">
            <dt>Limits</dt>
            <dd data-testid="agent-research-limits">
              At most {scope.budgets.maxSteps} steps, {scope.budgets.maxObservations} pages or searches
              and {scope.budgets.maxTabs} tabs, for up to {Math.round(scope.budgets.maxActiveSeconds / 60)} minutes.
              {research.grant?.expiresAt ? ` This permission expires at ${new Date(research.grant.expiresAt).toLocaleTimeString()}.` : ''}
            </dd>
            <dt>Sent to answer</dt>
            <dd data-testid="agent-research-disclosure">
              Up to {scope.maxTextChars.toLocaleString()} characters of the text on those pages, and your goal, go to: {describeRecipients(scope.recipients)}.
            </dd>
            {scope.seeds.length > 0 && (
              <>
                <dt>Addresses you gave</dt>
                <dd className="agent-digest">{scope.seeds.join(' · ')}</dd>
              </>
            )}
          </dl>
        </div>
      )}
      {answer && (answer.status === 'answered' || answer.status === 'partial') && (
        <div data-testid="agent-research-answer">
          <p><strong>{answer.answer}</strong></p>
          <p className="workspace-note">Quoted from the pages Lumi read:</p>
          <ul className="agent-evidence">
            {answer.evidence.map((item) => (
              <li key={`${item.observation}-${item.block}-${item.quote}`}><q>{item.quote}</q></li>
            ))}
          </ul>
        </div>
      )}
      {model.lines.map((line) => <p key={line}>{line}</p>)}
      {model.showProgress && (
        <p className="workspace-note" role="status" data-testid="agent-research-progress">
          {describeResearchProgress(research)}
        </p>
      )}
      {model.showProgress && sources.length > 0 && (
        <dl className="agent-booking-details" data-testid="agent-research-sources">
          <dt>Sources</dt>
          <dd>
            <ul className="agent-evidence">
              {sources.map((source) => (
                <li key={source.url} data-source-host={source.host}>
                  <span className="agent-digest">{source.url}</span>
                  {source.title ? ` — ${source.title}` : ''}
                </li>
              ))}
            </ul>
          </dd>
        </dl>
      )}
      {model.controls.length > 0 && (
        <div className="lifelens-confirmation-actions">
          {model.controls.map((control) => (
            <button key={control} type="button" disabled={disabled}
              className={control === 'allow_research' || control === 'run_research'
                ? 'lifelens-confirm-button' : 'lifelens-dismiss-button'}
              aria-busy={busy !== undefined || undefined}
              onClick={() => onControl(control)}>
              {RESEARCH_CONTROL_LABELS[control]}
            </button>
          ))}
        </div>
      )}
    </article>
  )
}

const FORM_PLAN_CONTROL_LABELS: Record<FormPlanControl, string> = {
  plan_form: 'Plan this form',
  allow_form_planning: 'Allow planning',
  decline_form_planning: 'Cancel',
  approve_disclosure: 'Approve this plan',
  decline_disclosure: 'Cancel'
}

/**
 * The trusted FORM PLANNING and PREPARE THIS FORM cards (Milestone 8b S5).
 *
 * Every label and line is Lumi's own, written in `agent-task-view.ts`. Website text
 * appears only as a plain-text field label or option name inside the manifest rows,
 * and a masked preview is shown exactly as the runtime sent it: this component never
 * masks anything and never holds a raw saved value. The buttons say what they do --
 * "Allow planning" and "Approve this plan" -- never "Fill", because nothing here
 * changes the page.
 */
function FormPlanCard({ plan, accountReadingActive, now, disabled, taskClosed, selected, onSelect, onControl }: {
  plan: AgentFormPlanView
  accountReadingActive: boolean
  now: number
  disabled: boolean
  taskClosed: boolean
  selected: AgentProtectedDataKind[]
  onSelect: (refs: AgentProtectedDataKind[]) => void
  onControl: (control: FormPlanControl) => void
}) {
  const model = describeFormPlan(plan, accountReadingActive, taskClosed, now)
  if (model.stage === 'hidden') return null
  const manifest = plan.disclosure && (model.stage === 'approval' || model.stage === 'prepared')
    ? describeDisclosureCard(plan.disclosure)
    : undefined
  return (
    <article className="agent-booking-card tone-info" role="group" aria-label={model.title}
      data-testid="agent-form-plan-card" data-stage={model.stage}
      data-grant-id={plan.grant?.grantId} data-grant-revision={plan.grant?.revision}
      data-action-id={plan.disclosure?.actionId} data-action-revision={plan.disclosure?.revision}>
      <p className="lifelens-card-eyebrow" data-testid="agent-form-plan-eyebrow">{model.eyebrow}</p>
      <h3 className="lifelens-card-heading" data-testid="agent-form-plan-title">{model.title}</h3>
      {model.stage === 'offer' && (
        <fieldset data-testid="agent-form-plan-offer">
          <legend className="workspace-note">Saved details</legend>
          {model.offerable.map((item) => (
            <label key={item.kind} className="agent-checkbox-row">
              <input type="checkbox" checked={selected.includes(item.kind)} disabled={disabled}
                onChange={(event) => onSelect(event.target.checked
                  ? [...selected, item.kind].filter((kind, index, all) => all.indexOf(kind) === index)
                  : selected.filter((kind) => kind !== item.kind))} />
              {' '}{item.label} <span className="workspace-note">({item.preview})</span>
            </label>
          ))}
        </fieldset>
      )}
      {model.permission && (
        <div data-testid="agent-form-plan-permission">
          <dl className="agent-booking-details">
            <dt>Site</dt><dd data-testid="agent-form-plan-site">{model.permission.site}</dd>
            <dt>Sent to</dt><dd data-testid="agent-form-plan-provider">{model.permission.provider}</dd>
          </dl>
          <p className="workspace-note">To propose which saved detail belongs in which field, Lumi will send to {model.permission.provider}:</p>
          <ul className="agent-evidence" data-testid="agent-form-plan-sent">
            {model.permission.sent.map((entry) => <li key={entry}>• {entry}</li>)}
          </ul>
          <p data-testid="agent-form-plan-not-sent"><strong>{model.permission.notSent[0]}</strong></p>
          {model.permission.countryNotice && <p className="workspace-note" data-testid="agent-form-plan-country">{model.permission.countryNotice}</p>}
          <p className="workspace-note">Saved details available:</p>
          <ul className="agent-evidence" data-testid="agent-form-plan-details">
            {model.permission.savedDetails.map((item) => <li key={item.kind}>✓ {item.label}</li>)}
          </ul>
          <p className="workspace-note" data-testid="agent-form-plan-cannot-act">{model.permission.cannotAct}</p>
        </div>
      )}
      {manifest && (
        <div data-testid="agent-form-plan-manifest">
          <dl className="agent-booking-details">
            <dt>Site</dt><dd data-testid="agent-form-plan-site">{manifest.site}</dd>
            {manifest.formLabel && (<><dt>Form</dt><dd data-testid="agent-form-plan-form">{manifest.formLabel}</dd></>)}
            <dt>Goes to</dt><dd data-testid="agent-form-plan-goes-to">{manifest.site}</dd>
          </dl>
          <p className="workspace-note">{model.stage === 'approval' ? 'Lumi plans to use:' : 'You approved:'}</p>
          <ul className="agent-evidence" data-testid="agent-form-plan-rows">
            {manifest.rows.map((row) => (
              <li key={`${row.fieldLabel}-${row.detail}`}>
                <strong>{row.savedLabel}</strong> {row.detail} → <q>{row.fieldLabel}</q>
              </li>
            ))}
          </ul>
          {manifest.countryNotice && <p className="workspace-note" data-testid="agent-form-plan-country">{manifest.countryNotice}</p>}
        </div>
      )}
      {model.lines.map((line) => <p key={line} className="workspace-note">{line}</p>)}
      {model.controls.length > 0 && (
        <div className="lifelens-confirmation-actions">
          {model.controls.map((control) => (
            <button key={control} type="button" disabled={disabled || (control === 'plan_form' && selected.length === 0)}
              className={control === 'allow_form_planning' || control === 'approve_disclosure' || control === 'plan_form'
                ? 'lifelens-confirm-button' : 'lifelens-dismiss-button'}
              data-testid={`agent-form-plan-${control}`}
              onClick={() => onControl(control)}>
              {FORM_PLAN_CONTROL_LABELS[control]}
            </button>
          ))}
        </div>
      )}
    </article>
  )
}

const AUTHENTICATED_CONTROL_LABELS: Record<AuthenticatedControl, string> = {
  allow_account_reading: 'Allow',
  decline_account_reading: 'Cancel',
  run_account_reading: 'Continue reading',
  stop_account_reading: 'Stop'
}

/**
 * The trusted disclosure, progress and answer card for one authenticated
 * account-reading task. Every label and every line is Lumi's own, written in
 * `agent-task-view.ts`. Account text appears in exactly one place, as plain
 * text in a labelled field: the quoted evidence of a verified answer -- the
 * redacted text the provider received, never the original. It never becomes a
 * label, a control, a line of instructions or a link.
 */
function AuthenticatedCard({ authenticated, now, disabled, busy, taskClosed, onControl }: {
  authenticated: AgentAuthenticatedView
  now: number
  disabled: boolean
  busy?: string
  taskClosed: boolean
  onControl: (control: AuthenticatedControl) => void
}) {
  const described = describeAuthenticated(authenticated, now)
  const model = taskClosed ? { ...described, controls: [] } : described
  const disclosure = model.showDisclosure ? describeAuthenticatedDisclosure(authenticated) : undefined
  const answer = authenticated.answer
  return (
    <article className={`agent-booking-card tone-${model.tone}`} role="group" aria-label={model.title}
      data-testid="agent-authenticated-card" data-grant-status={authenticated.grant?.status}
      data-grant-id={authenticated.grant?.grantId} data-grant-revision={authenticated.grant?.revision}
      data-answer-status={answer?.status} data-pause-reason={authenticated.pauseReason}>
      <p className="lifelens-card-eyebrow" data-testid="agent-authenticated-eyebrow">
        {disclosure ? disclosure.heading : 'YOUR ACCOUNT'}
      </p>
      <h3 className="lifelens-card-heading" data-testid="agent-authenticated-title">{model.title}</h3>
      <dl className="agent-booking-details" data-testid="agent-authenticated-goal">
        <dt>Question</dt><dd>{authenticated.objective}</dd>
        {authenticated.profile && (
          <>
            <dt>Profile</dt><dd data-testid="agent-authenticated-profile">{authenticated.profile.label}</dd>
            <dt>Site</dt><dd data-testid="agent-authenticated-site">{authenticated.profile.site}</dd>
          </>
        )}
      </dl>
      {disclosure && (
        <div data-testid="agent-authenticated-disclosure">
          <p className="workspace-note">Lumi may:</p>
          <ul className="agent-evidence" data-testid="agent-authenticated-may">
            {disclosure.mayDo.map((entry) => <li key={entry}>✓ {entry}</li>)}
          </ul>
          <p className="workspace-note">Lumi may not:</p>
          <ul className="agent-evidence" data-testid="agent-authenticated-may-not">
            {disclosure.mayNot.map((entry) => <li key={entry}>✗ {entry}</li>)}
          </ul>
          <p data-testid="agent-authenticated-side-effects"><strong>{disclosure.sideEffectNotice}</strong></p>
          <p className="workspace-note">{disclosure.sideEffectExamples}</p>
          <dl className="agent-booking-details">
            <dt>Sent to the AI</dt>
            <dd data-testid="agent-authenticated-sent">
              <ul className="agent-evidence">
                {disclosure.sent.map((entry) => <li key={entry}>{entry}</li>)}
              </ul>
            </dd>
            <dt>AI provider</dt>
            <dd data-testid="agent-authenticated-provider">{disclosure.provider}</dd>
          </dl>
          <p className="workspace-note" data-testid="agent-authenticated-failover">{disclosure.failoverNotice}</p>
          {authenticated.grant?.expiresAt === undefined && (
            <p className="workspace-note">This permission lasts for this question only.</p>
          )}
        </div>
      )}
      {answer && (answer.status === 'answered' || answer.status === 'partial') && (
        <div data-testid="agent-authenticated-answer">
          <p><strong>{answer.answer}</strong></p>
          <p className="workspace-note">Quoted from your account pages, with identifiers hidden:</p>
          <ul className="agent-evidence">
            {answer.evidence.map((item) => (
              <li key={`${item.observation}-${item.block}-${item.quote}`}><q>{item.quote}</q></li>
            ))}
          </ul>
          <p className="workspace-note" data-testid="agent-authenticated-answer-provider">
            Sent to {RECIPIENT_LABELS[answer.provider]} only.
          </p>
        </div>
      )}
      {model.lines.map((line) => <p key={line}>{line}</p>)}
      {model.showProgress && (
        <p className="workspace-note" role="status" data-testid="agent-authenticated-progress">
          {describeAuthenticatedProgress(authenticated)}
          {authenticatedRedactionCount(authenticated) > 0
            ? ` · ${authenticatedRedactionCount(authenticated)} identifier${authenticatedRedactionCount(authenticated) === 1 ? '' : 's'} hidden before sending`
            : ''}
        </p>
      )}
      {model.controls.length > 0 && (
        <div className="lifelens-confirmation-actions">
          {model.controls.map((control) => (
            <button key={control} type="button" disabled={disabled}
              className={control === 'allow_account_reading' || control === 'run_account_reading'
                ? 'lifelens-confirm-button' : 'lifelens-dismiss-button'}
              aria-busy={busy !== undefined || undefined}
              onClick={() => onControl(control)}>
              {AUTHENTICATED_CONTROL_LABELS[control]}
            </button>
          ))}
        </div>
      )}
    </article>
  )
}

function Timeline({ events }: { events: AgentEventView[] }) {
  return (
    <section className="agent-section" aria-label="Task timeline">
      <p className="eyebrow">TIMELINE</p>
      <ol className="agent-timeline" data-testid="agent-timeline">
        {events.map((event) => (
          <li key={event.sequence} data-sequence={event.sequence} data-event-type={event.type}>
            <span>{describeEvent(event)}</span>
            <time dateTime={event.createdAt}>{new Date(event.createdAt).toLocaleTimeString()}</time>
          </li>
        ))}
      </ol>
    </section>
  )
}

function ActionDetails({ action }: { action: AgentActionView }) {
  return (
    <>
      <dt>Action</dt><dd data-testid="agent-action-id">{action.actionId}</dd>
      <dt>Action status / revision</dt><dd>{action.status} / {action.revision}</dd>
      <dt>Proposal digest</dt><dd className="agent-digest">{action.proposalDigest}</dd>
      {action.approval && (
        <>
          <dt>Approval</dt>
          <dd>{action.approval.approvalId} · {action.approval.status} · bound to rev {action.approval.actionRevision} · expires {action.approval.expiresAt}</dd>
        </>
      )}
      {action.attempts.map((attempt) => (
        <Fragment key={attempt.attemptId}>
          <dt>Attempt {attempt.attemptNumber}</dt>
          <dd data-testid="agent-attempt">
            {attempt.attemptId} · {attempt.outcome ?? 'in progress'} · started {attempt.startedAt}
            {attempt.finishedAt ? ` · finished ${attempt.finishedAt}` : ''}
            {attempt.errorCode ? ` · ${attempt.errorCode}` : ''} · runtime {attempt.runtimeGeneration}
          </dd>
        </Fragment>
      ))}
    </>
  )
}

const CONTROL_LABELS: Record<BookingControl, string> = {
  approve_and_book: 'Approve and book',
  book_now: 'Book now',
  reject: 'Reject',
  request_approval: 'Ask for approval',
  check_booking: 'Check existing booking',
  discard_and_review: 'Discard and review again',
  review_updated: 'Review updated details',
  search_again: 'Search again'
}

function isPrimary(control: BookingControl): boolean {
  return control === 'approve_and_book' || control === 'book_now' || control === 'check_booking' || control === 'request_approval'
}

function busyText(label: string): string {
  switch (label) {
    case 'search': return 'Searching the clinic site (read-only)…'
    case 'prepare':
    case 'review': return 'Reading the current appointment details…'
    case 'approve':
    case 'execute': return 'Booking — waiting for the clinic site to confirm…'
    case 'reconcile': return 'Checking the existing booking — not booking again…'
    case 'request': return 'Understanding your request…'
    case 'lookup': return 'Reading the clinic site (read-only)…'
    case 'inspect_prepare': return 'Preparing the approval card — nothing is opened yet…'
    case 'inspect_execute': return 'Reading the approved page once, then answering from it…'
    case 'inspect_answer': return 'Answering from the saved page — not opening it again…'
    case 'research_prepare': return 'Preparing the research permission — nothing is searched yet…'
    case 'research_run': return 'Searching and reading public pages…'
    case 'research_stop': return 'Stopping — nothing else will be opened…'
    case 'authenticated_prepare': return 'Preparing the permission card — nothing is opened yet…'
    case 'authenticated_run': return 'Reading your account pages…'
    case 'authenticated_stop': return 'Stopping — nothing else will be opened…'
    default: return 'Working…'
  }
}

function errorText(error: AgentError): string {
  return error.message
}
