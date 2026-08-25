import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// Static-analysis guard: no <button> or <Button> element in the desktop renderer
// may use the native HTML `title=` attribute. Native tooltips are unstyled,
// delayed (~500ms OS default), and visually inconsistent with the themed `Tip`.
// When a tip is warranted (see DESIGN.md — not every icon, never menu triggers),
// use `<Tip label={...}>` instead of `title=`.
//
// This is a source-text scan, not a behavior test — it's the same category as
// an ESLint rule, expressed as a vitest so it runs with the rest of the suite.

// Files inside the scan scope whose title= violations are owned by an open
// upstream PR (#94882) and must not be double-fixed here. Remove each entry
// when that PR lands — the guard then covers the file again.
const PENDING_UPSTREAM_FIX = new Set(['chat/zoomable-image.tsx'])

// Recursively walk a directory and collect all .tsx file paths.
function collectTsxFiles(dir: string): string[] {
  const results: string[] = []

  for (const entry of readdirSync(dir)) {
    // Skip node_modules, dist, and __tests__ (this file itself)
    if (entry === 'node_modules' || entry === 'dist' || entry === '__tests__') {
      continue
    }

    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)

    if (stat.isDirectory()) {
      results.push(...collectTsxFiles(fullPath))
    } else if (entry.endsWith('.tsx')) {
      results.push(fullPath)
    }
  }

  return results
}

// Find every <Button ...> / <button ...> opening tag (may span multiple lines)
// carrying a native title= attribute. Returns `{ tagName, line }` per hit.
//
// A plain `[^>]*?` attribute window is defeated by any inline expression prop
// containing a `>`: the `=>` of an arrow handler (`onClick={() => ...}`) or a
// comparison (`if (a > b)`) terminates the match early, hiding a title= that
// follows it (false negative). So instead of one regex, the attribute window
// is walked with a tiny brace scanner: every `>` inside a `{...}` expression
// container (or a depth-0 quoted attribute string) is ignored, and the tag
// ends at the first `>` at brace depth 0 outside quotes. Only depth-0 text is
// kept when testing for `title=`, so `foo.title = x` inside a handler body
// can't false-positive. Quotes are deliberately NOT tracked inside braces —
// apostrophes in code comments (`// don't ...`) would desync a full string
// scanner, and unbalanced braces inside expression strings are far rarer than
// apostrophes in comments. Still a source-text scan (no JSX parser); good
// enough for a lint-style guard.
export function findNativeTitleButtons(content: string): { line: number; tagName: string }[] {
  const hits: { line: number; tagName: string }[] = []
  const openPattern = /<(Button|button)\b/gu
  let match: RegExpExecArray | null

  while ((match = openPattern.exec(content)) !== null) {
    let depth = 0
    let quote: null | string = null
    // Depth-0 attribute text only; nested expressions collapse to a space so
    // `\btitle=` can only match a real attribute of THIS tag.
    let topLevelAttrs = ''
    let closed = false
    let i = openPattern.lastIndex

    for (; i < content.length; i++) {
      const ch = content[i]

      if (depth > 0) {
        // Inside a {...} expression: only balance braces; ignore everything
        // else (strings, comments, nested JSX, arrows, comparisons).
        if (ch === '{') {
          depth++
        } else if (ch === '}') {
          depth--

          if (depth === 0) {
            topLevelAttrs += ' '
          }
        }
      } else if (quote !== null) {
        // Inside a depth-0 JSX attribute string ("..." / '...'): no escapes.
        if (ch === quote) {
          quote = null
        }
      } else if (ch === '"' || ch === "'") {
        quote = ch
      } else if (ch === '{') {
        depth = 1
      } else if (ch === '>') {
        closed = true

        break
      } else {
        topLevelAttrs += ch
      }
    }

    if (closed && /\btitle=/.test(topLevelAttrs)) {
      hits.push({
        line: content.slice(0, match.index).split('\n').length,
        tagName: match[1]
      })
    }
  }

  return hits
}

describe('no native title= on button elements', () => {
  // Regression fixtures for the matcher itself: an inline arrow-function prop
  // BEFORE the title attribute used to truncate the attribute window at the
  // `>` of `=>`, so the title= after it was never seen (false negative).
  it('catches title= even when an arrow-function prop precedes it', () => {
    const singleLine = `<button onClick={() => setOpen(true)} title={label} type="button">x</button>`

    expect(findNativeTitleButtons(singleLine)).toEqual([{ line: 1, tagName: 'button' }])

    const multiLine = [
      '<Button',
      '  onClick={event => {',
      '    if (a > b) return',
      '    doThing()',
      '  }}',
      '  title="Do the thing"',
      '>',
      '  x',
      '</Button>'
    ].join('\n')

    expect(findNativeTitleButtons(multiLine)).toEqual([{ line: 1, tagName: 'Button' }])

    // Apostrophes in comments inside a handler body must not desync the scan.
    const commented = [
      '<button',
      '  onClick={() => {',
      "    // don't collapse the clamp",
      '    toggle()',
      '  }}',
      '  title={dynamic ? a : undefined}',
      '  type="button"',
      '>',
      '  x',
      '</button>'
    ].join('\n')

    expect(findNativeTitleButtons(commented)).toEqual([{ line: 1, tagName: 'button' }])
  })

  it('does not flag buttons without title=, or title= on other elements', () => {
    expect(findNativeTitleButtons(`<button onClick={() => setOpen(true)} type="button">x</button>`)).toEqual([])
    expect(findNativeTitleButtons(`<span title="host">x</span>`)).toEqual([])
    expect(findNativeTitleButtons(`<button aria-label="Close" type="button">x</button>`)).toEqual([])
  })

  // Scan every .tsx file under src/components for <button or <Button opening
  // tags that also carry a title= attribute (anywhere in the opening tag,
  // which may span multiple lines).
  it('uses <Tip> instead of native title= on all button elements', () => {
    const violations: string[] = []
    const srcDir = resolve(__dirname, '../..')

    for (const filePath of collectTsxFiles(srcDir)) {
      const relativePath = filePath.replace(srcDir + '/', '')

      if (PENDING_UPSTREAM_FIX.has(relativePath)) {
        continue
      }

      const content = readFileSync(filePath, 'utf-8')

      for (const { line, tagName } of findNativeTitleButtons(content)) {
        violations.push(`${relativePath}:${line} <${tagName}> has title= — use <Tip>`)
      }
    }

    expect(violations, violations.join('\n')).toEqual([])
  })
})
