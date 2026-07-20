import type { DroppedFileDescriptor } from '../../../shared/contracts'
import { COPY, formatFileSize } from '../copy'

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
  const size = formatFileSize(file.sizeBytes)

  return (
    <section
      className="dropped-file-card"
      aria-label={COPY.drop.announce(file.fileName, file.fileTypeLabel, size)}
      aria-busy={busy || undefined}
    >
      <div className="dropped-file-preview">
        {file.thumbnailDataUrl
          ? <img src={file.thumbnailDataUrl} alt={`Preview of ${file.fileName}`} />
          : <span className="dropped-file-glyph" aria-hidden="true">{glyphFor(file.fileTypeLabel)}</span>}
      </div>

      <div className="dropped-file-body">
        <p className="dropped-file-name" title={file.fileName}>{file.fileName}</p>
        <p className="dropped-file-meta">
          {file.fileTypeLabel} · {size} · {COPY.drop.localOnly}
        </p>
        <p className="dropped-file-note">{COPY.drop.addedNote}</p>

        {/* Every action names the file, so a screen reader user hears which
            file a button acts on without relying on surrounding context. */}
        <div className="dropped-file-actions">
          <button className="text-button" type="button" onClick={onOpen} disabled={busy} aria-label={COPY.labels.openDroppedFile(file.fileName)}>
            Open
          </button>
          {isPhoto && (
            <button className="text-button" type="button" onClick={onAnalyse} disabled={busy} aria-label={COPY.labels.analyseDroppedFile(file.fileName)}>
              Analyse
            </button>
          )}
          <button className="text-button" type="button" onClick={onSend} disabled={busy} aria-label={COPY.labels.sendDroppedFile(file.fileName)}>
            Send
          </button>
          <button className="text-button danger-button" type="button" onClick={onRemove} disabled={busy} aria-label={COPY.labels.removeDroppedFile(file.fileName)}>
            Remove
          </button>
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

