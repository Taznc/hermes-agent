/**
 * The slice of the Kanban data model focus mode and All Boards read, declared
 * here instead of importing the bundled plugin's `types.ts` so this fork
 * module never forces an edit inside upstream's plugin directory.
 *
 * Every field is structurally compatible with upstream's `KanbanTask` /
 * `KanbanBoard` (a superset object type-checks against these), plus the three
 * fields only the fork's backend (`plugins/fork-kanban`) adds:
 *  - `board` / `board_name` on a task in the merged All Boards payload;
 *  - `link_edges` on a board payload (bare pairs for one board, tagged objects
 *    for All Boards).
 */

export interface KanbanTask {
  id: string
  title: string
  status: string
  assignee?: null | string
  priority?: number
  link_counts?: { children: number; parents: number }
  /** All Boards only: the slug of the board this card lives on. */
  board?: null | string
  /** All Boards only: that board's display name. */
  board_name?: null | string
}

export interface KanbanColumn {
  name: string
  tasks: KanbanTask[]
}

export type LinkEdge = [parent: string, child: string] | { board?: null | string; child: string; parent: string }

export interface KanbanBoard {
  columns: KanbanColumn[]
  /** Fork backend only. Absent on upstream's `/board` payload. */
  link_edges?: LinkEdge[]
  /** Upstream payload fields the fork never reads (kept so a full payload
   *  literal type-checks). */
  assignees?: string[]
  latest_event_id?: number
  now?: number
  tenants?: string[]
}

/** One linked task resolved against the board index (`deps.resolveLinks`). */
export interface ResolvedLink {
  id: string
  title: string
  status: string
  assignee?: null | string
  missing: boolean
}

/** Dev's wishlist lanes. `next` has no roadmap lanes (C03 `roadmap-sync`
 *  covers them out of tree), but a card that carries one of these statuses
 *  must still never read as gating anything, so the constant stays. */
export const ROADMAP_LANES = ['idea', 'roadmap'] as const
