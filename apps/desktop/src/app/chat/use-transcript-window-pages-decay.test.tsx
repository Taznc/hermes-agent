import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetThreadScroll, setThreadAtBottom } from '@/store/thread-scroll'

import { useTranscriptWindowPagesDecay } from './use-transcript-window-pages-decay'

// The hook polls every 5s rather than reacting to every scroll/selection
// event (see the module doc). That means eligibility is only ever SAMPLED at
// 5s boundaries: a streak that "starts" between two ticks is recorded as
// starting at the tick that observed it, not the instant it actually began.
// So decay fires at the first poll tick on/after (firstEligibleTick +
// threshold), which can be up to one interval later than the threshold alone
// would suggest. These tests advance to exact tick boundaries to stay
// independent of that rounding rather than fighting it.
const CHECK_INTERVAL_MS = 5_000

function selectSomeText() {
  const range = document.createRange()
  const node = document.createTextNode('hello')
  document.body.appendChild(node)
  range.selectNode(node)

  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

afterEach(() => {
  vi.useRealTimers()
  resetThreadScroll()
  window.getSelection()?.removeAllRanges()
})

describe('useTranscriptWindowPagesDecay', () => {
  it('does not decay while windowPages is 1 (nothing to decay)', () => {
    vi.useFakeTimers()
    setThreadAtBottom(true)
    const onDecay = vi.fn()

    renderHook(() => useTranscriptWindowPagesDecay(1, onDecay, 10_000))

    vi.advanceTimersByTime(60_000)

    expect(onDecay).not.toHaveBeenCalled()
  })

  it('decays after the sustained-bottom threshold with no selection', () => {
    vi.useFakeTimers()
    setThreadAtBottom(true)
    const onDecay = vi.fn()

    renderHook(() => useTranscriptWindowPagesDecay(3, onDecay, 10_000))

    // First poll tick (t=5s) observes eligibility and starts the streak;
    // threshold (10s) hasn't elapsed from that observation yet.
    vi.advanceTimersByTime(CHECK_INTERVAL_MS * 2)
    expect(onDecay).not.toHaveBeenCalled()

    // Third tick (t=15s): 10s have elapsed since the streak was observed
    // starting at t=5s.
    vi.advanceTimersByTime(CHECK_INTERVAL_MS)
    expect(onDecay).toHaveBeenCalledTimes(1)
  })

  it('does not decay while scrolled away from the bottom', () => {
    vi.useFakeTimers()
    setThreadAtBottom(false)
    const onDecay = vi.fn()

    renderHook(() => useTranscriptWindowPagesDecay(3, onDecay, 10_000))

    vi.advanceTimersByTime(60_000)

    expect(onDecay).not.toHaveBeenCalled()
  })

  it('does not decay while there is an active text selection', () => {
    vi.useFakeTimers()
    setThreadAtBottom(true)
    selectSomeText()
    const onDecay = vi.fn()

    renderHook(() => useTranscriptWindowPagesDecay(3, onDecay, 10_000))

    vi.advanceTimersByTime(60_000)

    expect(onDecay).not.toHaveBeenCalled()
  })

  it('resumes decaying once a selection is cleared, after re-earning the streak', () => {
    vi.useFakeTimers()
    setThreadAtBottom(true)
    selectSomeText()
    const onDecay = vi.fn()

    renderHook(() => useTranscriptWindowPagesDecay(3, onDecay, 10_000))

    // Two ticks pass with a selection held: no streak starts.
    vi.advanceTimersByTime(CHECK_INTERVAL_MS * 2)
    expect(onDecay).not.toHaveBeenCalled()

    window.getSelection()?.removeAllRanges()

    // Next tick (t=15s) is the first to observe the cleared selection and
    // starts a fresh streak from there — it must NOT count the two ticks
    // that passed while selecting.
    vi.advanceTimersByTime(CHECK_INTERVAL_MS * 2)
    expect(onDecay).not.toHaveBeenCalled()

    // Two more ticks (t=25s) clears the 10s threshold from the t=15s start.
    vi.advanceTimersByTime(CHECK_INTERVAL_MS * 2)
    expect(onDecay).toHaveBeenCalledTimes(1)
  })
})
