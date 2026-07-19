const ELECTRON_INVOKE_PREFIX = /^Error invoking remote method '[^'\r\n]+':\s*(?:Error:\s*)?/
const FALLBACK_MESSAGE = 'LifeLens encountered an unexpected error.'

/**
 * Removes Electron's known ipcRenderer.invoke wrapper without rewriting the
 * safe application message carried inside it. Unknown messages retain their
 * meaningful content; stack-frame lines are never user/model-visible.
 */
export function messageFrom(error: unknown): string {
  const raw = error instanceof Error
    ? error.message
    : typeof error === 'string'
      ? error
      : FALLBACK_MESSAGE
  return withoutStackFrames(raw.replace(ELECTRON_INVOKE_PREFIX, ''))
}

function withoutStackFrames(message: string): string {
  const lines = message.split(/\r?\n/)
  const firstStackFrame = lines.findIndex((line) => /^\s*at\s+/.test(line))
  return firstStackFrame === -1 ? message : lines.slice(0, firstStackFrame).join('\n')
}
