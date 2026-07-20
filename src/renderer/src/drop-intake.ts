/**
 * The renderer half of the drop gesture.
 *
 * Kept out of the component so the rules can be tested directly: how many files
 * a drag carries, whether it is accepted, and — most importantly — that a stray
 * drop can never navigate the window away from the app.
 *
 * Nothing here ever sees a filesystem path. Resolving a `File` to a path happens
 * in preload and the result goes straight to main.
 */

export type DropDecision =
  | { kind: 'accept'; file: File }
  | { kind: 'too-many' }
  | { kind: 'none' }

/** Counts the files a drag carries, during dragover as well as on drop. */
export function countDraggedFiles(transfer: Pick<DataTransfer, 'items' | 'files'> | null | undefined): number {
  if (!transfer) {
    return 0
  }
  // `items` is populated during dragover, when `files` is usually still empty.
  if (transfer.items && transfer.items.length > 0) {
    return Array.from(transfer.items).filter((item) => item.kind === 'file').length
  }
  return transfer.files?.length ?? 0
}

/** Lumi takes exactly one file; anything else is refused before main is asked. */
export function decideDrop(transfer: Pick<DataTransfer, 'items' | 'files'> | null | undefined): DropDecision {
  const count = countDraggedFiles(transfer)
  if (count === 0) {
    return { kind: 'none' }
  }
  if (count > 1) {
    return { kind: 'too-many' }
  }
  const file = transfer?.files?.[0]
  return file ? { kind: 'accept', file } : { kind: 'none' }
}

export const TOO_MANY_FILES_MESSAGE = 'One file at a time, please. Drop a single file and Lumi will pick it up.'

/**
 * Stops Chromium's default behaviour of navigating to a dropped file.
 *
 * Registered at the document level and unconditionally, so a drop landing
 * outside the overlay cannot replace the app with a file:// page.
 */
export function preventFileNavigation(target: Pick<Document, 'addEventListener' | 'removeEventListener'>): () => void {
  const swallow = (event: Event) => event.preventDefault()
  target.addEventListener('dragover', swallow)
  target.addEventListener('drop', swallow)
  return () => {
    target.removeEventListener('dragover', swallow)
    target.removeEventListener('drop', swallow)
  }
}
