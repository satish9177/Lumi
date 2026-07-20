import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const readSource = (...parts: string[]): string => readFileSync(join(process.cwd(), ...parts), 'utf8')

const app = readSource('src', 'renderer', 'src', 'LifeLensApp.tsx')
const preload = readSource('src', 'preload', 'index.ts')
const main = readSource('src', 'main', 'index.ts')
const realtime = readSource('src', 'renderer', 'src', 'realtime.ts')

describe('GPT-5.6 screen-review wiring', () => {
  it('requires the visible review confirmation before passing only a capture id over the typed bridge', () => {
    expect(app).toContain('setScreenReasoningConfirmationOpen(true)')
    expect(app).toContain('GPT-5.6 screen review confirmation')
    expect(app).toContain('Review this capture with GPT-5.6')
    expect(app).toContain('window.lifeLens.analyzeCapture(capture.id)')
    expect(preload).toContain('ipcRenderer.invoke(IPC_CHANNELS.analyzeCapture, captureId)')
  })

  it('resolves the capture from main-process memory, clears it, and displays the validated brief', () => {
    expect(main).toContain("ipcMain.handle(IPC_CHANNELS.analyzeCapture")
    expect(main).toContain('if (!isCaptureId(captureId))')
    expect(main).toContain('const capture = retainedCapture.get(captureId)')
    expect(main).toContain("createScreenReasoningSummary({ id: captureId, dataUrl: capture.dataUrl }, app.getPath('userData'))")
    expect(main).toContain("ipcMain.handle(IPC_CHANNELS.discardCapture")
    expect(preload).toContain('ipcRenderer.invoke(IPC_CHANNELS.discardCapture)')
    expect(app).toContain('void window.lifeLens.discardCapture()')
    expect(app).toContain('if (result.sourceCaptureId !== capture.id)')
    expect(app).toContain('setExplanation(explanationFromScreenReasoning(result))')
    expect(app).toContain('clientRef.current?.provideScreenReview(result)')
    expect(app).toContain("reasoning.dates.map((value) => ({ kind: 'date' as const")
    expect(app).toContain("reasoning.links.map((value) => ({ kind: 'link' as const")
    expect(app).toContain("reasoning.nextActions.map((value) => ({ kind: 'next_action' as const")
    expect(app).toContain('screenReasoning.risks.map')
  })

  it('never includes a raw screen image in the Realtime screen-context path', () => {
    const screenContext = realtime.slice(realtime.indexOf('async provideScreenContext'), realtime.indexOf('declineScreenContext'))

    expect(screenContext).not.toContain('input_image')
    expect(screenContext).not.toContain('dataUrl')
    expect(realtime).toContain('This is validated text only; no screenshot is attached or available.')
  })
})
