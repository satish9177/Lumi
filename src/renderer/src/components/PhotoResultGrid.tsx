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
}

/**
 * A local-only preview grid. Thumbnails are rendered from data URLs built in
 * the main process and are never sent anywhere; only a photo the user
 * explicitly selects for analysis ever leaves the machine.
 */
export function PhotoResultGrid({ results, thumbnails, fallback, onOpen, onAnalyze }: PhotoResultGridProps) {
  return (
    <ul className="lifelens-photo-grid">
      {results.map((result) => {
        const thumbnail = thumbnails.get(result.id)
        return (
          <li className="lifelens-photo-card" key={result.id}>
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
            <p className="lifelens-photo-match">{fallback ? 'Possible recent match' : 'Strong match'}</p>
            <div className="lifelens-photo-actions">
              <button className="text-button" type="button" onClick={() => onOpen(result)}>Open</button>
              <button className="text-button" type="button" onClick={() => onAnalyze(result)}>Ask Lumi about this photo</button>
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
