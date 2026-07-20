import type { CompanionState, PhotoSearchStatus, RealtimeMode } from '../../shared/contracts'

/**
 * Collapses every competing signal — companion state, connection, Telegram,
 * search, and photo indexing — into the single pill the header shows.
 *
 * Pure so the precedence can be tested directly. The order below is the
 * contract; background work must never mask a live state.
 */

export type StatusTone =
  | 'error'
  | 'offline'
  | 'busy'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'indexing'
  | 'idle'

export interface StatusDescriptor {
  readonly tone: StatusTone
  /** Always present: state is never conveyed by the dot colour alone. */
  readonly label: string
  /** Demo mode rides along as a suffix rather than a paragraph in the body. */
  readonly suffix?: string
}

export interface StatusInputs {
  readonly companionState: CompanionState
  readonly isConnecting: boolean
  readonly hasConnectedBefore: boolean
  readonly online: boolean
  readonly isSending: boolean
  readonly isSearching: boolean
  readonly photoSearchStatus?: Pick<PhotoSearchStatus, 'state' | 'indexed' | 'total'>
  readonly mode?: RealtimeMode
}

function indexingLabel(status: Pick<PhotoSearchStatus, 'indexed' | 'total'>): string {
  if (status.total > 0) {
    return `Indexing photos ${status.indexed.toLocaleString()} of ${status.total.toLocaleString()}`
  }
  return 'Indexing photos'
}

export function deriveStatus(inputs: StatusInputs): StatusDescriptor {
  const suffix = inputs.mode === 'mock' ? 'Demo mode' : undefined
  const describe = (tone: StatusTone, label: string): StatusDescriptor => ({ tone, label, suffix })

  // Needs attention outranks everything: it is the only state the user must act on.
  if (inputs.companionState === 'error') {
    return describe('error', 'Needs attention')
  }
  if (!inputs.online) {
    return describe('offline', 'Offline')
  }
  if (inputs.isSending) {
    return describe('busy', 'Sending')
  }
  if (inputs.isSearching) {
    return describe('busy', 'Searching')
  }
  if (inputs.isConnecting) {
    return describe('busy', inputs.hasConnectedBefore ? 'Reconnecting' : 'Connecting')
  }
  if (inputs.companionState === 'thinking') {
    return describe('thinking', 'Thinking')
  }
  if (inputs.companionState === 'listening') {
    return describe('listening', 'Listening')
  }
  if (inputs.companionState === 'speaking') {
    return describe('speaking', 'Speaking')
  }

  // Only once Lumi is otherwise idle does background indexing get the pill.
  const photo = inputs.photoSearchStatus
  if (photo && (photo.state === 'indexing' || photo.state === 'downloading' || photo.state === 'verifying')) {
    return describe(
      'indexing',
      photo.state === 'indexing' ? indexingLabel(photo) : photo.state === 'downloading' ? 'Getting the photo model' : 'Checking the download'
    )
  }

  if (inputs.companionState === 'success') {
    return describe('idle', 'Done')
  }
  return describe('idle', 'Ready')
}
