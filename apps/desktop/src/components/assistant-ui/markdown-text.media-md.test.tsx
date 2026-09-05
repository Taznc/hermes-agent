import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { isMarkdownDocumentPath, mediaMarkdownHref } from '@/lib/media'

import { MarkdownTextContent } from './markdown-text'

// Regression for #84951: a `.md` delivered via MEDIA has no entry in
// MEDIA_BY_EXT, so it classified as a generic 'file' and rendered as a
// download-style link. Markdown is renderable content — it must route to the
// preview rail (which renders .md with a rendered/source toggle) instead.
describe('markdown documents delivered via MEDIA', () => {
  afterEach(cleanup)

  it('classifies markdown extensions as markdown documents', () => {
    expect(isMarkdownDocumentPath('/tmp/report.md')).toBe(true)
    expect(isMarkdownDocumentPath('/tmp/notes.markdown')).toBe(true)
    expect(isMarkdownDocumentPath('C:\\Users\\a\\report.MD')).toBe(true)
    expect(isMarkdownDocumentPath('/tmp/report.md?x=1')).toBe(true)
    expect(isMarkdownDocumentPath('/tmp/archive.zip')).toBe(false)
    expect(isMarkdownDocumentPath('/tmp/clip.mp4')).toBe(false)
    expect(isMarkdownDocumentPath('/tmp/README')).toBe(false)
  })

  it('renders a MEDIA .md as a preview attachment, not a download link', async () => {
    const href = mediaMarkdownHref('/home/user/out/report.md')

    render(<MarkdownTextContent isRunning={false} text={`[report.md](${href})`} />)

    // PreviewAttachment renders an "open preview" toggle button; the old
    // MediaAttachment 'file' fallback rendered a bare "Open ..." anchor.
    // Two buttons now: Download + Open preview (maintainer-requested).
    const buttons = await screen.findAllByRole('button')
    expect(buttons.length).toBe(2)
    expect(screen.getByText('Download')).toBeTruthy()
    expect(screen.queryByText(/^Loading /)).toBeNull()
    expect(screen.getByText('report.md')).toBeTruthy()
  })

  it('renders a non-markdown MEDIA file as a preview attachment too', async () => {
    // Extends #84951 to every non-media extension: PDFs, archives, data
    // files. MediaAttachment's kind==='file' branch was a degraded dead-end
    // (bare "Open ..." anchor, verified live with .pdf and .qzx7 — the
    // markdown-LINK path already gave these a proper file card). MEDIA:
    // must never render worse than a plain markdown link to the same file.
    const href = mediaMarkdownHref('/home/user/out/archive.zip')

    render(<MarkdownTextContent isRunning={false} text={`[archive.zip](${href})`} />)

    const buttons = await screen.findAllByRole('button')
    expect(buttons.length).toBe(2)
    expect(screen.getByText('Download')).toBeTruthy()
    expect(screen.getByText('archive.zip')).toBeTruthy()
    expect(screen.queryByText(/^Open archive/)).toBeNull()
  })

  it('renders a MEDIA pdf as a preview attachment', async () => {
    const href = mediaMarkdownHref('C:/Users/a/report.pdf')

    render(<MarkdownTextContent isRunning={false} text={`[report.pdf](${href})`} />)

    const buttons = await screen.findAllByRole('button')
    expect(buttons.length).toBe(2)
    expect(screen.getByText('report.pdf')).toBeTruthy()
  })

  // Regression for the <div>-in-<p> hydration error: a MEDIA: link inside a
  // paragraph is a child of MarkdownParagraph's real <p> (MarkdownLink is the
  // registered `a` renderer), so the PreviewAttachment card it resolves to
  // must not emit a block-level <div> there — the browser's parser would
  // close the <p> early, desyncing React's tree from the DOM (React logs "In
  // HTML, <div> cannot be a descendant of <p>. This will cause a hydration
  // error.").
  it('does not nest a <div> inside <p> for a MEDIA: link paragraph', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const href = mediaMarkdownHref('/home/user/out/report.md')

    const { container } = render(
      <MarkdownTextContent isRunning={false} text={`Here is the file: [report.md](${href})`} />
    )

    await screen.findByText('report.md')

    expect(container.querySelector('p div')).toBeNull()

    for (const call of errorSpy.mock.calls) {
      expect(String(call[0])).not.toMatch(/cannot be a descendant of/i)
    }

    errorSpy.mockRestore()
  })
})
