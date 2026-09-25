import { describe, expect, it } from 'vitest'

import { ARROW_GAP, arrowSides, type CardBox, clampToLane, focusEdges, routeArrows } from './board-arrows'
import { buildGraph, chainSets, focusSets } from './deps'
import type { KanbanBoard } from './types'

const box = (left: number, top: number, width = 200, height = 60): CardBox => ({
  bottom: top + height,
  left,
  offscreen: false,
  right: left + width,
  top
})

/** Parse `M x1 y1 C … x2 y2` into its endpoints. */
function ends(d: string) {
  const nums = d.match(/-?\d+(\.\d+)?/g)!.map(Number)

  return { x1: nums[0], y1: nums[1], x2: nums[nums.length - 2], y2: nums[nums.length - 1] }
}

describe('arrowSides', () => {
  it('leaves toward the child and enters from the side facing the parent', () => {
    expect(arrowSides(box(0, 0), box(300, 0))).toEqual(['right', 'left'])
    expect(arrowSides(box(300, 0), box(0, 0))).toEqual(['left', 'right'])
  })

  it('brackets a same-lane pair out one side instead of cutting through the lane', () => {
    const [out, into] = arrowSides(box(0, 0), box(0, 200))

    expect(out).toBe(into)
  })
})

describe('routeArrows', () => {
  it('every arrow starts on its blocker and its tip sits just outside the blocked card', () => {
    const boxes = new Map([
      ['a', box(0, 0)],
      ['b', box(300, 100)],
      ['c', box(300, 400)]
    ])

    const arrows = routeArrows(
      [
        ['a', 'b'],
        ['c', 'a'],
        ['b', 'c']
      ],
      boxes
    )

    expect(arrows).toHaveLength(3)

    for (const arrow of arrows) {
      const from = boxes.get(arrow.parent)!
      const to = boxes.get(arrow.child)!
      const { x1, x2, y1, y2 } = ends(arrow.d)

      expect([from.left, from.right]).toContain(x1)
      expect(y1).toBeGreaterThan(from.top)
      expect(y1).toBeLessThan(from.bottom)
      expect([to.left - ARROW_GAP, to.right + ARROW_GAP]).toContain(x2)
      expect(y2).toBeGreaterThan(to.top)
      expect(y2).toBeLessThan(to.bottom)
    }
  })

  it('skips an edge whose card is not rendered rather than pointing at nothing', () => {
    const arrows = routeArrows(
      [
        ['a', 'b'],
        ['a', 'gone']
      ],
      new Map([
        ['a', box(0, 0)],
        ['b', box(300, 0)]
      ])
    )

    expect(arrows.map(arrow => arrow.child)).toEqual(['b'])
  })

  it('fans several arrows on one card side instead of stacking them on one point', () => {
    const arrows = routeArrows(
      [
        ['a', 'b'],
        ['a', 'c'],
        ['a', 'd']
      ],
      new Map([
        ['a', box(0, 0)],
        ['b', box(300, 0)],
        ['c', box(300, 100)],
        ['d', box(300, 200)]
      ])
    )

    const starts = arrows.map(arrow => ends(arrow.d).y1)

    expect(new Set(starts).size).toBe(3)
    // Ordered by the far end's height, so the fan never crosses itself.
    expect(starts).toEqual([...starts].sort((x, y) => x - y))
  })

  it('flags an arrow whose end is scrolled out of its lane', () => {
    const hidden = { ...box(300, 0), offscreen: true }

    const [arrow] = routeArrows(
      [['a', 'b']],
      new Map([
        ['a', box(0, 0)],
        ['b', hidden]
      ])
    )

    expect(arrow.offscreen).toBe(true)
  })
})

describe('clampToLane', () => {
  const lane = { bottom: 500, top: 100 }

  it('leaves a visible card alone', () => {
    expect(clampToLane({ bottom: 300, top: 200 }, lane)).toEqual({ bottom: 300, offscreen: false, top: 200 })
  })

  it('pins a card scrolled out of its lane to the edge it left by', () => {
    expect(clampToLane({ bottom: 80, top: 20 }, lane)).toEqual({ bottom: 100, offscreen: true, top: 100 })
    expect(clampToLane({ bottom: 700, top: 600 }, lane)).toEqual({ bottom: 500, offscreen: true, top: 500 })
  })
})

describe('focusEdges', () => {
  // gp → p → f → c, plus a sibling blocker s → c the focus never touches.
  const board = {
    columns: [],
    link_edges: [
      ['gp', 'p'],
      ['p', 'f'],
      ['f', 'c'],
      ['s', 'c']
    ]
  } as unknown as KanbanBoard

  const graph = buildGraph(board)

  it('direct mode draws a repeated link row once (no duplicate React keys)', () => {
    const dup = buildGraph({
      columns: [],
      link_edges: [
        ['p', 'f'],
        ['p', 'f'],
        ['f', 'c']
      ]
    } as unknown as KanbanBoard)

    const edges = focusEdges(dup, 'f', 'direct', { downstream: new Set(['c']), upstream: new Set(['p']) })

    expect(edges).toEqual([
      ['p', 'f'],
      ['f', 'c']
    ])
  })

  it('every drawn edge joins two cards the trace keeps lit', () => {
    for (const [depth, sets] of [
      ['direct', focusSets(graph, 'f')],
      ['chain', chainSets(graph, 'f')]
    ] as const) {
      const lit = new Set(['f', ...sets.upstream, ...sets.downstream])

      for (const [parent, child] of focusEdges(graph, 'f', depth, sets)) {
        expect(lit.has(parent) && lit.has(child)).toBe(true)
      }
    }
  })

  it('chain draws the transitive links that direct leaves out', () => {
    const direct = focusEdges(graph, 'f', 'direct', focusSets(graph, 'f'))
    const chain = focusEdges(graph, 'f', 'chain', chainSets(graph, 'f'))

    expect(direct).not.toContainEqual(['gp', 'p'])
    expect(chain).toContainEqual(['gp', 'p'])
    // The sibling blocker waits on nothing the focus reaches upstream.
    expect(chain).not.toContainEqual(['s', 'c'])
  })
})
