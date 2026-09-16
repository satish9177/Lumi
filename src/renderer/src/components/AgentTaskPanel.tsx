import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import {
  BOOKING_DAYS,
  TERMINAL_TASK_STATUSES,
  type AgentActionView,
  type AgentApi,
  type AgentBookingCriteria,
  type AgentError,
  type AgentEventView,
  type AgentResult,
  type AgentRuntimeView,
  type AgentSlotView,
  type AgentTaskView
} from '../../../shared/agent-contracts'
import type { VoiceTaskFocus } from '../../../shared/voice-task-contracts'
import {
  currentBooking,
  describeBooking,
  describeCriteria,
  describeEvent,
  formatAppointmentTime,
  formatPrice,
  latestSearchResults,
  mergeEvents,
  type BookingControl
} from '../agent-task-view'
import './components.css'

export interface AgentTaskPanelProps {
  agent: AgentApi
  onClose: () => void
  /** A voice step asked the panel to re-read durable state and draw attention. */
  focusRequest?: { target: VoiceTaskFocus; serial: number }
  /** Injectable for tests. */
  pollIntervalMs?: number
}

interface TaskState {
  generation: string
  task: AgentTaskView
  actions: AgentActionView[]
  events: AgentEventView[]
}

const RUNTIME_LABELS: Record<AgentRuntimeView['state'], string> = {
  running: 'Agent runtime connected',
  starting: 'Agent runtime starting…',
  stopping: 'Agent runtime stopping…',
  stopped: 'Agent runtime stopped',
  unavailable: 'Agent runtime unavailable — restarting…',
  failed: 'Agent runtime is not running',
  not_installed: 'Agent runtime is not available in this build'
}

/**
 * Durable appointment-booking task: explicit create/search/prepare controls,
 * the persisted approval preview, and the task timeline.
 *
 * Nothing here changes state on mount, on polling or on reconnect: polling
 * only reads. Every mutation is a button the user pressed, and approval sends
 * only the action id and the revision that was on screen.
 */
export function AgentTaskPanel({ agent, onClose, focusRequest, pollIntervalMs = 2_000 }: AgentTaskPanelProps) {
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
      events: sameTask && !full ? mergeEvents(current.events, snapshot.events) : mergeEvents([], snapshot.events)
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
  const shownSlots = taskClosed || bookingOpen ? slots : slots ?? (state ? latestSearchResults(state.events) : undefined)

  return (
    <div className="agent-task-panel" data-testid="agent-task-panel">
      <header className="settings-header">
        <h2>Appointment booking</h2>
        <button className="icon-button" type="button" aria-label="Close appointment booking" onClick={onClose}>&times;</button>
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

        {state && (
          <>
            <section className="agent-section" aria-label="Booking task">
              <p className="eyebrow">TASK · {state.task.status.replaceAll('_', ' ')}</p>
              <p className="workspace-note" data-testid="agent-task-criteria">
                {describeCriteria(state.task.criteria)}
              </p>
              <div className="actions">
                {!taskClosed && !bookingOpen ? (
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

            <details className="agent-technical" data-testid="agent-technical">
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
              <ol className="agent-event-ids">
                {state.events.map((event) => (
                  <li key={event.sequence}>#{event.sequence} {event.type} · {event.createdAt}{event.actionRevision ? ` · rev ${event.actionRevision}` : ''}</li>
                ))}
              </ol>
            </details>
          </>
        )}
      </div>
    </div>
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
    default: return 'Working…'
  }
}

function errorText(error: AgentError): string {
  return error.message
}
