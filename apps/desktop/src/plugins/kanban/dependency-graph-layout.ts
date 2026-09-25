/**
 * Layout for the dependency-graph overlay: a layered (Sugiyama-lite) drawing
 * of the focused card's full chain. Pure — no React, no DOM — so the dialog
 * and its tests share one source of truth for where every node sits.
 *
 * Columns are BFS distance from the focused card, signed: blockers to the
 * left (negative), the focused card at 0, dependants to the right (positive).
 * Arrows therefore always run left→right, parent→child, matching the `deps.ts`
 * vocabulary (an edge means "the parent blocks the child").
 */

import { chainEdges, chainSets, type DependencyGraph, downstreamOf, upstreamOf } from './deps'

export const NODE_W = 208
export const NODE_H = 64
export const COL_GAP = 72
export const ROW_GAP = 16
/** Canvas inset so the outermost arrowheads and rings are never clipped. */
export const CANVAS_PAD = 16

export interface GraphNode {
  key: string
  /** Signed BFS distance from the focused card (blockers < 0 < dependants). */
  rank: number
  /** 0-based slot within the rank's column. */
  row: number
  /** `rank` shifted so the leftmost column is 0 — the drawing coordinate. */
  col: number
}

export interface GraphLayout {
  nodes: GraphNode[]
  edges: Array<[parent: string, child: string]>
  /** Number of distinct ranks (columns) — the canvas is this many nodes wide. */
  ranks: number
  /** Deepest column, for the canvas height. */
  rows: number
}

/** Signed rank per member key. The focused card is 0; a key first reached
 *  walking blockers is negative, walking dependants positive. A key reachable
 *  both ways (a cycle) keeps whichever assignment came first — upstream is
 *  walked first, so it lands left. */
function assignRanks(graph: DependencyGraph, focusedKey: string): Map<string, number> {
  const rank = new Map<string, number>([[focusedKey, 0]])

  const walk = (next: (graph: DependencyGraph, key: string) => readonly string[], step: number) => {
    // Per-walk visited set, separate from `rank`: on cyclic (legacy) data a
    // node the upstream walk already ranked must still be traversed on the
    // downstream walk, or anything reachable only through it is never placed.
    const queue = [focusedKey]
    const visited = new Set<string>(queue)

    for (let head = 0; head < queue.length; head += 1) {
      const key = queue[head]
      const depth = rank.get(key)! + step

      for (const neighbour of next(graph, key)) {
        if (!visited.has(neighbour)) {
          visited.add(neighbour)

          if (!rank.has(neighbour)) {
            rank.set(neighbour, depth)
          }

          queue.push(neighbour)
        }
      }
    }
  }

  walk(upstreamOf, -1)
  walk(downstreamOf, 1)

  return rank
}

const mean = (values: number[]): number => values.reduce((sum, value) => sum + value, 0) / values.length

/** Order one column by the barycenter of each node's already-placed
 *  neighbours in the column nearer the focus (`previous`). Nodes with no
 *  placed neighbour sort after those that have, then everything ties on key
 *  so the result is deterministic. */
function orderColumn(
  keys: string[],
  previous: Map<string, number>,
  neighbours: (key: string) => readonly string[]
): string[] {
  const score = new Map<string, number>()

  for (const key of keys) {
    const rows = neighbours(key)
      .map(neighbour => previous.get(neighbour))
      .filter((row): row is number => row !== undefined)

    score.set(key, rows.length > 0 ? mean(rows) : Number.POSITIVE_INFINITY)
  }

  return [...keys].sort((a, b) => score.get(a)! - score.get(b)! || (a < b ? -1 : a > b ? 1 : 0))
}

export function layoutChain(graph: DependencyGraph, focusedKey: string): GraphLayout {
  const { downstream, upstream } = chainSets(graph, focusedKey)
  const members = new Set<string>([focusedKey, ...upstream, ...downstream])
  const rank = assignRanks(graph, focusedKey)

  const columns = new Map<number, string[]>()

  for (const key of members) {
    const r = rank.get(key)!
    const column = columns.get(r)

    column ? column.push(key) : columns.set(r, [key])
  }

  const ranks = [...columns.keys()].sort((a, b) => a - b)
  const minRank = ranks[0] ?? 0
  const rowOf = new Map<string, number>([[focusedKey, 0]])
  const nodes: GraphNode[] = [{ key: focusedKey, rank: 0, row: 0, col: 0 - minRank }]

  // Place outward from the focus in both directions so every column's
  // barycenter reads rows the previous (nearer) column has already fixed.
  const place = (r: number, neighbours: (key: string) => readonly string[]) => {
    const keys = columns.get(r)

    if (!keys) {
      return
    }

    orderColumn(keys, rowOf, neighbours).forEach((key, row) => {
      rowOf.set(key, row)
      nodes.push({ key, rank: r, row, col: r - minRank })
    })
  }

  for (let r = -1; r >= minRank; r -= 1) {
    // A blocker's placed neighbours are the cards it blocks (one column right).
    place(r, key => downstreamOf(graph, key))
  }

  for (let r = 1; r <= (ranks[ranks.length - 1] ?? 0); r += 1) {
    // A dependant's placed neighbours are its blockers (one column left).
    place(r, key => upstreamOf(graph, key))
  }

  const rows = Math.max(0, ...[...columns.values()].map(column => column.length))

  return { nodes, edges: chainEdges(graph, members), ranks: ranks.length, rows }
}

/** Top-left corner of a node's box on the canvas. */
export function nodePosition(node: GraphNode): { x: number; y: number } {
  return {
    x: CANVAS_PAD + node.col * (NODE_W + COL_GAP),
    y: CANVAS_PAD + node.row * (NODE_H + ROW_GAP)
  }
}

export function canvasSize(layout: GraphLayout): { height: number; width: number } {
  return {
    width: CANVAS_PAD * 2 + layout.ranks * NODE_W + Math.max(0, layout.ranks - 1) * COL_GAP,
    height: CANVAS_PAD * 2 + layout.rows * NODE_H + Math.max(0, layout.rows - 1) * ROW_GAP
  }
}

/** SVG path for one arrow: leaves the parent's right edge, enters the child's
 *  left edge, as a cubic bezier so parallel edges in a column fan visibly. */
export function edgePath(from: { x: number; y: number }, to: { x: number; y: number }): string {
  const x1 = from.x + NODE_W
  const y1 = from.y + NODE_H / 2
  const x2 = to.x
  const y2 = to.y + NODE_H / 2
  // A transitive shortcut can put parent and child in the same column
  // (x2 <= x1); a fixed outward bend keeps that arrow from folding back
  // through both boxes.
  const bend = x2 > x1 ? Math.max(24, (x2 - x1) / 2) : NODE_W / 2

  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
}
