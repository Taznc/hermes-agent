/**
 * The dependency VIEW: the context shared by every card (graph + cardKey→task
 * index + current focus), plus the focus/role predicates every card and
 * `LaneCardFooter` read. Parallels `deps.ts`, the pure adjacency-building
 * half — this is the React-component half consumed from `board.tsx`.
 */

import { createContext, useContext } from 'react'

import { type DependencyGraph, downstreamOf, taskCardKey, upstreamOf } from './deps'
import type { KanbanTask } from './types'

/**
 * The board's dependency adjacency, its cardKey→task index, and the current
 * focus, handed to cards through context rather than threaded as props:
 * `Column` already carries a dozen callbacks, and every card needs the same
 * three objects. The graph and index are built ONCE per board payload up in
 * `KanbanBoardPage` — rebuilding them per card would be O(cards × edges) on
 * every render.
 *
 * Every id-shaped value here is a `cardKey` (board + id in All Boards mode,
 * bare id in single-board mode), because task ids are only unique per board.
 *
 * `hasEdges` is the capability probe for an older backend that sends
 * `link_counts` but not `link_edges`: without edges we can still show honest
 * counts, but we cannot know which blockers are still gating.
 */
export interface DependencyView {
  downstream: ReadonlySet<string>
  focused: null | string
  graph: DependencyGraph
  hasEdges: boolean
  index: Map<string, KanbanTask>
  onFocus: (key: string) => void
  /** Focus `key` AND open the graph overlay centred on it. */
  onOpenGraph: (key: string) => void
  upstream: ReadonlySet<string>
}

export const EMPTY_IDS: ReadonlySet<string> = new Set<string>()

// Module-level constant so the context default keeps a stable identity across
// renders (a fresh object here would re-render every consumer for nothing).
export const NO_DEPENDENCIES: DependencyView = {
  downstream: EMPTY_IDS,
  focused: null,
  graph: { blockedBy: new Map(), blocking: new Map() },
  hasEdges: false,
  index: new Map(),
  onFocus: () => {},
  onOpenGraph: () => {},
  upstream: EMPTY_IDS
}

export const DependencyContext = createContext<DependencyView>(NO_DEPENDENCIES)

export const useDependencies = () => useContext(DependencyContext)

export type FocusRole = 'downstream' | 'focused' | 'upstream'

/** Where a card sits relative to the focused one, by `cardKey`. `null` while
 *  nothing is focused AND for unrelated cards — callers tell them apart via
 *  `focused`. */
export function focusRole(deps: DependencyView, key: string): FocusRole | null {
  if (!deps.focused) {
    return null
  }

  if (deps.focused === key) {
    return 'focused'
  }

  return deps.upstream.has(key) ? 'upstream' : deps.downstream.has(key) ? 'downstream' : null
}

/** Does this card have any link at all — i.e. is focusing it meaningful? */
export function hasDependencies(deps: DependencyView, task: KanbanTask): boolean {
  if (deps.hasEdges) {
    const key = taskCardKey(task)

    return upstreamOf(deps.graph, key).length > 0 || downstreamOf(deps.graph, key).length > 0
  }

  return Boolean(task.link_counts && (task.link_counts.parents > 0 || task.link_counts.children > 0))
}

/**
 * Statuses where "every blocker is done" is ACTIONABLE news worth a green chip.
 *
 * Deliberately narrow, and please don't "simplify" this gate away. On a real
 * board the overwhelmingly common shape of `parents > 0 && gating === 0` is a
 * card that is ITSELF already done — it finished long after its blockers did.
 * Measured on the reference board: of 32 tasks with all blockers satisfied, 30
 * were `done` and 2 were `running`. Dropping the status gate paints 30 done
 * cards green and drowns the handful that actually need a human to move them.
 * Only a card still parked in a waiting lane can act on the news.
 */
export const PROMOTABLE_STATUSES: ReadonlySet<string> = new Set(['on_hold', 'scheduled', 'todo', 'triage'])
