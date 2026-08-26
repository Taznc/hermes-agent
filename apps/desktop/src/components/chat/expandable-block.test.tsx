import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExpandableBlock } from './expandable-block'

// jsdom has no ResizeObserver and reports scrollHeight === 0, so the block
// never flips to `overflowing` on its own. Stub RO to fire immediately and
// force a tall scrollHeight on the observed node so the toggle mounts.
class TestResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element) {
    Object.defineProperty(target, 'scrollHeight', { configurable: true, value: 400 })
    this.callback([{ target } as ResizeObserverEntry], this as unknown as ResizeObserver)
  }

  unobserve() {}
  disconnect() {}
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('ExpandableBlock', () => {
  it('keeps the toggle outside the scroll container, below the scrollbars', () => {
    vi.stubGlobal('ResizeObserver', TestResizeObserver)

    const { container } = render(
      <ExpandableBlock>
        <pre data-testid="content">{'const x = 1\n'.repeat(20)}</pre>
      </ExpandableBlock>
    )

    const inner = container.querySelector('[data-testid="content"]')!.parentElement!
    const toggle = screen.getByRole('button', { name: /expand|collapse/i })
    const fade = screen.getByTestId('expandable-fade')

    // Inner container allows horizontal scroll so wide code gets a scrollbar:
    // platform overlay (`scrollbar-overlay`), not the always-on classic gutter.
    expect(inner.className).toContain('overflow-x-auto')
    expect(inner.className).toContain('scrollbar-overlay')

    // The fade is a pure decorative cue and must not intercept pointer
    // events or carry the click target.
    expect(fade.className).toContain('pointer-events-none')
    expect(fade.getAttribute('role')).not.toBe('button')

    // The toggle is NOT nested inside the scrollable box (the old bug: an
    // icon pinned inside the scroller's own corner, where a vertical or
    // horizontal scrollbar could sit on top of it and eat the click). It is
    // a sibling, full-width row below the box, so it can never overlap
    // either scrollbar regardless of scroll position or code block width.
    expect(inner.contains(toggle)).toBe(false)
    expect(toggle.className).toContain('w-full')
  })

  it('still toggles expanded state when the toggle is clicked', () => {
    vi.stubGlobal('ResizeObserver', TestResizeObserver)

    render(
      <ExpandableBlock>
        <pre data-testid="content">{'line\n'.repeat(20)}</pre>
      </ExpandableBlock>
    )

    const toggle = screen.getByRole('button', { name: 'Expand' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggle)

    expect(screen.getByRole('button', { name: 'Collapse' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('renders no toggle when the content does not overflow', () => {
    render(
      <ExpandableBlock>
        <pre data-testid="content">{'short'}</pre>
      </ExpandableBlock>
    )

    expect(screen.queryByRole('button', { name: /expand|collapse/i })).toBeNull()
  })
})
