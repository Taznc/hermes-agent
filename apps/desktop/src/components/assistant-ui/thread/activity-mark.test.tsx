import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import { THREAD_ACTIVITY_AREA, type ThreadActivityContribution } from '@/lib/thread-activity'

import { ThreadActivityMark } from './activity-mark'

/** Register a `thread.activity` contribution and return its disposer. */
function contributeMark(id: string, render: ThreadActivityContribution['render']) {
  return registry.register({
    area: THREAD_ACTIVITY_AREA,
    data: { render } satisfies ThreadActivityContribution,
    id,
    source: `plugin:${id}`
  })
}

/** Drive `prefers-reduced-motion` for one test. */
function setReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    addEventListener: vi.fn(),
    matches: reduce && query.includes('prefers-reduced-motion'),
    media: query,
    removeEventListener: vi.fn()
  })) as unknown as typeof window.matchMedia
}

const disposers: Array<() => void> = []

afterEach(() => {
  disposers.splice(0).forEach(dispose => dispose())
  vi.unstubAllGlobals()
})

describe('ThreadActivityMark', () => {
  it('renders the core dither pulse when no plugin claims the area', () => {
    const { container } = render(<ThreadActivityMark elapsedSeconds={3} hint="" phase="thinking" slot="turn" />)

    // The seam is invisible until something uses it: an unclaimed area must
    // still paint the mark the transcript has always had.
    expect(container.querySelector('.dither')).not.toBeNull()
  })

  it('renders a claiming plugin mark instead of the core pulse', () => {
    setReducedMotion(false)
    disposers.push(contributeMark('orbit', () => <span data-testid="plugin-mark">orbit</span>))

    const { container } = render(<ThreadActivityMark elapsedSeconds={3} hint="" phase="thinking" slot="turn" />)

    expect(screen.getByTestId('plugin-mark')).toBeTruthy()
    expect(container.querySelector('.dither')).toBeNull()
  })

  it('passes the row state through, including live reduced-motion', () => {
    setReducedMotion(true)
    const seen: unknown[] = []
    disposers.push(
      contributeMark('probe', state => {
        seen.push(state)

        return <span data-testid="plugin-mark" />
      })
    )

    render(<ThreadActivityMark elapsedSeconds={42} hint="Editing" phase="working" slot="response" />)

    // A plugin MUST be able to honor the OS motion preference, so the slot
    // resolves it rather than trusting each caller to remember to pass it.
    expect(seen[0]).toEqual({
      elapsedSeconds: 42,
      hint: 'Editing',
      phase: 'working',
      reducedMotion: true,
      slot: 'response'
    })
  })

  it('keeps the mark decorative so the row keeps the accessible status text', () => {
    disposers.push(contributeMark('orbit', () => <span data-testid="plugin-mark">orbit</span>))

    const { container } = render(<ThreadActivityMark elapsedSeconds={1} hint="" phase="thinking" slot="turn" />)

    // The surrounding StatusRow owns role="status" and the label; a plugin
    // must not be able to make the mark itself announce.
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull()
    expect(container.querySelector('[role="status"]')).toBeNull()
  })

  it('first registration wins — a second plugin does not stack a mark', () => {
    disposers.push(contributeMark('first', () => <span data-testid="first" />))
    disposers.push(contributeMark('second', () => <span data-testid="second" />))

    render(<ThreadActivityMark elapsedSeconds={1} hint="" phase="thinking" slot="turn" />)

    expect(screen.getByTestId('first')).toBeTruthy()
    expect(screen.queryByTestId('second')).toBeNull()
  })

  it('contains a throwing plugin mark in its boundary', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    disposers.push(
      contributeMark('broken', () => {
        throw new Error('bad mark')
      })
    )

    // A plugin that throws loses only the mark — the row (hint + elapsed
    // clock) around it must survive, so this render must not reject.
    expect(() => render(<ThreadActivityMark elapsedSeconds={1} hint="" phase="thinking" slot="turn" />)).not.toThrow()

    error.mockRestore()
  })
})
