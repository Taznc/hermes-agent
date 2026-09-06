/**
 * Worker log UX contracts: the drawer reads the complete retained artifact,
 * follows an active worker without stealing a manual inspection, and never
 * makes users page through an arbitrary short tail.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { WorkerLogSection } from './drawer_log'

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

beforeAll(() => {
  window.requestAnimationFrame = (cb: FrameRequestCallback) =>
    setTimeout(() => cb(performance.now()), 0) as unknown as number
  window.cancelAnimationFrame = (handle: number) => clearTimeout(handle as unknown as NodeJS.Timeout)
})

afterEach(cleanup)

const truncatedLog = {
  content: 'first meaningful line\nlatest meaningful line',
  exists: true,
  size_bytes: 2_000_000,
  truncated: true
}

describe('WorkerLogSection', () => {
  it('presents a live, fully retained log without a Show more pagination action', () => {
    render(<WorkerLogSection live log={truncatedLog} />)

    expect(screen.getByText('workerLogLive')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'workerLogShowMore' })).toBeNull()
    expect(screen.getByRole('button', { name: 'workerLogWrap' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('first meaningful line')).toBeTruthy()
  })

  it('releases follow when a reader scrolls away, then jumps back to the latest line on request', () => {
    const { container } = render(<WorkerLogSection live log={truncatedLog} />)
    const viewport = container.querySelector<HTMLElement>('[data-kanban-worker-log="true"]')!

    Object.defineProperties(viewport, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 500 }
    })
    viewport.scrollTop = 0
    fireEvent.scroll(viewport)

    const jump = screen.getByRole('button', { name: 'workerLogJumpToLatest' })
    fireEvent.click(jump)

    expect(viewport.scrollTop).toBe(500)
    expect(screen.queryByRole('button', { name: 'workerLogJumpToLatest' })).toBeNull()
  })
})
