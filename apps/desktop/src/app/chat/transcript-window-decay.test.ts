import { describe, expect, it } from 'vitest'

import {
  INITIAL_WINDOW_PAGES_DECAY_STATE,
  shouldDecayWindowPages,
  TRANSCRIPT_WINDOW_DECAY_AFTER_MS,
  updateDecayEligibility
} from './transcript-window-decay'

const T0 = 1_700_000_000_000

describe('updateDecayEligibility', () => {
  it('stays ineligible while windowPages is 1 (nothing expanded yet)', () => {
    const state = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 1, true, false, T0)

    expect(state.eligibleSince).toBeNull()
  })

  it('starts the streak once expanded, at bottom, with no selection', () => {
    const state = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 2, true, false, T0)

    expect(state.eligibleSince).toBe(T0)
  })

  it('does not restart an already-running streak', () => {
    const started = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 2, true, false, T0)
    const held = updateDecayEligibility(started, 2, true, false, T0 + 30_000)

    expect(held.eligibleSince).toBe(T0)
  })

  it('resets when the user scrolls away from the bottom', () => {
    const started = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 2, true, false, T0)
    const scrolledAway = updateDecayEligibility(started, 2, false, false, T0 + 10_000)

    expect(scrolledAway.eligibleSince).toBeNull()
  })

  it('resets while there is an active text selection', () => {
    const started = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 2, true, false, T0)
    const selecting = updateDecayEligibility(started, 2, true, true, T0 + 10_000)

    expect(selecting.eligibleSince).toBeNull()
  })

  it('re-earns the streak from scratch after a reset, not resumed', () => {
    const started = updateDecayEligibility(INITIAL_WINDOW_PAGES_DECAY_STATE, 2, true, false, T0)
    const interrupted = updateDecayEligibility(started, 2, false, false, T0 + 5_000)
    const resumed = updateDecayEligibility(interrupted, 2, true, false, T0 + 6_000)

    expect(resumed.eligibleSince).toBe(T0 + 6_000)
  })
})

describe('shouldDecayWindowPages', () => {
  it('is false before the threshold elapses', () => {
    const state = { eligibleSince: T0 }

    expect(shouldDecayWindowPages(state, T0 + TRANSCRIPT_WINDOW_DECAY_AFTER_MS - 1)).toBe(false)
  })

  it('is true once the threshold has elapsed', () => {
    const state = { eligibleSince: T0 }

    expect(shouldDecayWindowPages(state, T0 + TRANSCRIPT_WINDOW_DECAY_AFTER_MS)).toBe(true)
  })

  it('is false when no streak is running', () => {
    expect(shouldDecayWindowPages(INITIAL_WINDOW_PAGES_DECAY_STATE, T0 + 10_000_000)).toBe(false)
  })

  it('honors a custom threshold', () => {
    const state = { eligibleSince: T0 }

    expect(shouldDecayWindowPages(state, T0 + 4_999, 5_000)).toBe(false)
    expect(shouldDecayWindowPages(state, T0 + 5_000, 5_000)).toBe(true)
  })
})
