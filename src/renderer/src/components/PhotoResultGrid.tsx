import type { DocumentSearchResult, ResultThumbnail } from '../../../shared/contracts'
import { fileKindLabel } from '../../../shared/search-query'
import './components.css'

export interface PhotoResultGridProps {
  results: DocumentSearchResult[]
  thumbnails: Map<string, ResultThumbnail>
  /** True when these are recent possibilities rather than filename matches. */
  fallback: boolean
  onOpen: (result: DocumentSearchResult) => void
  onAnalyze: (result: DocumentSearchResult) => void
  onSend: (result: DocumentSearchResult) => void
  /**
   * Present only while a people-enrolment draft is open. Adds one more action
   * per card so choosing a reference photo reuses the same trusted-result id
   * every other action here already uses — there is no separate photo picker.
   * The label names whose profile the reference is for, so the action is
   * unambiguous even with two enrolments never open at once but reused across
   * sessions.
   */
  onUseAsReference?: (result: DocumentSearchResult) => void
  /** The person the "Use as reference" action is currently collecting for. */
  referenceForLabel?: string
}

/**
 * A local-only preview grid. Thumbnails are rendered from data URLs built in
 * the main process and are never sent anywhere. Original bytes leave the
 * machine only after the separate analysis or Telegram confirmation flow.
 */
export function PhotoResultGrid({
  results,
  thumbnails,
  fallback,
  onOpen,
  onAnalyze,
  onSend,
  onUseAsReference,
  referenceForLabel
}: PhotoResultGridProps) {
  return (
    <ul className="lifelens-photo-grid">
      {results.map((result) => {
        const thumbnail = thumbnails.get(result.id)
        return (
          // Each card is a named group, so a screen reader user hears which
          // photo the following controls act on.
          <li className="lifelens-photo-card" key={result.id} role="group" aria-label={result.name}>
            <div className="lifelens-photo-frame">
              {thumbnail?.status === 'ok' && thumbnail.dataUrl
                ? <img src={thumbnail.dataUrl} alt={`Preview of ${result.name}`} loading="lazy" />
                : <span className="lifelens-photo-placeholder">{placeholderLabel(thumbnail)}</span>}
            </div>
            <p className="lifelens-photo-name" title={result.name}>{result.name}</p>
            <p className="lifelens-photo-meta">
              {fileKindLabel(result.kind)} · {new Date(result.modifiedAt).toLocaleDateString()}
            </p>
            {folderLabel(result.relativePath) && (
              <p className="lifelens-photo-meta">in {folderLabel(result.relativePath)}</p>
            )}
            <p className="lifelens-photo-match">{result.reason ?? (fallback ? 'Possible recent match' : 'Filename match')}</p>
            <div className="lifelens-photo-actions">
              <button className="text-button" type="button" onClick={() => onOpen(result)} aria-label={`Open ${result.name}`}>Open</button>
              <button className="text-button" type="button" onClick={() => onAnalyze(result)} aria-label={`Ask Lumi about ${result.name}`}>Ask Lumi about this photo</button>
              <button className="text-button" type="button" onClick={() => onSend(result)} aria-label={`Send ${result.name} on Telegram`}>Send via Telegram</button>
              {onUseAsReference && referenceForLabel && (
                <button
                  className="text-button"
                  type="button"
                  onClick={() => onUseAsReference(result)}
                  aria-label={`Use ${result.name} as a reference photo for ${referenceForLabel}`}
                >
                  Use as reference for {referenceForLabel}
                </button>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

function placeholderLabel(thumbnail: ResultThumbnail | undefined): string {
  switch (thumbnail?.status) {
    case 'unsupported':
      return 'No preview'
    case 'unavailable':
      return 'Unavailable'
    case 'too_large':
      return 'Too large to preview'
    default:
      return 'Loading…'
  }
}

/** The safe relative folder, never an absolute path. */
function folderLabel(relativePath: string): string {
  const segments = relativePath.split('/')
  segments.pop()
  return segments.join('/')
}
