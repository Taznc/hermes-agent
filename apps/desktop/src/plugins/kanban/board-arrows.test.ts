import { describe, expect, it } from 'vitest'

import {
  ARROW_GAP,
  arrowHead,
  arrowSides,
  BRACKET_MAX,
  type CardBox,
  chevrons,
  clampToLane,
  type Curve,
  FAN_LIFT,
  focusEdges,
  pointAt,
  REVEAL_MARGIN,
  revealDelta,
  routeArrows
} from './board-arrows'
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

describe('ribbon routing', () => {
  it('is one smooth cubic per edge whose ends match its curve', () => {
    const [arrow] = routeArrows(
      [['a', 'b']],
      new Map([
        ['a', box(0, 0)],
        ['b', box(400, 200)]
      ])
    )

    expect(arrow.d).toMatch(/^M [\d.]+ [\d.]+ C [\d.]+ [\d.]+, [\d.]+ [\d.]+, [\d.]+ [\d.]+$/)
    expect(pointAt(arrow.curve, 0)).toEqual(arrow.curve[0])
    expect(pointAt(arrow.curve, 1)).toEqual(arrow.curve[3])
    // Handles leave horizontally toward the child, and never overshoot it.
    expect(arrow.curve[1].x).toBeGreaterThan(arrow.curve[0].x)
    expect(arrow.curve[2].x).toBeLessThan(arrow.curve[3].x)
    expect(arrow.curve[1].x).toBeLessThanOrEqual(arrow.curve[0].x + 120)
  })

  it('ribbons that land on one card side arc through separate height bands', () => {
    const arrows = routeArrows(
      [
        ['a', 'c'],
        ['b', 'c']
      ],
      new Map([
        ['a', box(0, 0)],
        ['b', box(0, 100)],
        ['c', box(400, 50)]
      ])
    )

    const lifts = arrows.map(arrow => arrow.curve[2].y - arrow.curve[3].y)

    expect(lifts).toEqual([-FAN_LIFT / 2, FAN_LIFT / 2])
  })

  it("a same-lane bracket stays inside the strip's reserved right gutter", () => {
    const [arrow] = routeArrows(
      [['a', 'b']],
      new Map([
        ['a', box(0, 0)],
        ['b', box(0, 2000)]
      ])
    )

    let widest = 0

    for (let t = 0; t <= 1; t += 0.01) {
      widest = Math.max(widest, pointAt(arrow.curve, t).x)
    }

    expect(widest - 200).toBeLessThanOrEqual(BRACKET_MAX + ARROW_GAP)
  })
})

describe('arrowHead', () => {
  const pairs = (points: string) => points.split(' ').map(p => p.split(',').map(Number))

  it('puts the tip exactly on the curve end, pointing along it', () => {
    const curve: Curve = [
      { x: 0, y: 0 },
      { x: 50, y: 0 },
      { x: 150, y: 0 },
      { x: 200, y: 0 }
    ]

    const [tip, wing1, notch, wing2] = pairs(arrowHead(curve, 22))

    expect(tip).toEqual([200, 0])
    expect(wing1[0]).toBeCloseTo(178)
    expect(notch[0]).toBeLessThan(200)
    expect(notch[0]).toBeGreaterThan(wing1[0])
    expect(wing1[1]).toBe(-wing2[1])
  })

  it('shrinks on a short hop so the head never reaches back over the blocker', () => {
    const curve: Curve = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 0 },
      { x: 20, y: 0 }
    ]

    const xs = pairs(arrowHead(curve, 22)).map(([x]) => x)

    expect(Math.min(...xs)).toBeGreaterThanOrEqual(20 - Math.max(8, 20 * 0.6) - 0.1)
  })
})

describe('chevrons', () => {
  const straight = (length: number): Curve => [
    { x: 0, y: 0 },
    { x: length / 3, y: 0 },
    { x: (2 * length) / 3, y: 0 },
    { x: length, y: 0 }
  ]

  it('none on a short line; roughly one per 46px on a long one', () => {
    expect(chevrons(straight(60), 5)).toEqual([])
    expect(chevrons(straight(500), 5)).toHaveLength(Math.floor((500 - 40) / 46))
  })

  it('every chevron points the way the line runs (its apex is ahead of its arms)', () => {
    for (const points of chevrons(straight(300), 5)) {
      const [arm, apex] = points.split(' ').map(p => p.split(',').map(Number))

      expect(apex[0]).toBeGreaterThan(arm[0])
    }
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

describe('revealDelta', () => {
  const view = { end: 500, start: 100 }

  const shown = (item: { end: number; start: number }) => {
    const d = revealDelta(item, view)

    return { end: item.end - d, start: item.start - d }
  }

  it('does not scroll a card that is already fully in view', () => {
    expect(revealDelta({ end: 300, start: 200 }, view)).toBe(0)
  })

  it('brings a card below or above the fold fully into view, with the margin', () => {
    for (const item of [
      { end: 560, start: 440 }, // straddles the bottom edge
      { end: 900, start: 780 }, // fully below
      { end: 160, start: 40 }, // straddles the top edge
      { end: 20, start: -100 } // fully above
    ]) {
      const after = shown(item)

      expect(after.start).toBeGreaterThanOrEqual(view.start + REVEAL_MARGIN)
      expect(after.end).toBeLessThanOrEqual(view.end - REVEAL_MARGIN)
    }
  })

  it('shows the leading edge of a card taller than the view', () => {
    expect(shown({ end: 1400, start: 700 }).start).toBe(view.start + REVEAL_MARGIN)
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
