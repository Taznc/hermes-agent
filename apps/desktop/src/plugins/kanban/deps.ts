/** Dependency-chain helpers shared by the drawer (§1), the card footer (§2),
 *  and the board's focus mode (§3).
 *
 *  Vocabulary, fixed here so every surface agrees: an edge is
 *  `[parent_id, child_id]` and means **the parent blocks the child**. So for
 *  a given task, its `parents` are its BLOCKERS and its `children` are its
 *  DEPENDANTS. The backend's `/tasks/:id` `links` field uses the same names.
 *
 *  Everything below reads the board cache the UI already holds — no extra
 *  round-trips. All of it is null-safe against an older backend that doesn't
 *  send `link_edges`.
 */

import type { KanbanBoard, KanbanTask, ResolvedLink } from './types'

/** A blocker stops gating once it reaches a terminal state. `done` is the
 *  dispatcher's own promotion rule (a child promotes when every parent is
 *  done); `archived` is treated the same way here because an archived task
 *  will never complete and would otherwise gate forever. */
export const GATING_CLEARED: ReadonlySet<string> = new Set(['done', 'archived'])

export const isGating = (status: string): boolean => !GATING_CLEARED.has(status)

/** Flatten every column into one id→task index. */
export function indexBoard(board: KanbanBoard | undefined): Map<string, KanbanTask> {
  const index = new Map<string, KanbanTask>()

  if (!board) {
    return index
  }

  for (const column of board.columns) {
    for (const task of column.tasks) {
      index.set(task.id, task)
    }
  }

  return index
}

/** Resolve raw link ids against the board index. Ids the board doesn't have
 *  still produce a row (flagged `missing`) — a dangling link is exactly the
 *  thing the user needs to see so they can cut it. */
export function resolveLinks(ids: string[], index: Map<string, KanbanTask>): ResolvedLink[] {
  return ids.map(id => {
    const task = index.get(id)

    return task
      ? { id, title: task.title, status: task.status, assignee: task.assignee, missing: false }
      : { id, title: '', status: 'unknown', assignee: null, missing: true }
  })
}

/** Split blockers into the ones still holding the gate and the ones already
 *  satisfied. A `missing` blocker counts as gating: the backend link still
 *  exists, so it may still be enforced — surface it rather than hide it. */
export function partitionBlockers(links: ResolvedLink[]): { gating: ResolvedLink[]; satisfied: ResolvedLink[] } {
  const gating: ResolvedLink[] = []
  const satisfied: ResolvedLink[] = []

  for (const link of links) {
    if (link.missing || isGating(link.status)) {
      gating.push(link)
    } else {
      satisfied.push(link)
    }
  }

  return { gating, satisfied }
}

/** Adjacency built once per board payload, then shared by every card.
 *  `blockedBy`: who gates this task. `blocking`: who waits on it. */
export interface DependencyGraph {
  blockedBy: Map<string, string[]>
  blocking: Map<string, string[]>
}

const EMPTY: readonly string[] = []

export function buildGraph(board: KanbanBoard | undefined): DependencyGraph {
  const blockedBy = new Map<string, string[]>()
  const blocking = new Map<string, string[]>()

  for (const edge of board?.link_edges ?? []) {
    // Defensive: tolerate a malformed row rather than throwing mid-render.
    if (!Array.isArray(edge) || edge.length < 2) {
      continue
    }

    const [parent, child] = edge

    if (!parent || !child) {
      continue
    }

    const parents = blockedBy.get(child)
    parents ? parents.push(parent) : blockedBy.set(child, [parent])

    const children = blocking.get(parent)
    children ? children.push(child) : blocking.set(parent, [child])
  }

  return { blockedBy, blocking }
}

export const upstreamOf = (graph: DependencyGraph, id: string): readonly string[] => graph.blockedBy.get(id) ?? EMPTY

export const downstreamOf = (graph: DependencyGraph, id: string): readonly string[] => graph.blocking.get(id) ?? EMPTY

/** How a card's blockers stand, for the footer chips. `total` counts links,
 *  `gating` counts the ones not yet done — so `total > 0 && gating === 0` is
 *  the "all clear, promote me" case worth calling out in green. */
export interface BlockerStand {
  total: number
  gating: number
}

export function blockerStand(
  graph: DependencyGraph,
  index: Map<string, KanbanTask>,
  id: string
): BlockerStand {
  const parents = upstreamOf(graph, id)
  let gating = 0

  for (const parent of parents) {
    const task = index.get(parent)

    // Unknown parent → assume it still gates (see partitionBlockers).
    if (!task || isGating(task.status)) {
      gating += 1
    }
  }

  return { total: parents.length, gating }
}

/** The set to keep lit when a card is focused: itself + direct neighbours.
 *  Deliberately ONE hop. Transitive closure on a busy board lights up nearly
 *  everything, which defeats the point of dimming. */
export function focusSets(
  graph: DependencyGraph,
  id: string
): { upstream: Set<string>; downstream: Set<string> } {
  return {
    upstream: new Set(upstreamOf(graph, id)),
    downstream: new Set(downstreamOf(graph, id))
  }
}
