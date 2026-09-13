// Trailing-edge decay for the transcript window's "Show earlier" pages.
//
// `windowPages` (see index.tsx / transcript-window.ts) only ever GROWS: each
// "Show earlier" click bumps it, and nothing ever brings it back down except
// a session switch. A user who expands a long session once, then reads at
// the bottom for the rest of a multi-day-open window, keeps every one of
// those extra pages materialized in the runtime repository for no reason —
// the whole point of the window is to bound what's live, and an
// expand-only ratchet defeats that over time.
//
// The decision is intentionally conservative: pages only decay after the
// user has been sitting at the bottom of the thread, with no active text
// selection (decaying out from under a selection would clear it and is a
// jarring surprise), for a full sustained interval — not on the first tick
// that happens to satisfy both. `updateDecayEligibility` tracks *when* the
// conditions were last continuously true; `shouldDecayWindowPages` checks
// whether that streak has run long enough. Both are pure so the timing
// contract is unit-testable without a real timer or a DOM selection.

export const TRANSCRIPT_WINDOW_DECAY_AFTER_MS = 60_000

export interface WindowPagesDecayState {
  /** Timestamp the eligibility conditions became continuously true, or null. */
  eligibleSince: null | number
}

export const INITIAL_WINDOW_PAGES_DECAY_STATE: WindowPagesDecayState = { eligibleSince: null }

/**
 * Recompute eligibility for one tick. Any condition failing resets the
 * streak to null (must be freshly re-earned, not resumed); all conditions
 * holding starts or continues the streak from its original start time.
 */
export function updateDecayEligibility(
  state: WindowPagesDecayState,
  windowPages: number,
  atBottom: boolean,
  hasSelection: boolean,
  now: number
): WindowPagesDecayState {
  if (windowPages <= 1 || !atBottom || hasSelection) {
    return INITIAL_WINDOW_PAGES_DECAY_STATE
  }

  return state.eligibleSince === null ? { eligibleSince: now } : state
}

/** Has the current eligibility streak run at least `thresholdMs`? */
export function shouldDecayWindowPages(
  state: WindowPagesDecayState,
  now: number,
  thresholdMs = TRANSCRIPT_WINDOW_DECAY_AFTER_MS
): boolean {
  return state.eligibleSince !== null && now - state.eligibleSince >= thresholdMs
}
