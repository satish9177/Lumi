import type { DroppedFileDescriptor } from '../../../shared/contracts'

/**
 * The temporary file the user handed Lumi.
 *
 * Every control here proposes an action that still has to be confirmed. The
 * card itself does nothing on appearing — that is the point, and the copy says
 * so.
 */
export interface DroppedFileCardProps {
  file: DroppedFileDescriptor
  onOpen: () => void
  onAnalyse: () => void
  onSend: () => void
  onRemove: () => void
  busy?: boolean
}

export function DroppedFileCard({ file, onOpen, onAnalyse, onSend, onRemove, busy = false }: DroppedFileCardProps) {
  const isPhoto = file.mediaKind === 'photo'

  return (
    <section className="dropped-file-card" aria-label={`Attached file ${file.fileName}. No action has been taken.`}>
      <div className="dropped-file-preview">
        {file.thumbnailDataUrl
          ? <img src={file.thumbnailDataUrl} alt={`Preview of ${file.fileName}`} />
          : <span className="dropped-file-glyph" aria-hidden="true">{glyphFor(file.fileTypeLabel)}</span>}
      </div>

      <div className="dropped-file-body">
        <p className="dropped-file-name" title={file.fileName}>{file.fileName}</p>
        <p className="dropped-file-meta">
          {file.fileTypeLabel} · {formatFileSize(file.sizeBytes)} · Stays on this device
        </p>
        <p className="dropped-file-note">Added locally. Nothing happens until you choose an action.</p>

        <div className="dropped-file-actions">
          <button className="text-button" type="button" onClick={onOpen} disabled={busy}>Open</button>
          {isPhoto && <button className="text-button" type="button" onClick={onAnalyse} disabled={busy}>Analyse</button>}
          <button className="text-button" type="button" onClick={onSend} disabled={busy}>Send</button>
          <button className="text-button danger-button" type="button" onClick={onRemove} disabled={busy}>Remove</button>
        </div>
      </div>
    </section>
  )
}

/** An app-authored glyph. Document contents are never read to produce one. */
function glyphFor(fileTypeLabel: string): string {
  if (fileTypeLabel.includes('PDF')) return 'PDF'
  if (fileTypeLabel.includes('Word')) return 'DOC'
  if (fileTypeLabel.includes('Text')) return 'TXT'
  return 'FILE'
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
