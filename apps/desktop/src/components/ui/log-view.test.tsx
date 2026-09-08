/**
 * Focused tests for LogView's numbered variant — line numbers, monospace
 * alignment (no mid-token wrap), and that the default (non-numbered) mode is
 * untouched for the other call sites (install/boot-failure overlays).
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { LogView } from './log-view'

afterEach(() => {
  cleanup()
})

describe('LogView default mode (unchanged — install/boot-failure overlays)', () => {
  it('wraps long text and preserves data-selectable-text', () => {
    const { container } = render(<LogView>hello world</LogView>)
    const root = container.firstElementChild as HTMLElement

    expect(root.dataset.selectableText).toBe('true')
    expect(root.className).toContain('whitespace-pre-wrap')
    expect(root.className).toContain('break-words')
    expect(screen.getByText('hello world')).toBeTruthy()
  })
})

describe('LogView numbered mode', () => {
  it('renders one line number per line, 1-indexed', () => {
    render(<LogView content={'first\nsecond\nthird'} numbered />)

    expect(screen.getByText('1')).toBeTruthy()
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('first')).toBeTruthy()
    expect(screen.getByText('second')).toBeTruthy()
    expect(screen.getByText('third')).toBeTruthy()
  })

  it('drops the phantom trailing empty line from a final newline', () => {
    render(<LogView content={'a\nb\n'} numbered />)

    expect(screen.getByText('1')).toBeTruthy()
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.queryByText('3')).toBeNull()
  })

  it('never wraps mid-token — each line is whitespace-pre, not pre-wrap/break-words', () => {
    const longCommand = '.venv/bin/pip install -q --disable-pip-version-check -e .[dev]'
    const { container } = render(<LogView content={longCommand} numbered />)

    const lineSpans = Array.from(container.querySelectorAll('span.whitespace-pre'))
    expect(lineSpans).toHaveLength(1)
    expect(lineSpans[0].textContent).toBe(longCommand)
    // The container scrolls horizontally instead of wrapping; no
    // whitespace-pre-wrap/break-words classes anywhere in numbered mode.
    expect(container.innerHTML).not.toContain('whitespace-pre-wrap')
    expect(container.innerHTML).not.toContain('break-words')
  })

  it('can opt into readable wrapped lines without losing the numbered gutter', () => {
    const longCommand = '.venv/bin/pip install -q --disable-pip-version-check -e .[dev]'
    const { container } = render(<LogView content={longCommand} numbered wrap />)

    expect(screen.getByText('1')).toBeTruthy()
    expect(container.querySelector('span.whitespace-pre-wrap')?.textContent).toBe(longCommand)
    expect(container.innerHTML).toContain('grid-cols-[auto_minmax(0,1fr)]')
  })

  it('preserves data-selectable-text on the numbered root', () => {
    const { container } = render(<LogView content="line one" numbered />)
    const root = container.firstElementChild as HTMLElement

    expect(root.dataset.selectableText).toBe('true')
  })

  it('empty content still renders line 1 (a blank line, not nothing)', () => {
    render(<LogView content="" numbered />)

    expect(screen.getByText('1')).toBeTruthy()
  })

  // Worker log lines carry a `[YYYY-MM-DD HH:MM:SS] ` prefix written by the
  // dispatcher's log filter. A prefix (rather than a second gutter column) was
  // chosen precisely so numbered mode needs no per-line parsing: a stamped line
  // is ordinary text. The risk that buys is a MIXED file — a task whose earlier
  // runs predate timestamps and whose later runs have them — so that is what is
  // pinned here.
  it('renders a mixed timestamped/legacy log with one gutter number per line', () => {
    const mixed = [
      '--- hermes-kanban-run:1 ---',
      'legacy line with no timestamp',
      '--- hermes-kanban-run:2 ---',
      '[2026-09-07 20:14:03] stamped line',
      '[2026-09-07 20:14:04] another stamped line'
    ].join('\n')
    const { container } = render(<LogView content={mixed} numbered />)

    // Five lines, numbered 1..5 — the gutter counts lines, not formats.
    expect(screen.getByText('5')).toBeTruthy()
    expect(screen.queryByText('6')).toBeNull()
    expect(screen.getByText('legacy line with no timestamp')).toBeTruthy()
    expect(screen.getByText('[2026-09-07 20:14:03] stamped line')).toBeTruthy()
    // The run marker keeps its exact on-disk shape — unstamped, one line.
    expect(screen.getByText('--- hermes-kanban-run:2 ---')).toBeTruthy()

    const lineSpans = Array.from(container.querySelectorAll('span.whitespace-pre'))
    expect(lineSpans).toHaveLength(5)
  })
})
