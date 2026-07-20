/**
 * Keyboard containment for the settings slide-over.
 *
 * Settings covers the conversation rather than replacing it, so without a trap
 * Tab would walk into controls the user cannot see. Extracted from the
 * component so the selector and the wrap-around arithmetic can be tested.
 */

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[tabindex]:not([tabindex="-1"])'
].join(', ')

/**
 * Visible, focusable elements inside `container`, in tab order.
 *
 * Visibility is judged by `offsetParent`, which is null for anything
 * `display: none` or `hidden`, so a collapsed section cannot be tabbed into.
 * Deliberately duck-typed rather than checked against `HTMLElement`: this runs
 * in the renderer but must stay callable from a plain Node test.
 */
export function focusableWithin(container: {
  querySelectorAll: (selector: string) => ArrayLike<Element>
}): HTMLElement[] {
  return (Array.from(container.querySelectorAll(FOCUSABLE)) as HTMLElement[]).filter((element) => {
    // `summary` is focusable but reports no offsetParent in some engines.
    if (element.tagName === 'SUMMARY') {
      return true
    }
    return element.offsetParent !== null || element.offsetParent === undefined
  })
}

/**
 * Decides where Tab should land, or returns undefined to let the browser handle
 * it. Wraps from last to first and, with Shift, from first to last.
 */
export function nextTrappedFocus(
  elements: readonly HTMLElement[],
  active: HTMLElement | null,
  shiftKey: boolean
): HTMLElement | undefined {
  if (elements.length === 0) {
    return undefined
  }
  const first = elements[0]!
  const last = elements[elements.length - 1]!

  // Focus outside the layer is pulled back in.
  if (!active || !elements.includes(active)) {
    return shiftKey ? last : first
  }
  if (shiftKey && active === first) {
    return last
  }
  if (!shiftKey && active === last) {
    return first
  }
  return undefined
}
