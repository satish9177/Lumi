/**
 * Shown while a drag is over the panel.
 *
 * It states plainly that dropping adds the file and sends nothing, because the
 * whole trust model rests on the drop itself being inert.
 */
export interface DropOverlayProps {
  /** How many files the drag carries. Lumi accepts exactly one. */
  fileCount: number
}

export function DropOverlay({ fileCount }: DropOverlayProps) {
  const accepted = fileCount === 1

  return (
    <div className={`drop-overlay ${accepted ? 'is-accepted' : 'is-rejected'}`} role="presentation">
      <div className="drop-overlay-inner">
        <p className="drop-overlay-title">
          {accepted ? 'Drop to add this file to Lumi.' : 'Add one file at a time.'}
        </p>
        {accepted && <p className="drop-overlay-note">Nothing will be sent.</p>}
      </div>
    </div>
  )
}
