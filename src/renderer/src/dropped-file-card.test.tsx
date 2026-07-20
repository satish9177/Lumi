import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { DroppedFileDescriptor } from '../../shared/contracts'
import { DropOverlay, DroppedFileCard } from './components'

const PHOTO: DroppedFileDescriptor = {
  droppedId: 'dropped-1',
  fileName: 'whiteboard.png',
  fileTypeLabel: 'PNG image',
  sizeBytes: 348_160,
  mediaKind: 'photo',
  expiresAt: '2026-07-20T13:00:00.000Z',
  thumbnailDataUrl: 'data:image/jpeg;base64,AAAA'
}

const DOCUMENT: DroppedFileDescriptor = {
  droppedId: 'dropped-2',
  fileName: 'contract.pdf',
  fileTypeLabel: 'PDF document',
  sizeBytes: 1_048_576,
  mediaKind: 'document',
  expiresAt: '2026-07-20T13:00:00.000Z'
}

function render(file: DroppedFileDescriptor): string {
  return renderToStaticMarkup(
    <DroppedFileCard
      file={file}
      onOpen={vi.fn()}
      onAnalyse={vi.fn()}
      onSend={vi.fn()}
      onRemove={vi.fn()}
    />
  )
}

describe('dropped-file card', () => {
  it('says plainly that nothing has happened yet', () => {
    const markup = render(DOCUMENT)

    expect(markup).toContain('Added locally. Nothing happens until you choose an action.')
    expect(markup).toContain('Stays on this device')
  })

  it('announces the file and its inaction to a screen reader', () => {
    const markup = render(DOCUMENT)

    expect(markup).toContain('aria-label="File added: contract.pdf, PDF document, 1.0 MB. No action taken."')
  })

  it('offers Analyse for an image', () => {
    expect(render(PHOTO)).toContain('>Analyse<')
  })

  it('hides Analyse for a document, which Lumi cannot read', () => {
    const markup = render(DOCUMENT)

    expect(markup).not.toContain('>Analyse<')
    // Open and Send remain, because those do not require reading the contents.
    expect(markup).toContain('>Open<')
    expect(markup).toContain('>Send<')
  })

  it('shows the main-rendered thumbnail for an image', () => {
    expect(render(PHOTO)).toContain('data:image/jpeg;base64,AAAA')
  })

  it('shows an app-authored glyph for a document instead of its contents', () => {
    const markup = render(DOCUMENT)

    expect(markup).toContain('dropped-file-glyph')
    expect(markup).toContain('PDF')
    expect(markup).not.toContain('<img')
  })

  it('never renders a filesystem path', () => {
    const markup = render(PHOTO) + render(DOCUMENT)

    expect(markup).not.toMatch(/[A-Za-z]:\\/)
    expect(markup).not.toContain('/Users/')
    expect(markup).not.toContain('dropped-1')
  })

  it('shows a human-readable size', () => {
    expect(render(DOCUMENT)).toContain('1.0 MB')
    expect(render(PHOTO)).toContain('340.0 KB')
  })

  it('disables every action while one is in flight', () => {
    const markup = renderToStaticMarkup(
      <DroppedFileCard file={PHOTO} busy onOpen={vi.fn()} onAnalyse={vi.fn()} onSend={vi.fn()} onRemove={vi.fn()} />
    )

    expect(markup.match(/disabled/g)?.length).toBe(4)
  })
})

describe('drop overlay', () => {
  it('promises that dropping sends nothing', () => {
    const markup = renderToStaticMarkup(<DropOverlay fileCount={1} />)

    expect(markup).toContain('Drop to add this file to Lumi.')
    expect(markup).toContain('Nothing will be sent.')
  })

  it('refuses a multi-file drag in plain words', () => {
    const markup = renderToStaticMarkup(<DropOverlay fileCount={3} />)

    expect(markup).toContain('Add one file at a time.')
    expect(markup).toContain('is-rejected')
  })
})
