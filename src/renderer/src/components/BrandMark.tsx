/**
 * The Lumi orb, inline so it stays crisp at any display scale.
 *
 * Mirrors build/icon-master.svg. Decorative by default: the header pairs it
 * with a visible "Lumi" wordmark, so a second announcement would be noise.
 */
export function BrandMark({ size = 20, glow = false }: { size?: number; glow?: boolean }) {
  // Gradient ids must be unique per instance or a second mark reuses the first.
  const id = `lumi-mark-${size}${glow ? '-glow' : ''}`

  return (
    <svg
      className={glow ? 'brand-mark brand-mark-glow' : 'brand-mark'}
      width={size}
      height={size}
      viewBox="0 0 1024 1024"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={`${id}-orb`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#79efff" />
          <stop offset="0.5" stopColor="#8f9bff" />
          <stop offset="1" stopColor="#dd75ff" />
        </linearGradient>
        <radialGradient id={`${id}-spark`} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.85" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="512" cy="512" r="440" fill={`url(#${id}-orb)`} />
      <circle cx="336" cy="292" r="132" fill={`url(#${id}-spark)`} />
      <circle cx="512" cy="512" r="437" fill="none" stroke="#2a2f6e" strokeOpacity="0.4" strokeWidth="6" />
    </svg>
  )
}
