import type { ReactNode } from 'react'

/**
 * THREAD ACTIVITY — the transcript's active-work indicator as a contribution
 * area.
 *
 * The core renders one canonical indicator: a pulsing mark, an optional named
 * hint, and the elapsed clock. That row is deliberately quiet, and quiet is
 * the right default — but "quiet" and "legible" are a real trade-off, and it
 * is a presentation choice, not agent behavior. So the mark itself is a slot.
 *
 * A plugin registers a `thread.activity` contribution to draw the indicator's
 * VISUAL its own way, from the same state the core row reads. Nothing else
 * moves: the row, its `role="status"` label, the hint text, and the elapsed
 * timer stay core-owned, so a plugin cannot cost the user the accessible
 * status line or the honest clock by being prettier.
 *
 * Precedence is first-wins, matching `transcript.directives`: exactly one
 * indicator renders, and a second plugin claiming the area is inert rather
 * than stacked. When no plugin claims it, the core mark renders unchanged —
 * this seam is invisible until something uses it.
 */

export const THREAD_ACTIVITY_AREA = 'thread.activity'

/**
 * Which kind of wait the transcript is showing. These are presentation states,
 * not backend statuses — a plugin styles them; it does not infer agent
 * behavior from them.
 *
 * - `thinking`  — the turn is working and the wait has no name yet (the
 *                 pre-first-token spinner, or a quiet gap between tool calls).
 * - `working`   — the wait is named (`hint` is non-empty): a tool is being
 *                 drafted, or the provider told us what it is doing.
 * - `compacting`— auto-compaction owns the whole turn; `hint` is the fixed
 *                 compaction label and `elapsedSeconds` counts the turn, not
 *                 the gap.
 */
export type ThreadActivityPhase = 'thinking' | 'working' | 'compacting'

/** State handed to a `thread.activity` contribution's `render`. */
export interface ThreadActivityState {
  /** Which flavor of wait this is. */
  phase: ThreadActivityPhase
  /** The named wait, or `''` when the gap is unnamed. Already localized and
   *  already rendered as text by the core row — supplied here for shaping the
   *  visual (a tool name can pick an icon), NOT for re-printing. */
  hint: string
  /** Whole seconds on the core row's clock. Same value the core prints, so a
   *  plugin can react to a long wait (calm the motion down, widen a ring)
   *  without starting a second timer that could disagree with it. */
  elapsedSeconds: number
  /** True when the OS asks for reduced motion. Read at render time and kept
   *  live, so a plugin gets the flip mid-session. A plugin MUST honor this:
   *  render a static, still-legible mark instead of animating. */
  reducedMotion: boolean
  /** Which indicator row is mounting this: the pre-first-token spinner
   *  (`'response'`) or the tail activity row (`'turn'`). */
  slot: 'response' | 'turn'
}

/** Payload of a `thread.activity` contribution's `data`. */
export interface ThreadActivityContribution {
  /**
   * Renders the indicator mark. Mounted inside the contribution error
   * boundary and marked `aria-hidden` by the host, because the row around it
   * already carries the accessible status text — a throw here degrades to a
   * small inline error, never a dead transcript and never a lost status line.
   *
   * Keep the mark inline-sized and roughly square (the core mark is
   * `0.75rem`); the row is a text line, and an oversized mark reflows it.
   */
  render: (state: ThreadActivityState) => ReactNode
}
