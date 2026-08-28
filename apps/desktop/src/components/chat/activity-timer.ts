import { useEffect, useRef, useState } from 'react'

import { useViewedInterval } from '@/hooks/use-viewed-interval'

// Module-level registry so timers survive component unmount/remount (e.g.
// when a tool row scrolls out and back). Keyed by caller-supplied timerKey;
// anonymous timers (no key) start fresh each mount.
//
// Every distinct timerKey ever seen stayed in these maps forever: a
// long-lived session accumulates one entry per tool call / reasoning block
// ever rendered, with nothing ever removing an old one. Size-capped LRU
// (delete-then-set on touch, evict oldest-first on overflow — same recency
// trick as markdown-blocks.ts's exactCache) bounds it without changing
// behavior for the common case of a session with far fewer than the cap.
const TIMER_REGISTRY_MAX = 5000
const startedAtByKey = new Map<string, number>()

// Durations of things that have already finished, kept beside the origins that
// measured them. See `useMeasuredDuration`.
const durationByKey = new Map<string, number>()

function touchLru<V>(map: Map<string, V>, key: string, value: V): void {
  map.delete(key)
  map.set(key, value)

  if (map.size > TIMER_REGISTRY_MAX) {
    const oldestKey = map.keys().next().value as string

    map.delete(oldestKey)
  }
}

function startedAt(key?: string): number {
  if (!key) {
    return Date.now()
  }

  const existing = startedAtByKey.get(key)

  if (existing !== undefined) {
    // Read-touch: refresh recency so a long-lived tool row's timer key isn't
    // the "oldest" entry evicted out from under it just because it started
    // long ago and other keys have been created since.
    touchLru(startedAtByKey, key, existing)

    return existing
  }

  const now = Date.now()
  touchLru(startedAtByKey, key, now)

  return now
}

export function formatElapsed(seconds: number): string {
  if (seconds < 60) {
    return `${seconds}s`
  }

  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/**
 * Seconds since the timer's origin, reported once a second while `active`.
 *
 * Origin, in order: an explicit `since` timestamp, else the `timerKey`'s
 * registry entry (survives unmount/remount), else mount time. Pass `since` when
 * the thing being measured started at a moment the caller knows and that moment
 * isn't the mount — otherwise an anonymous timer reports the component's age,
 * which is only the same number by accident.
 */
export function useElapsedSeconds(active = true, timerKey?: string, since?: number): number {
  const start = useRef(since ?? startedAt(timerKey))
  const lastKey = useRef(timerKey)
  const [elapsed, setElapsed] = useState(() => Math.max(0, Math.floor((Date.now() - start.current) / 1000)))

  if (lastKey.current !== timerKey) {
    start.current = since ?? startedAt(timerKey)
    lastKey.current = timerKey
  }

  // eslint-disable-next-line no-restricted-syntax -- timer origin is imperative state, not an atom mirror
  useEffect(() => {
    if (since !== undefined) {
      start.current = since
    } else if (timerKey) {
      start.current = startedAt(timerKey)
    }

    if (active) {
      setElapsed(Math.max(0, Math.floor((Date.now() - start.current) / 1000)))
    }
  }, [active, since, timerKey])

  useViewedInterval(() => setElapsed(Math.max(0, Math.floor((Date.now() - start.current) / 1000))), 1000, active)

  return elapsed
}

/**
 * How long something took, measured by watching it finish and remembered
 * afterwards. `null` until it has been watched at least once.
 *
 * Some durations exist nowhere but in the watching. A reasoning block is the
 * case this was written for: the persisted turn records the text the model
 * thought, never how long it spent thinking it, so the only way to know is to
 * have been there. Watching alone isn't enough either — the thread virtualizes,
 * so the component that saw a block finish is usually gone by the time anyone
 * scrolls back to read it. Keeping the number in the same registry as the
 * timer's origin lets it outlive the component that measured it.
 *
 * A block that was never watched running — history loaded from an earlier app
 * session, or reasoning that arrived already complete — has no duration and
 * says so, rather than reporting a timer that never ran.
 */
export function useMeasuredDuration(active: boolean, timerKey: string): null | number {
  const elapsed = useElapsedSeconds(active, timerKey)
  const [watching, setWatching] = useState(false)
  const [measured, setMeasured] = useState<null | number>(() => durationByKey.get(timerKey) ?? null)

  useEffect(() => {
    if (active) {
      setWatching(true)
    } else if (watching) {
      const finalElapsed = Math.max(elapsed, Math.floor((Date.now() - startedAt(timerKey)) / 1000))

      setWatching(false)
      touchLru(durationByKey, timerKey, finalElapsed)
      setMeasured(finalElapsed)
    }
  }, [active, elapsed, timerKey, watching])

  return measured
}

export function __resetElapsedTimerRegistryForTests() {
  startedAtByKey.clear()
  durationByKey.clear()
}

// Test-only introspection for the LRU cap contract.
export function __timerRegistrySizesForTests() {
  return { durationByKey: durationByKey.size, startedAtByKey: startedAtByKey.size }
}

export const __TIMER_REGISTRY_MAX_FOR_TESTS = TIMER_REGISTRY_MAX
