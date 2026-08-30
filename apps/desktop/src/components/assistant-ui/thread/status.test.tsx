import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetElapsedTimerRegistryForTests } from '@/components/chat/activity-timer'
import { I18nProvider } from '@/i18n'
import { $providerWaitSessions, setSessionProviderWait } from '@/store/provider-wait'
import { $activeSessionId, $turnStartedAt } from '@/store/session'

import { ResponseLoadingIndicator } from './status'

function renderIndicator() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <ResponseLoadingIndicator />
    </I18nProvider>
  )
}

describe('ResponseLoadingIndicator timer', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'))
    // useViewedInterval gates ticking on document focus + visibility; jsdom's
    // hasFocus() is unreliable across runners, so pin it (same as the
    // background-sync backstop tests).
    vi.spyOn(globalThis.document, 'hasFocus').mockReturnValue(true)
    __resetElapsedTimerRegistryForTests()
  })

  afterEach(() => {
    cleanup()
    $activeSessionId.set(null)
    $turnStartedAt.set(null)
    $providerWaitSessions.set({})
    __resetElapsedTimerRegistryForTests()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('preserves each running session timer while switching between sessions', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    const sessionA = renderIndicator()

    act(() => vi.advanceTimersByTime(5_000))
    expect(screen.getAllByText((_, node) => node?.textContent === '5s').length).toBeGreaterThan(0)
    sessionA.unmount()

    $activeSessionId.set('session-b')
    $turnStartedAt.set(Date.now())
    const sessionB = renderIndicator()

    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.getAllByText((_, node) => node?.textContent === '3s').length).toBeGreaterThan(0)
    sessionB.unmount()

    $activeSessionId.set('session-a')
    $turnStartedAt.set(new Date('2026-01-01T00:00:00.000Z').getTime())
    renderIndicator()

    expect(screen.getAllByText((_, node) => node?.textContent === '8s').length).toBeGreaterThan(0)
  })

  it('names a prolonged provider wait in the existing response status row', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    setSessionProviderWait('session-a', '⏳ waiting on local-model — 30s with no output yet')

    renderIndicator()

    expect(screen.getByText('⏳ waiting on local-model — 30s with no output yet')).toBeTruthy()
  })
})

// The live status line was originally marked as transcript scaffolding, on the
// reasoning that a line sitting between tool rows and thinking headers should
// rest at the same fade rather than claim emphasis it hasn't earned. That is
// right for a settled row and wrong for this one: it exists only while the user
// is waiting on it, and it is the only thing on screen saying the app is alive
// — so the emphasis IS earned, for exactly as long as the row exists.
//
// Carrying the mark anyway multiplied the row by 0.67 on top of its already
// partial text alpha, which is what made it repeatedly unreadable. It now opts
// out and lights itself; see `activity-timer-text.test.tsx` for the alpha stack.
describe('status line', () => {
  afterEach(cleanup)

  it('is not marked as settled scaffolding, so the fade rule cannot dim it', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    const { container } = renderIndicator()

    const row = container.querySelector('[role="status"]')

    expect(row?.hasAttribute('data-conversation-scaffold')).toBe(false)
    expect(row?.hasAttribute('data-activity-strip')).toBe(true)
  })
})
