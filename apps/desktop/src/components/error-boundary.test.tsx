import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary, RootErrorBoundary } from './error-boundary'

const CURRENT_LOOKUP_ERROR = new Error('useClientLookup: Index 6 out of bounds (length: 2)')
const RELOAD_WINDOW = { name: 'Reload window', role: 'button' } as const

function makeBomb(box: { error: Error | null }) {
  return function Bomb() {
    if (box.error) {
      throw box.error
    }

    return <div>recovered</div>
  }
}

const recoveryWarningCount = (calls: unknown[][]) =>
  calls.filter(call => call.some(value => String(value).includes('auto-recovering from assistant-ui lookup'))).length

describe('ErrorBoundary assistant-ui lookup recovery', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it.each([
    ['the current useClientLookup error', CURRENT_LOOKUP_ERROR],
    ['the legacy tapClientLookup error', new Error('tapClientLookup: Index 6 out of bounds (length: 2)')],
    ['the legacy tapClientResource error', new Error('tapClientResource: Index 6 out of bounds (length: 2)')]
  ])('recovers the production root composition without StrictMode after %s clears', (_label, error) => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const box: { error: Error | null } = { error }
    const Bomb = makeBomb(box)

    render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    box.error = null
    act(() => vi.runOnlyPendingTimers())

    expect(screen.getByText('recovered')).toBeTruthy()
    expect(screen.queryByRole(RELOAD_WINDOW.role, { name: RELOAD_WINDOW.name })).toBeNull()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(1)
  })

  it('recovers the production root composition when StrictMode replays its mount lifecycle', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const box: { error: Error | null } = { error: CURRENT_LOOKUP_ERROR }
    const Bomb = makeBomb(box)

    render(
      <StrictMode>
        <RootErrorBoundary>
          <Bomb />
        </RootErrorBoundary>
      </StrictMode>
    )

    box.error = null
    act(() => vi.runOnlyPendingTimers())

    expect(screen.getByText('recovered')).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stops retrying a persistent lookup error after the recovery budget is exhausted', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const box: { error: Error | null } = { error: CURRENT_LOOKUP_ERROR }
    const Bomb = makeBomb(box)

    render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    for (let attempt = 0; attempt < 4; attempt += 1) {
      act(() => vi.runOnlyPendingTimers())
    }

    expect(screen.getByRole(RELOAD_WINDOW.role, { name: RELOAD_WINDOW.name })).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(3)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('starts a fresh automatic recovery budget at the 5 second window boundary', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const box: { error: Error | null } = { error: CURRENT_LOOKUP_ERROR }
    const Bomb = makeBomb(box)

    const view = render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    for (let attempt = 0; attempt < 3; attempt += 1) {
      box.error = null
      act(() => vi.runOnlyPendingTimers())
      expect(screen.getByText('recovered')).toBeTruthy()

      if (attempt < 2) {
        box.error = CURRENT_LOOKUP_ERROR
        view.rerender(
          <RootErrorBoundary>
            <Bomb />
          </RootErrorBoundary>
        )
      }
    }

    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(3)
    act(() => vi.advanceTimersByTime(5_000))

    box.error = CURRENT_LOOKUP_ERROR
    view.rerender(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )
    expect(vi.getTimerCount()).toBe(1)

    box.error = null
    act(() => vi.runOnlyPendingTimers())

    expect(screen.getByText('recovered')).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(4)
  })

  it('manual retry replaces an active timer, resets the budget, and can exhaust again', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const box: { error: Error | null } = { error: CURRENT_LOOKUP_ERROR }
    const Bomb = makeBomb(box)

    render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    expect(vi.getTimerCount()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(vi.getTimerCount()).toBe(1)

    for (let attempt = 0; attempt < 3; attempt += 1) {
      act(() => vi.runOnlyPendingTimers())
    }

    expect(screen.getByRole(RELOAD_WINDOW.role, { name: RELOAD_WINDOW.name })).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(4)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cancels an owned automatic recovery timer when unmounted', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const Bomb = makeBomb({ error: CURRENT_LOOKUP_ERROR })

    const view = render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    expect(vi.getTimerCount()).toBe(1)
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
    act(() => vi.runAllTimers())
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(1)
  })

  it('does not auto-recover the same error in a scoped boundary', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const Bomb = makeBomb({ error: CURRENT_LOOKUP_ERROR })

    render(
      <ErrorBoundary fallback={() => <div>scoped fallback</div>} label="thread">
        <Bomb />
      </ErrorBoundary>
    )

    act(() => vi.runAllTimers())

    expect(screen.getByText('scoped fallback')).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(0)
  })

  // The root fallback REPLACES <App/>, and the toast host (NotificationStack)
  // renders inside <App/>. Anything this surface reports through notifyError()
  // is therefore written to a store with no renderer — invisible to the user.
  // These cover the three things a user actually needs off a crash screen.
  describe('root fallback crash-report affordances', () => {
    it('marks the error message selectable so it can be read and copied out', () => {
      // body { user-select: none } is app-wide; the opt-in attribute is the
      // documented escape hatch (styles.css [data-selectable-text='true']).
      const Bomb = makeBomb({
        error: new Error("Cannot read properties of undefined (reading 'getDevMainBundleStale')")
      })

      render(
        <RootErrorBoundary>
          <Bomb />
        </RootErrorBoundary>
      )

      const message = screen.getByText(/getDevMainBundleStale/)

      expect(message.closest('[data-selectable-text="true"]')).not.toBeNull()
    })

    it('copies the message and component stack to the clipboard', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: undefined, writable: true })
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText }, writable: true })

      const Bomb = makeBomb({ error: new Error('boom-copy') })

      render(
        <RootErrorBoundary>
          <Bomb />
        </RootErrorBoundary>
      )

      fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
      await act(async () => { await Promise.resolve() })

      expect(writeText).toHaveBeenCalledTimes(1)
      expect(String(writeText.mock.calls[0][0])).toContain('boom-copy')
      expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
    })

    it('reports the log outcome inline instead of into the unmounted toast host', async () => {
      Object.defineProperty(window, 'hermesDesktop', {
        configurable: true,
        value: {
          getRecentLogs: async () => ({ lines: ['line-one', 'line-two'], path: '/tmp/desktop.log' }),
          revealLogs: async () => ({ error: 'not available in the web spike', ok: false, path: '' })
        },
        writable: true
      })

      const Bomb = makeBomb({ error: new Error('boom-logs') })

      render(
        <RootErrorBoundary>
          <Bomb />
        </RootErrorBoundary>
      )

      fireEvent.click(screen.getByRole('button', { name: 'Open logs' }))
      await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })

      // Falls back to rendering the log tail in-page when the host filesystem
      // cannot be revealed (the web build), rather than doing nothing visible.
      expect(screen.getByText(/line-two/)).toBeTruthy()
      expect(screen.getByText('/tmp/desktop.log')).toBeTruthy()
    })
  })

  it.each([
    ['a differently cased classifier near-miss', new Error('UseClientLookup: Index 6 out of bounds (length: 2)')],
    ['a non-bounds lookup error', new Error('useClientLookup: Key "missing" not found')],
    ['an unrelated render error', new Error('some unrelated application error')]
  ])('does not auto-recover %s at root', (_label, error) => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const Bomb = makeBomb({ error })

    render(
      <RootErrorBoundary>
        <Bomb />
      </RootErrorBoundary>
    )

    act(() => vi.runAllTimers())

    expect(screen.getByRole(RELOAD_WINDOW.role, { name: RELOAD_WINDOW.name })).toBeTruthy()
    expect(recoveryWarningCount(warnSpy.mock.calls)).toBe(0)
  })
})
