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

  it('preserves data-selectable-text on the numbered root', () => {
    const { container } = render(<LogView content="line one" numbered />)
    const root = container.firstElementChild as HTMLElement

    expect(root.dataset.selectableText).toBe('true')
  })

  it('empty content still renders line 1 (a blank line, not nothing)', () => {
    render(<LogView content="" numbered />)

    expect(screen.getByText('1')).toBeTruthy()
  })
})
