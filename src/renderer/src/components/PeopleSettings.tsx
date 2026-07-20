import { useId, useState } from 'react'
import type { PeopleEnrolmentView, PeopleFaceCandidateView, PeopleProfileView, PeopleSearchStatus } from '../../../shared/contracts'
import { COPY, formatFileSize } from '../copy'
import './components.css'

export interface PeopleSettingsProps {
  status: PeopleSearchStatus
  /** The active enrolment or addition draft, once main has opened one. */
  enrolment?: PeopleEnrolmentView
  /** True while an enrolment mutation is in flight, to disable double-submits. */
  enrolmentBusy?: boolean
  busy?: boolean
  onEnable: () => void
  onPause: () => void
  onResume: () => void
  onDeleteAll: () => void
  onBeginEnrolment: (label: string) => void
  onBeginAddition: (profileId: string) => void
  onSelectFace: (candidateId: string) => void
  onConfirmEnrolment: () => void
  onCancelEnrolment: () => void
  onRenameProfile: (profileId: string, label: string) => void
  onRescanProfile: (profileId: string) => void
  onDeleteProfile: (profileId: string) => void
}

/**
 * The People section of the intelligent-photo-search settings card.
 *
 * Photo selection for a reference is not handled here: the app's existing
 * approved-folder search is the only photo browser Lumi has, so choosing a
 * reference photo means running a search and using the "Use as reference"
 * action added to each result card while a draft is open (see
 * PhotoResultGrid and LifeLensApp's enrolment wiring). This component owns
 * the label step, the face-selection step, and the summary — the parts that
 * do not need a photo browser.
 *
 * "Add person" does not open a main-process draft by itself. It only reveals
 * a local label field; nothing crosses the IPC boundary, and no draft exists
 * anywhere, until that field is submitted. That is what makes "dropping or
 * selecting a file must not automatically enrol it" true from the very first
 * click, not just once photos are involved.
 */
export function PeopleSettings({
  status,
  enrolment,
  enrolmentBusy = false,
  busy = false,
  onEnable,
  onPause,
  onResume,
  onDeleteAll,
  onBeginEnrolment,
  onBeginAddition,
  onSelectFace,
  onConfirmEnrolment,
  onCancelEnrolment,
  onRenameProfile,
  onRescanProfile,
  onDeleteProfile
}: PeopleSettingsProps) {
  const [addingLabel, setAddingLabel] = useState(false)
  const [labelDraft, setLabelDraft] = useState('')
  const [renamingId, setRenamingId] = useState<string | undefined>(undefined)
  const [renameValue, setRenameValue] = useState('')
  const headingId = useId()

  const showLabelStep = addingLabel && enrolment === undefined
  const showEnrolmentView = enrolment !== undefined

  const cancelLocalLabelStep = (): void => {
    setAddingLabel(false)
    setLabelDraft('')
  }

  return (
    <section className="people-settings" aria-labelledby={headingId}>
      <div className="section-heading-row">
        <div>
          <p className="eyebrow">LOCAL PEOPLE MATCHING</p>
          <h2 id={headingId}>People</h2>
        </div>
        <span className={`people-state state-${status.state}`}>{COPY.people.stateLabel[status.state]}</span>
      </div>

      <p className="workspace-note">{COPY.people.privacy}</p>

      {!status.enabled && (
        <>
          <p className="workspace-note">{COPY.people.off}</p>
          <div className="actions">
            <button className="secondary-button" type="button" disabled={busy} onClick={onEnable}>
              {COPY.people.enable}
            </button>
          </div>
        </>
      )}

      {status.enabled && (
        <>
          {status.state === 'model_required' && (
            <p className="workspace-note">
              {COPY.people.modelDownload(formatFileSize(status.modelDownloadBytes))}
            </p>
          )}
          {status.state === 'downloading' && (
            <div className="people-progress">
              <progress
                max={Math.max(1, status.modelDownloadBytes)}
                value={status.downloadedBytes}
                aria-label="Download progress"
                aria-valuenow={status.downloadedBytes}
                aria-valuemin={0}
                aria-valuemax={Math.max(1, status.modelDownloadBytes)}
                aria-valuetext={`${formatFileSize(status.downloadedBytes)} of ${formatFileSize(status.modelDownloadBytes)}`}
              />
              <span>{formatFileSize(status.downloadedBytes)} of {formatFileSize(status.modelDownloadBytes)}</span>
            </div>
          )}
          {status.state === 'profile_store_unavailable' && (
            <p className="workspace-note" role="alert">{COPY.people.storeUnavailable}</p>
          )}
          {status.message && <p className="workspace-note" aria-live="polite">{status.message}</p>}

          <div className="actions">
            <button
              className="secondary-button"
              type="button"
              disabled={busy || showLabelStep || showEnrolmentView}
              onClick={() => setAddingLabel(true)}
            >
              {COPY.people.addPerson}
            </button>
            {status.paused
              ? <button className="secondary-button" type="button" disabled={busy} onClick={onResume}>{COPY.people.resumeScan}</button>
              : <button className="secondary-button" type="button" disabled={busy} onClick={onPause}>{COPY.people.pauseScan}</button>}
          </div>

          {status.profiles.length === 0
            ? <p className="workspace-note">{COPY.people.noProfiles}</p>
            : (
              <ul className="people-profile-list">
                {status.profiles.map((profile) => (
                  <ProfileRow
                    key={profile.id}
                    profile={profile}
                    total={status.total}
                    busy={busy}
                    enrolmentActive={showLabelStep || showEnrolmentView}
                    renaming={renamingId === profile.id}
                    renameValue={renameValue}
                    onStartRename={() => {
                      setRenamingId(profile.id)
                      setRenameValue(profile.label)
                    }}
                    onRenameValueChange={setRenameValue}
                    onSubmitRename={() => {
                      if (renameValue.trim().length > 0) onRenameProfile(profile.id, renameValue.trim())
                      setRenamingId(undefined)
                    }}
                    onCancelRename={() => setRenamingId(undefined)}
                    onAddReference={() => onBeginAddition(profile.id)}
                    onRescan={() => {
                      if (window.confirm(COPY.people.confirmRescan(profile.label))) onRescanProfile(profile.id)
                    }}
                    onDelete={() => {
                      if (window.confirm(COPY.people.confirmDeleteProfile(profile.label))) onDeleteProfile(profile.id)
                    }}
                  />
                ))}
              </ul>
            )}

          <div className="actions">
            <button
              className="text-button danger-button"
              type="button"
              disabled={busy}
              onClick={() => {
                if (window.confirm(COPY.people.confirmDeleteAll)) onDeleteAll()
              }}
            >
              {COPY.people.deleteAll}
            </button>
          </div>
        </>
      )}

      {showLabelStep && (
        <LabelStep
          label={labelDraft}
          setLabel={setLabelDraft}
          busy={busy}
          onContinue={(value) => {
            onBeginEnrolment(value)
            cancelLocalLabelStep()
          }}
          onCancel={cancelLocalLabelStep}
        />
      )}

      {showEnrolmentView && enrolment && (
        <EnrolmentView
          enrolment={enrolment}
          busy={enrolmentBusy}
          onSelectFace={onSelectFace}
          onConfirm={onConfirmEnrolment}
          onCancel={onCancelEnrolment}
        />
      )}
    </section>
  )
}

interface ProfileRowProps {
  profile: PeopleProfileView
  total: number
  busy: boolean
  enrolmentActive: boolean
  renaming: boolean
  renameValue: string
  onStartRename: () => void
  onRenameValueChange: (value: string) => void
  onSubmitRename: () => void
  onCancelRename: () => void
  onAddReference: () => void
  onRescan: () => void
  onDelete: () => void
}

function ProfileRow({
  profile,
  total,
  busy,
  enrolmentActive,
  renaming,
  renameValue,
  onStartRename,
  onRenameValueChange,
  onSubmitRename,
  onCancelRename,
  onAddReference,
  onRescan,
  onDelete
}: ProfileRowProps) {
  return (
    <li className="people-profile-card" role="group" aria-label={profile.label}>
      {renaming
        ? (
          <form
            className="people-rename-form"
            onSubmit={(event) => {
              event.preventDefault()
              onSubmitRename()
            }}
          >
            <label className="visually-hidden" htmlFor={`rename-${profile.id}`}>{COPY.people.rename(profile.label)}</label>
            <input
              id={`rename-${profile.id}`}
              value={renameValue}
              maxLength={40}
              onChange={(event) => onRenameValueChange(event.target.value)}
              autoFocus
            />
            <button className="text-button" type="submit">Save</button>
            <button className="text-button" type="button" onClick={onCancelRename}>Cancel</button>
          </form>
        )
        : <p className="people-profile-name">{profile.label}</p>}

      <p className="people-profile-meta">{COPY.people.referenceCount(profile.referenceCount)}</p>
      {profile.status !== 'ready' && (
        <p className="people-profile-meta people-profile-warning">{COPY.people.profileStatusLabel[profile.status]}</p>
      )}
      <p className="people-profile-meta">{COPY.people.coverage(profile.checked, total)}</p>
      <p className="people-profile-meta">{COPY.people.matched(profile.matched)}</p>

      <div className="people-profile-actions">
        <button
          className="text-button"
          type="button"
          disabled={busy || enrolmentActive}
          onClick={onAddReference}
          aria-label={COPY.people.addReference(profile.label)}
        >
          Add reference
        </button>
        <button
          className="text-button"
          type="button"
          disabled={busy || renaming}
          onClick={onStartRename}
          aria-label={COPY.people.rename(profile.label)}
        >
          Rename
        </button>
        <button className="text-button" type="button" disabled={busy} onClick={onRescan} aria-label={COPY.people.rescan(profile.label)}>
          Rescan
        </button>
        <button
          className="text-button danger-button"
          type="button"
          disabled={busy}
          onClick={onDelete}
          aria-label={COPY.people.deleteProfile(profile.label)}
        >
          Delete
        </button>
      </div>
    </li>
  )
}

function LabelStep({
  label,
  setLabel,
  busy,
  onContinue,
  onCancel
}: {
  label: string
  setLabel: (value: string) => void
  busy: boolean
  onContinue: (label: string) => void
  onCancel: () => void
}) {
  const headingId = useId()
  return (
    <div className="people-enrolment-view" role="group" aria-labelledby={headingId}>
      <h3 id={headingId}>{COPY.people.enrolStep1Title}</h3>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (label.trim().length > 0) onContinue(label.trim())
        }}
      >
        <label htmlFor="people-enrol-label">{COPY.people.enrolStep1Title}</label>
        <input
          id="people-enrol-label"
          value={label}
          maxLength={40}
          placeholder={COPY.people.enrolLabelPlaceholder}
          onChange={(event) => setLabel(event.target.value)}
          autoFocus
        />
        <div className="actions">
          <button className="secondary-button" type="submit" disabled={busy || label.trim().length === 0}>
            {COPY.people.enrolStart}
          </button>
          <button className="text-button" type="button" onClick={onCancel}>{COPY.people.enrolCancel}</button>
        </div>
      </form>
    </div>
  )
}

interface EnrolmentViewProps {
  enrolment: PeopleEnrolmentView
  busy: boolean
  onSelectFace: (candidateId: string) => void
  onConfirm: () => void
  onCancel: () => void
}

function EnrolmentView({ enrolment, busy, onSelectFace, onConfirm, onCancel }: EnrolmentViewProps) {
  const headingId = useId()
  return (
    <div className="people-enrolment-view" role="group" aria-labelledby={headingId}>
      <h3 id={headingId}>{COPY.people.enrolStep2Title(enrolment.label)}</h3>
      <p className="workspace-note">{COPY.people.enrolStep2Hint}</p>
      <p className="workspace-note">{COPY.people.droppingDoesNotEnrol}</p>

      {enrolment.lastRejection && (
        <p className="workspace-note people-rejection" role="alert">{enrolment.lastRejection}</p>
      )}

      {enrolment.candidates && enrolment.candidates.length > 0 && (
        <CandidateSelection candidates={enrolment.candidates} busy={busy} onSelectFace={onSelectFace} />
      )}

      <p className="workspace-note" aria-live="polite">
        {COPY.people.enrolSummary(enrolment.acceptedReferences)}{' '}
        {enrolment.readyToCreate
          ? COPY.people.enrolReadyToCreate
          : COPY.people.enrolNeedsMore(Math.max(0, enrolment.requiredReferences - enrolment.acceptedReferences))}
      </p>

      <div className="actions">
        <button className="secondary-button" type="button" disabled={busy || !enrolment.readyToCreate} onClick={onConfirm}>
          {busy ? COPY.people.enrolCreating : COPY.people.enrolCreate}
        </button>
        <button className="text-button" type="button" disabled={busy} onClick={onCancel}>{COPY.people.enrolCancel}</button>
      </div>
    </div>
  )
}

function CandidateSelection({
  candidates,
  busy,
  onSelectFace
}: {
  candidates: PeopleFaceCandidateView[]
  busy: boolean
  onSelectFace: (candidateId: string) => void
}) {
  const headingId = useId()
  return (
    <div role="group" aria-labelledby={headingId}>
      <p id={headingId} className="workspace-note">{COPY.people.enrolCandidatesTitle}</p>
      <ul className="people-candidate-grid">
        {candidates.map((candidate, index) => (
          <li key={candidate.candidateId}>
            <button
              type="button"
              className="people-candidate-button"
              disabled={busy || !candidate.selectable}
              onClick={() => onSelectFace(candidate.candidateId)}
              aria-label={
                candidate.selectable
                  ? COPY.people.enrolSelectFace(index + 1)
                  : `${COPY.people.enrolSelectFace(index + 1)}: ${COPY.people.enrolNotSelectable(candidate.note ?? '')}`
              }
            >
              <img src={candidate.previewDataUrl} alt="" aria-hidden="true" />
              {/* Never color-only: an unusable face says so in text, not only
                  in a border colour. */}
              <span className="people-candidate-caption">
                {candidate.selectable ? `Face ${index + 1}` : COPY.people.enrolNotSelectable(candidate.note ?? 'not usable')}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <p className="workspace-note">{COPY.people.previewLocalOnly}</p>
    </div>
  )
}
