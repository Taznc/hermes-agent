/**
 * The focus-mode "answer": in one line, why the focused card is (or isn't)
 * held up. Shared by the answer bar above the board and the roll-up on the
 * focused card itself, so both always say the same thing.
 *
 * Pure — reads the same graph + index every other dependency surface reads
 * (`deps.ts`), no round-trips.
 */

import { type DependencyGraph, downstreamOf, isGating, upstreamOf } from './deps'
import type { KanbanTask } from './types'

/** A linked card as the answer bar lists it. `status` is `'unknown'` for a
 *  link whose card the board doesn't have (deleted, filtered by tenant). */
export interface FocusLink {
  key: string
  missing: boolean
  status: string
  title: string
}

/**
 * - `none`: nothing blocks the card;
 * - `clear`: every blocker is satisfied;
 * - `stalled`: every blocker still gating is On hold — nothing will move
 *   until a human lifts the hold, which is the case this view exists to spot;
 * - `waiting`: some blocker is still in flight or parked elsewhere.
 */
export type VerdictKind = 'clear' | 'none' | 'stalled' | 'waiting'

export interface FocusVerdict {
  kind: VerdictKind
  /** Every blocker, in `BLOCKER_ORDER`. */
  blockers: FocusLink[]
  /** Blockers still gating, grouped by status in `BLOCKER_ORDER`. */
  byStatus: Array<[status: string, count: number]>
  /** Blockers no longer gating (done / archived / wishlist). */
  cleared: number
  /** Blockers still gating. A missing blocker counts: its link still exists
   *  in the backend, so it may still be enforced (see `partitionBlockers`). */
  open: number
}

/** Most-stuck first: the rows a human has to act on lead the list, the
 *  satisfied ones trail it. Anything the backend adds later sorts just
 *  before the cleared tail. */
export const BLOCKER_ORDER: readonly string[] = [
  'unknown',
  'blocked',
  'on_hold',
  'review',
  'running',
  'ready',
  'scheduled',
  'todo',
  'triage',
  'idea',
  'roadmap',
  'done',
  'archived'
]

/**
 * A dependency line's colour = its BLOCKER's status, so "is everything that
 * holds this card On hold?" reads straight off the lines. Its own palette,
 * not `COLUMN_META`: a lane dot can afford a muted grey, a 3px line crossing
 * a dimmed board cannot, and On hold needs to stand apart from Blocked and
 * Todo at a glance (it is the one status the answer bar calls out). The
 * answer bar's pills and legend read this same map, so bar and lines agree.
 */
export const LINK_TONE: Readonly<Record<string, string>> = {
  archived: '#4ade80',
  blocked: '#f87171',
  done: '#4ade80',
  idea: '#8b949e',
  on_hold: '#c084fc',
  ready: '#2dd4bf',
  review: '#fbbf24',
  roadmap: '#8b949e',
  running: '#60a5fa',
  scheduled: '#f472b6',
  todo: '#8b949e',
  triage: '#8b949e',
  unknown: '#6b7280'
}

export const linkTone = (status: string): string => LINK_TONE[status] ?? LINK_TONE.todo

/** The statuses the legend explains, in `BLOCKER_ORDER`. */
export const LEGEND_STATUSES: readonly string[] = ['blocked', 'on_hold', 'review', 'running', 'ready', 'todo', 'done']

const LATE = BLOCKER_ORDER.indexOf('idea')

const rank = (status: string) => {
  const at = BLOCKER_ORDER.indexOf(status)

  return at === -1 ? LATE - 0.5 : at
}

export const byBlockerOrder = (a: FocusLink, b: FocusLink) => rank(a.status) - rank(b.status)

const toLink = (key: string, index: Map<string, KanbanTask>): FocusLink => {
  const task = index.get(key)

  return task
    ? { key, missing: false, status: task.status, title: task.title || task.id }
    : { key, missing: true, status: 'unknown', title: '' }
}

const gates = (link: FocusLink) => link.missing || isGating(link.status)

/** The focused card's direct blockers and dependants, resolved and ordered. */
export function focusLinks(
  graph: DependencyGraph,
  index: Map<string, KanbanTask>,
  key: string
): { blockers: FocusLink[]; dependants: FocusLink[] } {
  return {
    blockers: upstreamOf(graph, key)
      .map(k => toLink(k, index))
      .sort(byBlockerOrder),
    dependants: downstreamOf(graph, key)
      .map(k => toLink(k, index))
      .sort(byBlockerOrder)
  }
}

export function focusVerdict(graph: DependencyGraph, index: Map<string, KanbanTask>, key: string): FocusVerdict {
  const { blockers } = focusLinks(graph, index, key)
  const open = blockers.filter(gates)
  const counts = new Map<string, number>()

  for (const link of open) {
    counts.set(link.status, (counts.get(link.status) ?? 0) + 1)
  }

  const byStatus = [...counts].sort(([a], [b]) => rank(a) - rank(b))

  const kind: VerdictKind =
    blockers.length === 0
      ? 'none'
      : open.length === 0
        ? 'clear'
        : open.every(link => link.status === 'on_hold')
          ? 'stalled'
          : 'waiting'

  return { blockers, byStatus, cleared: blockers.length - open.length, kind, open: open.length }
}
