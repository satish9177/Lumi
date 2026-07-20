/**
 * The six-dot grip that tells the user the header can be dragged.
 *
 * Windows ignores the CSS `cursor` inside a native drag region, so the glyph
 * itself is the whole affordance — there is no cursor change to fall back on.
 */
export function DragGrip() {
  return (
    <span className="drag-grip" title="Drag to move Lumi" aria-hidden="true">
      <svg width="10" height="16" viewBox="0 0 10 16" focusable="false">
        {[4, 8, 12].map((y) =>
          [2, 8].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.2" fill="currentColor" />)
        )}
      </svg>
    </span>
  )
}
