import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MarkdownTextContent } from './markdown-text'

// Regression for the <div>-in-<p> hydration warning: MarkdownLink is the `a`
// renderer, so an inline markdown link that resolves to PreviewAttachment /
// MediaAttachment renders as a CHILD of MarkdownParagraph's real <p>. Those
// attachment cards must therefore emit inline-safe markup (no <div>), or the
// browser's HTML parser closes the <p> early and desyncs React's tree from
// the DOM — the exact "In HTML, <div> cannot be a descendant of <p>" warning.
function expectNoParagraphNestingWarning(errorSpy: ReturnType<typeof vi.spyOn>) {
  for (const call of errorSpy.mock.calls) {
    expect(String(call[0])).not.toMatch(/cannot be a descendant of/i)
  }
}

// Regression for #82140: a plain filesystem href in assistant markdown
// (`[report](/home/user/report.md)`) rendered as a bare dead anchor —
// file:// is blocked in the renderer, and on a remote gateway the path
// isn't on this disk at all. Such links must route through the preview
// pipeline (PreviewAttachment → normalizeOrLocalPreviewTarget), which
// resolves the path at VIEW time against the session's backend: local
// connections read the file directly, remote connections fetch it over the
// authenticated /api/fs bridge. Media-extension paths keep their inline
// player instead.
describe('MarkdownLink filesystem hrefs', () => {
  afterEach(cleanup)

  it('routes an absolute file path link through the preview attachment', async () => {
    render(<MarkdownTextContent isRunning={false} text="Wrote it: [report](/home/user/report.md)" />)

    // PreviewAttachment paints the filename + an Open preview button —
    // that's the view-time door, not a dead <a>.
    await screen.findByText('report.md')
    expect(screen.getByRole('button', { name: 'Open preview' })).toBeTruthy()
    expect(document.querySelector('a[href="/home/user/report.md"]')).toBeNull()
  })

  it('routes file:// and ~/ links the same way', async () => {
    render(
      <MarkdownTextContent isRunning={false} text={'See [notes](file:///srv/data/notes.txt) and [todo](~/todo.md)'} />
    )

    await screen.findByText('notes.txt')
    await screen.findByText('todo.md')
    expect(screen.getAllByRole('button', { name: 'Open preview' })).toHaveLength(2)
  })

  it('renders a media player for a media-extension path link', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text="[clip](/tmp/demo.mp4)" />)

    await waitFor(() => expect(container.querySelector('video')).not.toBeNull())
    expect(container.querySelector('a[href="/tmp/demo.mp4"]')).toBeNull()
  })

  it('leaves anchors and relative links out of the preview pipeline', () => {
    render(
      <MarkdownTextContent
        isRunning={false}
        text={'[frag](#section-2) and [rel](docs/guide.md) and [site](https://example.com)'}
      />
    )

    // Fragment anchors survive untouched; relative links are NOT rewritten
    // (they keep Streamdown's pre-existing handling) — neither gains a
    // preview affordance.
    expect(screen.queryByRole('button', { name: 'Open preview' })).toBeNull()
    expect(document.querySelector('a[href="#section-2"]')).not.toBeNull()
  })

  it('does not nest a <div> attachment card inside the paragraph <p>', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(
      <MarkdownTextContent isRunning={false} text="Wrote it: [report](/home/user/report.md)" />
    )

    await screen.findByText('report.md')

    // The attachment card renders as a sibling flow inside the <p>; assert
    // structurally (not just "no warning") that no block <div> landed there.
    expect(container.querySelector('p div')).toBeNull()
    expectNoParagraphNestingWarning(errorSpy)
    errorSpy.mockRestore()
  })

  it('keeps a mixed text+attachment paragraph valid when text surrounds the link', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(
      <MarkdownTextContent
        isRunning={false}
        text="See the report here: [report](/home/user/report.md) for details."
      />
    )

    await screen.findByText('report.md')

    const paragraph = container.querySelector('p')
    expect(paragraph).not.toBeNull()
    expect(paragraph?.textContent).toContain('See the report here:')
    expect(paragraph?.textContent).toContain('for details.')
    expect(container.querySelector('p div')).toBeNull()
    expectNoParagraphNestingWarning(errorSpy)
    errorSpy.mockRestore()
  })
})
