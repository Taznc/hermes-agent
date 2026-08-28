import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { $threadScrolledUp } from '@/store/thread-scroll'

import {
  INITIAL_WINDOW_PAGES_DECAY_STATE,
  shouldDecayWindowPages,
  TRANSCRIPT_WINDOW_DECAY_AFTER_MS,
  updateDecayEligibility,
  type WindowPagesDecayState
} from './transcript-window-decay'

const CHECK_INTERVAL_MS = 5_000

function hasActiveSelection(): boolean {
  if (typeof window === 'undefined') {
    return false
  }

  const selection = window.getSelection()

  return Boolean(selection && !selection.isCollapsed && selection.toString().length > 0)
}

/**
 * Wires `transcript-window-decay`'s pure eligibility logic to real time,
 * real scroll position, and the real DOM selection. See that module for the
 * decision itself; this hook only supplies the three inputs it needs and
 * calls `onDecay` once the sustained-bottom streak clears the threshold.
 *
 * A polling interval (not a scroll/selection event listener) is deliberate:
 * the decay is a background reclaim, not a UI reaction, so it only needs to
 * notice eventually — a 5s poll is (threshold / 5s) times cheaper than
 * subscribing to high-frequency scroll/selectionchange events for a check
 * this coarse.
 */
export function useTranscriptWindowPagesDecay(
  windowPages: number,
  onDecay: () => void,
  thresholdMs = TRANSCRIPT_WINDOW_DECAY_AFTER_MS
): void {
  const scrolledUp = useStore($threadScrolledUp)
  const atBottom = !scrolledUp
  const stateRef = useRef<WindowPagesDecayState>(INITIAL_WINDOW_PAGES_DECAY_STATE)
  const onDecayRef = useRef(onDecay)
  onDecayRef.current = onDecay

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment): stateRef is this hook's own decay accumulator, not a mirror of a reactive atom
  useEffect(() => {
    if (windowPages <= 1) {
      stateRef.current = INITIAL_WINDOW_PAGES_DECAY_STATE

      return
    }

    const tick = () => {
      stateRef.current = updateDecayEligibility(stateRef.current, windowPages, atBottom, hasActiveSelection(), Date.now())

      if (shouldDecayWindowPages(stateRef.current, Date.now(), thresholdMs)) {
        stateRef.current = INITIAL_WINDOW_PAGES_DECAY_STATE
        onDecayRef.current()
      }
    }

    const interval = setInterval(tick, CHECK_INTERVAL_MS)

    return () => clearInterval(interval)
  }, [windowPages, atBottom, thresholdMs])
}
