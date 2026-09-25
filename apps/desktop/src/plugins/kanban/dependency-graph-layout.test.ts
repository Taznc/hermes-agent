/**
 * Invariants of the dependency-graph layout: ranks are signed BFS distance
 * from the focused card, a cycle terminates, only member-to-member edges are
 * drawn, and no two nodes in a column share a row. Pure module — fixtures are
 * hand-built adjacency maps rather than full boards.
 */

import { describe, expect, it } from 'vitest'

import {
  canvasSize,
  COL_GAP,
  edgePath,
  layoutChain,
  NODE_H,
  NODE_W,
  nodePosition,
  ROW_GAP
} from './dependency-graph-layout'
import { type DependencyGraph } from './deps'

function graphOf(edges: Array<[string, string]>): DependencyGraph {
  const blockedBy = new Map<string, string[]>()
  const blocking = new Map<string, string[]>()

  for (const [parent, child] of edges) {
    blockedBy.set(child, [...(blockedBy.get(child) ?? []), parent])
    blocking.set(parent, [...(blocking.get(parent) ?? []), child])
  }

  return { blockedBy, blocking }
}

const rankOf = (layout: ReturnType<typeof layoutChain>, key: string) => layout.nodes.find(n => n.key === key)?.rank

describe('layoutChain', () => {
  it('puts the focused card at rank 0, blockers negative, dependants positive', () => {
    const layout = layoutChain(graphOf([['a', 'b'], ['b', 'c']]), 'b')

    expect(rankOf(layout, 'a')).toBe(-1)
    expect(rankOf(layout, 'b')).toBe(0)
    expect(rankOf(layout, 'c')).toBe(1)
    expect(layout.ranks).toBe(3)
  })

  it('ranks by BFS distance, so a grandparent sits two columns left', () => {
    const layout = layoutChain(graphOf([['gp', 'p'], ['p', 'f'], ['f', 'c'], ['c', 'gc']]), 'f')

    expect(rankOf(layout, 'gp')).toBe(-2)
    expect(rankOf(layout, 'gc')).toBe(2)
  })

  it('terminates on a cycle and keeps the first rank assignment', () => {
    const layout = layoutChain(graphOf([['a', 'b'], ['b', 'c'], ['c', 'a']]), 'a')
    const keys = layout.nodes.map(n => n.key).sort()

    expect(keys).toEqual(['a', 'b', 'c'])
    expect(rankOf(layout, 'a')).toBe(0)
    // Upstream is walked first: c blocks a, b blocks c — both land left.
    expect(rankOf(layout, 'c')).toBe(-1)
    expect(rankOf(layout, 'b')).toBe(-2)
  })

  it('draws only edges between member nodes', () => {
    const layout = layoutChain(graphOf([['a', 'b'], ['b', 'c'], ['x', 'y']]), 'b')
    const members = new Set(layout.nodes.map(n => n.key))

    expect(layout.edges.length).toBeGreaterThan(0)

    for (const [parent, child] of layout.edges) {
      expect(members.has(parent)).toBe(true)
      expect(members.has(child)).toBe(true)
    }

    expect(members.has('x')).toBe(false)
  })

  it('never seats two nodes of the same rank in the same row', () => {
    const layout = layoutChain(
      graphOf([['a', 'f'], ['b', 'f'], ['c', 'f'], ['f', 'x'], ['f', 'y']]),
      'f'
    )

    const seen = new Set<string>()

    for (const node of layout.nodes) {
      const slot = `${node.rank}:${node.row}`

      expect(seen.has(slot)).toBe(false)
      seen.add(slot)
    }

    expect(layout.rows).toBe(3)
  })

  it('orders a column by the barycenter of its placed neighbours', () => {
    // Two dependants d1, d2 of the focus; each has its own grandchild. The
    // grandchildren should line up with their parents rather than swap.
    const layout = layoutChain(graphOf([['f', 'd1'], ['f', 'd2'], ['d1', 'g1'], ['d2', 'g2']]), 'f')
    const row = (key: string) => layout.nodes.find(n => n.key === key)!.row

    expect(row('g1')).toBe(row('d1'))
    expect(row('g2')).toBe(row('d2'))
  })

  it('is a single node with no edges when the card is unlinked', () => {
    const layout = layoutChain(graphOf([]), 'lonely')

    expect(layout.nodes).toEqual([{ key: 'lonely', rank: 0, row: 0, col: 0 }])
    expect(layout.edges).toEqual([])
  })
})

describe('geometry', () => {
  it('spaces columns by NODE_W + COL_GAP and rows by NODE_H + ROW_GAP', () => {
    const origin = nodePosition({ key: 'o', rank: 0, row: 0, col: 0 })
    const right = nodePosition({ key: 'r', rank: 1, row: 0, col: 1 })
    const below = nodePosition({ key: 'b', rank: 0, row: 1, col: 0 })

    expect(right.x - origin.x).toBe(NODE_W + COL_GAP)
    expect(below.y - origin.y).toBe(NODE_H + ROW_GAP)
  })

  it('sizes the canvas to contain every node', () => {
    const layout = layoutChain(graphOf([['a', 'b'], ['b', 'c'], ['d', 'b']]), 'b')
    const { height, width } = canvasSize(layout)

    for (const node of layout.nodes) {
      const at = nodePosition(node)

      expect(at.x + NODE_W).toBeLessThanOrEqual(width)
      expect(at.y + NODE_H).toBeLessThanOrEqual(height)
    }
  })

  it('draws an edge from the parent right edge to the child left edge', () => {
    const from = { x: 0, y: 0 }
    const to = { x: 400, y: 100 }
    const d = edgePath(from, to)

    expect(d.startsWith(`M ${NODE_W} ${NODE_H / 2}`)).toBe(true)
    expect(d.endsWith(`400 ${100 + NODE_H / 2}`)).toBe(true)
  })
})
