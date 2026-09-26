/**
 * Folding a lane around a focused trace. While a card is focused, the cards
 * in a lane that are NOT part of the trace collapse into small "+N cards" gap
 * markers, so every linked card sits on screen at once instead of being
 * spread across a lane the user has to scroll. Pure: the lane renders
 * whatever slots this returns.
 */

/** One rendered slot in a folded lane: a real card, or a run of unrelated
 *  cards folded into a gap marker. A gap's `id` is its first card's key —
 *  stable while that run is unchanged, which is all an "expanded" flag needs. */
export type LaneSlot<T> = { item: T; kind: 'card' } | { id: string; items: T[]; kind: 'gap' }

/**
 * Split `items` into slots, keeping order: every item `keep` accepts renders
 * as itself; each maximal run of the others becomes ONE gap, unless that gap
 * was expanded (`open`), in which case its items render as cards again.
 */
export function foldLane<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
  keep: (item: T) => boolean,
  open: ReadonlySet<string> = new Set()
): LaneSlot<T>[] {
  const slots: LaneSlot<T>[] = []
  let run: T[] = []

  const flush = () => {
    if (run.length === 0) {
      return
    }

    const id = keyOf(run[0])

    if (open.has(id)) {
      slots.push(...run.map(item => ({ item, kind: 'card' as const })))
    } else {
      slots.push({ id, items: run, kind: 'gap' })
    }

    run = []
  }

  for (const item of items) {
    if (keep(item)) {
      flush()
      slots.push({ item, kind: 'card' })
    } else {
      run.push(item)
    }
  }

  flush()

  return slots
}
