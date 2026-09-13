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

import { type KanbanBoard, type KanbanTask, type ResolvedLink, ROADMAP_LANES } from './types'

/** A blocker stops gating once it reaches a terminal state. `done` is the
 *  dispatcher's own promotion rule (a child promotes when every parent is
 *  done); `archived` is treated the same way here because an archived task
 *  will never complete and would otherwise gate forever.
 *
 *  The wishlist lanes (`idea`/`roadmap`) clear for the opposite reason: they
 *  are INERT, not terminal. No automation ever selects them (the backend's own
 *  `kanban_db.NON_ACTIVE_STATUSES` — used by `_ACTIVE_CHILDREN_SQL` and the
 *  coedit stalled-holder query — excludes both), so a wishlist child can never
 *  become work and must not read as something holding a real card up. */
export const GATING_CLEARED: ReadonlySet<string> = new Set(['done', 'archived', ...ROADMAP_LANES])

export const isGating = (status: string): boolean => !GATING_CLEARED.has(status)

/** A card's identity is the (board, id) PAIR, never the bare id. `GET
 *  /board/all` says so outright — *"task ids are only unique per board —
 *  clients key on the pair"* — and the merged All Boards index drives
 *  mutation routing, so two cards sharing an id must not collapse onto one
 *  board's row (that is the one path that could send a delete to the wrong
 *  board's DB).
 *
 *  In single-board mode a task carries no `board`, so the key IS the bare id
 *  and every single-board path is byte-for-byte unchanged. The separator is
 *  NUL because a board slug is a filesystem-safe identifier that can never
 *  contain one, so no (board, id) pair can be spelled two ways. */
export const cardKey = (id: string, board?: null | string): string => (board ? `${board}\u0000${id}` : id)

/** `cardKey` for a task that already knows its own board. */
export const taskCardKey = (task: Pick<KanbanTask, 'board' | 'id'>): string => cardKey(task.id, task.board)

/** Split a `cardKey` back into its parts, so a surface holding only a key
 *  (the open-drawer pointer, a focused card) can still route by board without
 *  a second index lookup that a refresh may have invalidated. */
export function parseCardKey(key: string): { board?: string; id: string } {
  const at = key.indexOf('\u0000')

  return at === -1 ? { id: key } : { board: key.slice(0, at), id: key.slice(at + 1) }
}

/** Flatten every column into one cardKey→task index (see `cardKey`: the key
 *  is `board + id` in All Boards mode, the bare id in single-board mode). */
export function indexBoard(board: KanbanBoard | undefined): Map<string, KanbanTask> {
  const index = new Map<string, KanbanTask>()

  if (!board) {
    return index
  }

  for (const column of board.columns) {
    for (const task of column.tasks) {
      index.set(taskCardKey(task), task)
    }
  }

  return index
}

/** Resolve raw link ids against the board index. `board` scopes the lookup to
 *  the linked task's own board — links only ever exist within one board, so a
 *  drawer open on a `homelab` card resolves its blocker ids against `homelab`
 *  rows even while the merged All Boards index also holds a same-id card from
 *  somewhere else. Omit it in single-board mode (keys are bare ids there).
 *
 *  Ids the board doesn't have still produce a row (flagged `missing`) — a
 *  dangling link is exactly the thing the user needs to see so they can cut
 *  it. */
export function resolveLinks(ids: string[], index: Map<string, KanbanTask>, board?: null | string): ResolvedLink[] {
  return ids.map(id => {
    const task = index.get(cardKey(id, board))

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
 *  `blockedBy`: who gates this task. `blocking`: who waits on it.
 *
 *  Both maps are keyed by `cardKey` (board + id in All Boards mode, bare id
 *  in single-board mode) and hold `cardKey`s, so a chain never crosses from
 *  one board's card onto a same-id card from another board. */
export interface DependencyGraph {
  blockedBy: Map<string, string[]>
  blocking: Map<string, string[]>
}

const EMPTY: readonly string[] = []

/** Normalize one `link_edges` row to `[parentKey, childKey]`, or null when the
 *  row is unusable. Two wire shapes exist and both are supported here rather
 *  than at every call site:
 *
 *  - single-board `GET /board`: `[parent_id, child_id]` — no board, bare ids;
 *  - consolidated `GET /board/all`: `{board, parent, child}` — the owning
 *    board travels with the edge, and links only ever exist WITHIN a board.
 *
 *  Silently dropping the object form (the old `Array.isArray` guard did) left
 *  the All Boards view with `hasEdges === true` and an empty graph, so every
 *  card claimed zero blockers and focus mode lit nothing. */
function edgeKeys(edge: unknown): null | [string, string] {
  if (Array.isArray(edge)) {
    const [parent, child] = edge

    return parent && child ? [cardKey(parent as string), cardKey(child as string)] : null
  }

  if (edge && typeof edge === 'object') {
    const { board, child, parent } = edge as { board?: null | string; child?: string; parent?: string }

    return parent && child ? [cardKey(parent, board), cardKey(child, board)] : null
  }

  return null
}

export function buildGraph(board: KanbanBoard | undefined): DependencyGraph {
  const blockedBy = new Map<string, string[]>()
  const blocking = new Map<string, string[]>()

  for (const edge of board?.link_edges ?? []) {
    // Defensive: tolerate a malformed row rather than throwing mid-render.
    const keys = edgeKeys(edge)

    if (!keys) {
      continue
    }

    const [parent, child] = keys

    const parents = blockedBy.get(child)
    parents ? parents.push(parent) : blockedBy.set(child, [parent])

    const children = blocking.get(parent)
    children ? children.push(child) : blocking.set(parent, [child])
  }

  return { blockedBy, blocking }
}

export const upstreamOf = (graph: DependencyGraph, key: string): readonly string[] => graph.blockedBy.get(key) ?? EMPTY

export const downstreamOf = (graph: DependencyGraph, key: string): readonly string[] => graph.blocking.get(key) ?? EMPTY

/** How a card's blockers stand, for the footer chips. `total` counts links,
 *  `gating` counts the ones not yet done — so `total > 0 && gating === 0` is
 *  the "all clear, promote me" case worth calling out in green. */
export interface BlockerStand {
  total: number
  gating: number
}

export function blockerStand(graph: DependencyGraph, index: Map<string, KanbanTask>, key: string): BlockerStand {
  const parents = upstreamOf(graph, key)
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
export function focusSets(graph: DependencyGraph, key: string): { upstream: Set<string>; downstream: Set<string> } {
  return {
    upstream: new Set(upstreamOf(graph, key)),
    downstream: new Set(downstreamOf(graph, key))
  }
}
