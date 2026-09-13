import { describe, expect, it } from 'vitest'

import { preprocessMarkdown } from './markdown-preprocess'

// A `::name{...}` paragraph is a transcript directive (transcript-directives.ts):
// the paragraph renderer needs its text byte-intact to parse it. The prose
// rewrites — bare-path linkify above all — must not touch those lines, or an
// absolute `file="/abs/path.html"` becomes a nested markdown link, the
// paragraph stops being text-only, and the directive renders as prose instead
// of the live preview frame (the original ::preview regression).
describe('preprocessMarkdown transcript-directive shielding', () => {
  it('leaves a ::preview directive with an absolute file path untouched', () => {
    const directive = '::preview{file="/home/user/sketches/demo.html"}'

    expect(preprocessMarkdown(directive)).toBe(directive)
  })

  it('shields the directive line while still linkifying surrounding prose', () => {
    const out = preprocessMarkdown(
      'Wrote /tmp/report.md for you.\n\n::preview{file="/tmp/widget.html"}\n\nSee /tmp/notes.md too.'
    )

    expect(out).toContain('::preview{file="/tmp/widget.html"}')
    // The neighbours still get the normal bare-path treatment.
    expect(out).toContain('[/tmp/report.md](#path/')
    expect(out).toContain('[/tmp/notes.md](#path/')
  })

  it('still linkifies a bare path in ordinary prose', () => {
    expect(preprocessMarkdown('see /tmp/demo.html')).toContain('[/tmp/demo.html](#path/')
  })

  it('leaves prose mentioning :: mid-sentence alone', () => {
    const out = preprocessMarkdown('the C++ scope operator :: and /tmp/a.md')

    expect(out).toContain('[/tmp/a.md](#path/')
  })
})
