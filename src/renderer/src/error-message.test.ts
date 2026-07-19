import { describe, expect, it } from 'vitest'
import { messageFrom } from './error-message'

const APPROVE_PREFIX = "Error invoking remote method 'lifelens:approve-pending-action': Error: "

describe('renderer IPC error normalization', () => {
  it('unwraps the uncertain notice exactly without exposing the IPC channel', () => {
    const notice = 'I can’t confirm whether this reached Telegram. Check the chat before trying again.'
    const normalized = messageFrom(new Error(`${APPROVE_PREFIX}${notice}`))

    expect(normalized).toBe(notice)
    expect(normalized).not.toContain('lifelens:approve-pending-action')
    expect(normalized).not.toContain('remote method')
  })

  it.each([
    'That approval expired. Ask LifeLens to propose the action again.',
    'That approval was already handled and cannot be used again.',
    'That approval is invalid or no longer available.',
    'That file changed since you reviewed it. Nothing was sent. Please confirm it again.',
    'Telegram is not connected. Connect your personal account first.',
    'Telegram upload timed out before it started. Nothing was sent.'
  ])('preserves a wrapped safe application message exactly: %s', (message) => {
    expect(messageFrom(new Error(`${APPROVE_PREFIX}${message}`))).toBe(message)
  })

  it('retains meaningful unknown content while removing stack frames', () => {
    const normalized = messageFrom(new Error('A meaningful unexpected transport failure.\n    at invoke (electron/js2c/renderer_init:2:1)'))
    expect(normalized).toBe('A meaningful unexpected transport failure.')
    expect(normalized).not.toContain('renderer_init')
  })

  it('unwraps an unknown Electron channel message without echoing the channel name', () => {
    const normalized = messageFrom(new Error("Error invoking remote method 'lifelens:future-safe-channel': Error: Keep this meaningful detail."))
    expect(normalized).toBe('Keep this meaningful detail.')
    expect(normalized).not.toContain('future-safe-channel')
  })
})
