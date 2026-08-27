/**
 * Focused tests for the dependency-chain helpers in ./deps.
 *
 * This is a pure-logic module — no React, no DOM, no network — so every test
 * calls the exported functions directly against small board fixtures shaped
 * like the real `KanbanBoard` contract.
 *
 * The invariant this file exists to protect: an edge is `[parent_id, child_id]`
 * and means the PARENT BLOCKS THE CHILD. Three UI surfaces (drawer rows, card
 * chips, board focus mode) read the same graph, so an inversion here would
 * surface as three unrelated-looking visual bugs. The direction is asserted
 * explicitly, from both sides, in `buildGraph — edge direction`.
 */

import { describe, expect, it } from 'vitest'

import {
  blockerStand,
  buildGraph,
  type DependencyGraph,
  downstreamOf,
  focusSets,
  GATING_CLEARED,
  indexBoard,
  isGating,
  partitionBlockers,
  resolveLinks,
  upstreamOf
} from './deps'
import type { KanbanBoard, KanbanColumn, KanbanTask, ResolvedLink } from './types'

/** The backend's BOARD_COLUMNS, in order. Fixtures always carry all ten so the
 *  helpers are exercised against empty columns as well as populated ones. */
const BOARD_COLUMNS = [
  'triage',
  'todo',
  'scheduled',
  'ready',
  'running',
  'blocked',
  'on_hold',
  'review',
  'done',
  'archived'
] as const

/** Every status that is NOT terminal — each one must keep gating its children. */
const STILL_GATING = ['todo', 'ready', 'running', 'blocked', 'review', 'on_hold', 'triage', 'scheduled'] as const

type TaskSpec = [id: string, status: string]

const task = (id: string, status: string): KanbanTask => ({
  id,
  title: `Task ${id}`,
  status,
  assignee: `owner-${id}`
})

/**
 * Compact board fixture: a list of `[id, status]` pairs plus the raw edge rows.
 *
 * `edges` is deliberately `unknown[]` — the malformed-payload tests need to
 * hand `buildGraph` rows that no backend *should* send but a stale/buggy one
 * might (null, short arrays, empty ids). Casting once here at the fixture
 * boundary keeps every test body type-clean. Omit `edges` entirely to model an
 * older backend that sends no `link_edges` key at all.
 */
function makeBoard(specs: TaskSpec[], edges?: unknown[]): KanbanBoard {
  const columns: KanbanColumn[] = BOARD_COLUMNS.map(name => ({ name, tasks: [] }))
  const byName = new Map<string, KanbanColumn>(columns.map(column => [column.name, column]))

  for (const [id, status] of specs) {
    let column = byName.get(status)

    if (!column) {
      column = { name: status, tasks: [] }
      byName.set(status, column)
      columns.push(column)
    }

    column.tasks.push(task(id, status))
  }

  const board: KanbanBoard = {
    columns,
    tenants: [],
    assignees: [],
    latest_event_id: 1,
    now: 1_700_000_000
  }

  if (edges !== undefined) {
    board.link_edges = edges as KanbanBoard['link_edges']
  }

  return board
}

/** Board + its derived index and graph, since most tests want all three. */
function scene(specs: TaskSpec[], edges?: unknown[]) {
  const board = makeBoard(specs, edges)

  return { board, index: indexBoard(board), graph: buildGraph(board) }
}

/** `upstreamOf` yields `readonly string[]`; `resolveLinks` asks for `string[]`.
 *  Copy at the seam so the composition type-checks (see the note in the suite
 *  summary — the two signatures do not compose directly). */
const rows = (graph: DependencyGraph, id: string, index: Map<string, KanbanTask>): ResolvedLink[] =>
  resolveLinks([...upstreamOf(graph, id)], index)

describe('gating status', () => {
  it('clears the gate on exactly done and archived', () => {
    expect([...GATING_CLEARED].sort()).toEqual(['archived', 'done'])
  })

  it('does not gate on done — the dispatcher promotion rule', () => {
    expect(isGating('done')).toBe(false)
  })

  it('does not gate on archived — it would otherwise gate forever', () => {
    expect(isGating('archived')).toBe(false)
  })

  it.each(STILL_GATING)('still gates on %s', status => {
    expect(isGating(status)).toBe(true)
  })

  it('gates on an unrecognised status the backend may add later', () => {
    expect(isGating('some_new_column')).toBe(true)
  })

  it("gates on the 'unknown' status resolveLinks gives a missing task", () => {
    expect(isGating('unknown')).toBe(true)
  })
})

describe('indexBoard', () => {
  it('flattens every column into one id → task index', () => {
    const index = indexBoard(makeBoard([['a', 'todo'], ['b', 'running'], ['c', 'done']]))

    expect([...index.keys()].sort()).toEqual(['a', 'b', 'c'])
  })

  it('keeps the whole task, not just its id', () => {
    const index = indexBoard(makeBoard([['a', 'review']]))

    expect(index.get('a')).toMatchObject({ id: 'a', title: 'Task a', status: 'review', assignee: 'owner-a' })
  })

  it('returns an empty index for an undefined board', () => {
    expect(indexBoard(undefined).size).toBe(0)
  })

  it('returns an empty index for a board whose columns are all empty', () => {
    expect(indexBoard(makeBoard([])).size).toBe(0)
  })

  it('indexes tasks that sit in different columns', () => {
    const index = indexBoard(makeBoard([['a', 'todo'], ['b', 'archived']]))

    expect(index.get('a')?.status).toBe('todo')
    expect(index.get('b')?.status).toBe('archived')
  })
})

describe('resolveLinks', () => {
  const index = indexBoard(makeBoard([['a', 'done'], ['b', 'running']]))

  it('resolves a known id to its own identity', () => {
    expect(resolveLinks(['a'], index)).toEqual([
      { id: 'a', title: 'Task a', status: 'done', assignee: 'owner-a', missing: false }
    ])
  })

  it('flags an id the board does not have as missing', () => {
    const [row] = resolveLinks(['ghost'], index)

    expect(row.missing).toBe(true)
  })

  it('gives a missing id an empty title so the row renders id-only', () => {
    const [row] = resolveLinks(['ghost'], index)

    expect(row.title).toBe('')
  })

  it("gives a missing id the 'unknown' status and a null assignee", () => {
    const [row] = resolveLinks(['ghost'], index)

    expect(row.status).toBe('unknown')
    expect(row.assignee).toBeNull()
  })

  it('still carries the id of a missing link so the user can cut it', () => {
    expect(resolveLinks(['ghost'], index)[0].id).toBe('ghost')
  })

  it('preserves input order and mixes present with missing', () => {
    expect(resolveLinks(['b', 'ghost', 'a'], index).map(row => [row.id, row.missing])).toEqual([
      ['b', false],
      ['ghost', true],
      ['a', false]
    ])
  })

  it('returns an empty list for no ids', () => {
    expect(resolveLinks([], index)).toEqual([])
  })

  it('resolves every id against an empty index as missing', () => {
    expect(resolveLinks(['a', 'b'], new Map()).every(row => row.missing)).toBe(true)
  })
})

describe('partitionBlockers', () => {
  const link = (id: string, status: string, missing = false): ResolvedLink => ({
    id,
    title: `Task ${id}`,
    status,
    assignee: null,
    missing
  })

  it('sorts a done blocker into satisfied', () => {
    const { gating, satisfied } = partitionBlockers([link('a', 'done')])

    expect(satisfied.map(row => row.id)).toEqual(['a'])
    expect(gating).toEqual([])
  })

  it('sorts an archived blocker into satisfied', () => {
    expect(partitionBlockers([link('a', 'archived')]).satisfied.map(row => row.id)).toEqual(['a'])
  })

  it('sorts a running blocker into gating', () => {
    const { gating, satisfied } = partitionBlockers([link('a', 'running')])

    expect(gating.map(row => row.id)).toEqual(['a'])
    expect(satisfied).toEqual([])
  })

  it('counts a MISSING blocker as gating, never satisfied', () => {
    const { gating, satisfied } = partitionBlockers([link('ghost', 'unknown', true)])

    expect(gating.map(row => row.id)).toEqual(['ghost'])
    expect(satisfied).toEqual([])
  })

  it('counts a missing blocker as gating even if its status reads terminal', () => {
    // Conservative on purpose: the backend link still exists and may still be
    // enforced, so a dangling row must never be treated as cleared.
    const { gating, satisfied } = partitionBlockers([link('ghost', 'done', true)])

    expect(gating.map(row => row.id)).toEqual(['ghost'])
    expect(satisfied).toEqual([])
  })

  it('splits a mixed list into both buckets, preserving order', () => {
    const { gating, satisfied } = partitionBlockers([
      link('a', 'done'),
      link('b', 'running'),
      link('c', 'archived'),
      link('ghost', 'unknown', true)
    ])

    expect(gating.map(row => row.id)).toEqual(['b', 'ghost'])
    expect(satisfied.map(row => row.id)).toEqual(['a', 'c'])
  })

  it('returns two empty buckets for no links', () => {
    expect(partitionBlockers([])).toEqual({ gating: [], satisfied: [] })
  })
})

describe('buildGraph — edge direction', () => {
  // The single most consequential assertion in this file. An edge is
  // [parent, child] and the parent BLOCKS the child.
  const { graph } = scene([['parent', 'running'], ['child', 'todo']], [['parent', 'child']])

  it('puts the PARENT in blockedBy of the CHILD', () => {
    expect(graph.blockedBy.get('child')).toEqual(['parent'])
  })

  it('puts the CHILD in blocking of the PARENT', () => {
    expect(graph.blocking.get('parent')).toEqual(['child'])
  })

  it('does NOT invert: the child never appears in blockedBy of the parent', () => {
    expect(graph.blockedBy.get('parent')).toBeUndefined()
  })

  it('does NOT invert: the parent never appears in blocking of the child', () => {
    expect(graph.blocking.get('child')).toBeUndefined()
  })

  it('reads the same way through the accessors: the child is blocked by the parent', () => {
    expect(upstreamOf(graph, 'child')).toEqual(['parent'])
    expect(downstreamOf(graph, 'child')).toEqual([])
  })

  it('reads the same way through the accessors: the parent blocks the child', () => {
    expect(downstreamOf(graph, 'parent')).toEqual(['child'])
    expect(upstreamOf(graph, 'parent')).toEqual([])
  })

  it('accumulates several parents onto one child (fan-in)', () => {
    const { graph: fanIn } = scene(
      [['p1', 'todo'], ['p2', 'todo'], ['kid', 'todo']],
      [['p1', 'kid'], ['p2', 'kid']]
    )

    expect(fanIn.blockedBy.get('kid')).toEqual(['p1', 'p2'])
    expect(fanIn.blocking.get('p1')).toEqual(['kid'])
    expect(fanIn.blocking.get('p2')).toEqual(['kid'])
  })

  it('accumulates several children onto one parent (fan-out)', () => {
    const { graph: fanOut } = scene(
      [['root', 'todo'], ['c1', 'todo'], ['c2', 'todo']],
      [['root', 'c1'], ['root', 'c2']]
    )

    expect(fanOut.blocking.get('root')).toEqual(['c1', 'c2'])
    expect(fanOut.blockedBy.get('c1')).toEqual(['root'])
    expect(fanOut.blockedBy.get('c2')).toEqual(['root'])
  })

  it('builds edges for ids the board index does not contain', () => {
    // The graph is built from the edge list alone; membership is resolved later.
    const { graph: dangling } = scene([['child', 'todo']], [['ghost', 'child']])

    expect(dangling.blockedBy.get('child')).toEqual(['ghost'])
    expect(dangling.blocking.get('ghost')).toEqual(['child'])
  })
})

describe('buildGraph — absent and malformed payloads', () => {
  it('returns empty maps for an undefined board', () => {
    const graph = buildGraph(undefined)

    expect(graph.blockedBy.size).toBe(0)
    expect(graph.blocking.size).toBe(0)
  })

  it('returns empty maps when link_edges is absent (older backend)', () => {
    const graph = buildGraph(makeBoard([['a', 'todo']]))

    expect(graph.blockedBy.size).toBe(0)
    expect(graph.blocking.size).toBe(0)
  })

  it('returns empty maps for an empty link_edges array', () => {
    const graph = buildGraph(makeBoard([['a', 'todo']], []))

    expect(graph.blockedBy.size).toBe(0)
    expect(graph.blocking.size).toBe(0)
  })

  it('does not throw on a payload made entirely of junk rows', () => {
    expect(() => buildGraph(makeBoard([], [null, undefined, [], ['solo'], 'nope', 42, {}]))).not.toThrow()
  })

  it('skips a null row', () => {
    expect(buildGraph(makeBoard([], [null])).blockedBy.size).toBe(0)
  })

  it('skips a one-element row', () => {
    expect(buildGraph(makeBoard([], [['solo']])).blockedBy.size).toBe(0)
  })

  it('skips a row that is not an array at all', () => {
    expect(buildGraph(makeBoard([], ['p,c'])).blockedBy.size).toBe(0)
  })

  it('skips an edge whose parent id is an empty string', () => {
    expect(buildGraph(makeBoard([], [['', 'child']])).blockedBy.size).toBe(0)
  })

  it('skips an edge whose child id is an empty string', () => {
    expect(buildGraph(makeBoard([], [['parent', '']])).blocking.size).toBe(0)
  })

  it('still processes valid edges sitting alongside malformed ones', () => {
    const graph = buildGraph(
      makeBoard(
        [['p', 'todo'], ['c', 'todo']],
        [null, ['solo'], ['', 'x'], ['y', ''], 'junk', ['p', 'c'], undefined]
      )
    )

    expect(graph.blockedBy.get('c')).toEqual(['p'])
    expect(graph.blocking.get('p')).toEqual(['c'])
    expect(graph.blockedBy.size).toBe(1)
    expect(graph.blocking.size).toBe(1)
  })

  it('tolerates an over-long row by reading its first two ids', () => {
    const graph = buildGraph(makeBoard([], [['p', 'c', 'extra']]))

    expect(graph.blockedBy.get('c')).toEqual(['p'])
  })
})

describe('upstreamOf / downstreamOf', () => {
  const { graph } = scene([['p', 'todo'], ['c', 'todo']], [['p', 'c']])

  it('returns an empty array — never undefined — for an unknown id upstream', () => {
    const found = upstreamOf(graph, 'nobody')

    expect(found).not.toBeUndefined()
    expect(Array.isArray(found)).toBe(true)
    expect(found).toEqual([])
  })

  it('returns an empty array — never undefined — for an unknown id downstream', () => {
    const found = downstreamOf(graph, 'nobody')

    expect(found).not.toBeUndefined()
    expect(Array.isArray(found)).toBe(true)
    expect(found).toEqual([])
  })

  it('hands back a STABLE empty array, so a memo/identity check never churns', () => {
    expect(upstreamOf(graph, 'nobody')).toBe(upstreamOf(graph, 'nobody-else'))
    expect(downstreamOf(graph, 'nobody')).toBe(upstreamOf(graph, 'nobody'))
  })

  it('returns an empty array for a known task with no links either way', () => {
    const { graph: lonely } = scene([['solo', 'todo']], [])

    expect(upstreamOf(lonely, 'solo')).toEqual([])
    expect(downstreamOf(lonely, 'solo')).toEqual([])
  })

  it('returns empty arrays against a graph built from a board with no link_edges', () => {
    const graphless = buildGraph(makeBoard([['a', 'todo']]))

    expect(upstreamOf(graphless, 'a')).toEqual([])
    expect(downstreamOf(graphless, 'a')).toEqual([])
  })

  it('returns the blockers of a task that has them', () => {
    expect(upstreamOf(graph, 'c')).toEqual(['p'])
  })

  it('returns the dependants of a task that has them', () => {
    expect(downstreamOf(graph, 'p')).toEqual(['c'])
  })
})

describe('blockerStand', () => {
  it('reports the total and how many of them still gate', () => {
    const { graph, index } = scene(
      [['done1', 'done'], ['done2', 'done'], ['busy', 'running'], ['kid', 'todo']],
      [['done1', 'kid'], ['done2', 'kid'], ['busy', 'kid']]
    )

    expect(blockerStand(graph, index, 'kid')).toEqual({ total: 3, gating: 1 })
  })

  it('counts every blocker as gating when none are terminal', () => {
    const { graph, index } = scene(
      [['a', 'running'], ['b', 'review'], ['kid', 'todo']],
      [['a', 'kid'], ['b', 'kid']]
    )

    expect(blockerStand(graph, index, 'kid')).toEqual({ total: 2, gating: 2 })
  })

  it('reports ALL CLEAR as total > 0 with gating 0 — the green promote chip', () => {
    const { graph, index } = scene(
      [['a', 'done'], ['b', 'archived'], ['kid', 'todo']],
      [['a', 'kid'], ['b', 'kid']]
    )

    const stand = blockerStand(graph, index, 'kid')

    expect(stand).toEqual({ total: 2, gating: 0 })
    expect(stand.total > 0 && stand.gating === 0).toBe(true)
  })

  it('reports NO DEPENDENCIES as total 0 — rendered differently from all-clear', () => {
    const { graph, index } = scene([['kid', 'todo']], [])
    const stand = blockerStand(graph, index, 'kid')

    expect(stand).toEqual({ total: 0, gating: 0 })
    expect(stand.total > 0 && stand.gating === 0).toBe(false)
  })

  // The board's green "blockers clear" chip is NOT driven by the raw predicate
  // alone: on a real board most finished cards also satisfy `total > 0 &&
  // gating === 0`, so the UI additionally requires the card to be in a waiting
  // status ('todo', 'triage', 'scheduled', 'on_hold'). That extra gate is
  // deliberately a UI-layer concern. `blockerStand` reports facts about the
  // BLOCKERS and must never inspect the subject task's own status — the two
  // tests below pin that separation down so it is not "fixed" into the helper.
  it('still reports all-clear for a DONE task whose blockers are all done', () => {
    const { graph, index } = scene(
      [['a', 'done'], ['b', 'done'], ['subject', 'done']],
      [['a', 'subject'], ['b', 'subject']]
    )

    // The helper does not suppress this case; the UI is what stays quiet.
    expect(blockerStand(graph, index, 'subject')).toEqual({ total: 2, gating: 0 })
  })

  it('depends only on the blockers\u2019 statuses, never on the subject\u2019s own', () => {
    const stands = ['done', 'todo', 'triage', 'scheduled', 'on_hold', 'running', 'archived'].map(status => {
      const { graph, index } = scene(
        [['a', 'done'], ['b', 'done'], ['subject', status]],
        [['a', 'subject'], ['b', 'subject']]
      )

      return blockerStand(graph, index, 'subject')
    })

    // A done subject and a todo subject are indistinguishable at this layer.
    for (const stand of stands) {
      expect(stand).toEqual({ total: 2, gating: 0 })
    }
  })

  it('counts a blocker missing from the board index toward gating', () => {
    const { graph, index } = scene([['kid', 'todo'], ['a', 'done']], [['a', 'kid'], ['ghost', 'kid']])

    expect(blockerStand(graph, index, 'kid')).toEqual({ total: 2, gating: 1 })
  })

  it('never reports all-clear while a blocker is missing, even with the rest done', () => {
    const { graph, index } = scene([['kid', 'todo'], ['a', 'done']], [['a', 'kid'], ['ghost', 'kid']])

    expect(blockerStand(graph, index, 'kid').gating).toBeGreaterThan(0)
  })

  it('ignores dependants — only blockers count', () => {
    const { graph, index } = scene(
      [['root', 'running'], ['kid', 'todo']],
      [['root', 'kid']]
    )

    // `root` blocks `kid` but nothing blocks `root`.
    expect(blockerStand(graph, index, 'root')).toEqual({ total: 0, gating: 0 })
  })

  it('returns a zero stand for a board with no link_edges (older backend)', () => {
    const board = makeBoard([['a', 'todo']])

    expect(blockerStand(buildGraph(board), indexBoard(board), 'a')).toEqual({ total: 0, gating: 0 })
  })

  it('returns a zero stand for an id that is not on the board at all', () => {
    const { graph, index } = scene([['a', 'todo']], [])

    expect(blockerStand(graph, index, 'nobody')).toEqual({ total: 0, gating: 0 })
  })

  it('agrees with partitionBlockers over the resolved rows, missing ids included', () => {
    const { graph, index } = scene(
      [['kid', 'todo'], ['a', 'done'], ['b', 'running'], ['c', 'archived']],
      [['a', 'kid'], ['b', 'kid'], ['c', 'kid'], ['ghost', 'kid']]
    )

    const stand = blockerStand(graph, index, 'kid')
    const { gating, satisfied } = partitionBlockers(rows(graph, 'kid', index))

    expect(stand.total).toBe(gating.length + satisfied.length)
    expect(stand.gating).toBe(gating.length)
    expect(stand).toEqual({ total: 4, gating: 2 })
  })
})

describe('focusSets', () => {
  // grandparent → parent → focus → child → grandchild
  const { graph } = scene(
    [
      ['grandparent', 'todo'],
      ['parent', 'todo'],
      ['focus', 'todo'],
      ['child', 'todo'],
      ['grandchild', 'todo']
    ],
    [
      ['grandparent', 'parent'],
      ['parent', 'focus'],
      ['focus', 'child'],
      ['child', 'grandchild']
    ]
  )

  it('lights the direct blockers upstream', () => {
    expect([...focusSets(graph, 'focus').upstream]).toEqual(['parent'])
  })

  it('lights the direct dependants downstream', () => {
    expect([...focusSets(graph, 'focus').downstream]).toEqual(['child'])
  })

  it('does NOT include a grandparent two hops upstream — one hop is deliberate', () => {
    expect(focusSets(graph, 'focus').upstream.has('grandparent')).toBe(false)
  })

  it('does NOT include a grandchild two hops downstream — one hop is deliberate', () => {
    expect(focusSets(graph, 'focus').downstream.has('grandchild')).toBe(false)
  })

  it('keeps the two directions separate', () => {
    const { upstream, downstream } = focusSets(graph, 'focus')

    expect(upstream.has('child')).toBe(false)
    expect(downstream.has('parent')).toBe(false)
  })

  it('returns the focused id in neither set — the caller lights the card itself', () => {
    const { upstream, downstream } = focusSets(graph, 'focus')

    expect(upstream.has('focus')).toBe(false)
    expect(downstream.has('focus')).toBe(false)
  })

  it('returns two empty sets for an unknown id', () => {
    const { upstream, downstream } = focusSets(graph, 'nobody')

    expect(upstream.size).toBe(0)
    expect(downstream.size).toBe(0)
  })

  it('returns two empty sets against a board with no link_edges', () => {
    const { upstream, downstream } = focusSets(buildGraph(makeBoard([['a', 'todo']])), 'a')

    expect(upstream.size).toBe(0)
    expect(downstream.size).toBe(0)
  })

  it('gives a chain end an empty set on its open side', () => {
    expect(focusSets(graph, 'grandparent').upstream.size).toBe(0)
    expect(focusSets(graph, 'grandchild').downstream.size).toBe(0)
  })

  it('de-duplicates repeated neighbours into a set', () => {
    const { graph: doubled } = scene([['p', 'todo'], ['c', 'todo']], [['p', 'c'], ['p', 'c']])

    expect(focusSets(doubled, 'c').upstream.size).toBe(1)
  })
})

describe('a diamond dependency: A blocks B and C; B and C block D', () => {
  const diamond = (statuses: Partial<Record<'a' | 'b' | 'c' | 'd', string>> = {}) =>
    scene(
      [
        ['a', statuses.a ?? 'running'],
        ['b', statuses.b ?? 'todo'],
        ['c', statuses.c ?? 'todo'],
        ['d', statuses.d ?? 'todo']
      ],
      [
        ['a', 'b'],
        ['a', 'c'],
        ['b', 'd'],
        ['c', 'd']
      ]
    )

  it('gives A no blockers and both mid nodes as dependants', () => {
    const { graph } = diamond()

    expect(upstreamOf(graph, 'a')).toEqual([])
    expect([...downstreamOf(graph, 'a')].sort()).toEqual(['b', 'c'])
  })

  it('gives B a single blocker A and a single dependant D', () => {
    const { graph } = diamond()

    expect(upstreamOf(graph, 'b')).toEqual(['a'])
    expect(downstreamOf(graph, 'b')).toEqual(['d'])
  })

  it('gives C a single blocker A and a single dependant D', () => {
    const { graph } = diamond()

    expect(upstreamOf(graph, 'c')).toEqual(['a'])
    expect(downstreamOf(graph, 'c')).toEqual(['d'])
  })

  it('gives D both mid nodes as blockers and nothing downstream', () => {
    const { graph } = diamond()

    expect([...upstreamOf(graph, 'd')].sort()).toEqual(['b', 'c'])
    expect(downstreamOf(graph, 'd')).toEqual([])
  })

  it('holds D fully gated while both mid nodes are open', () => {
    const { graph, index } = diamond()

    expect(blockerStand(graph, index, 'd')).toEqual({ total: 2, gating: 2 })
  })

  it('half-clears D when only one mid node is done', () => {
    const { graph, index } = diamond({ b: 'done' })

    expect(blockerStand(graph, index, 'd')).toEqual({ total: 2, gating: 1 })
  })

  it('clears D once both mid nodes are terminal', () => {
    const { graph, index } = diamond({ b: 'done', c: 'archived' })

    expect(blockerStand(graph, index, 'd')).toEqual({ total: 2, gating: 0 })
  })

  it('clears B and C once A is done, without touching D', () => {
    const { graph, index } = diamond({ a: 'done' })

    expect(blockerStand(graph, index, 'b')).toEqual({ total: 1, gating: 0 })
    expect(blockerStand(graph, index, 'c')).toEqual({ total: 1, gating: 0 })
    expect(blockerStand(graph, index, 'd')).toEqual({ total: 2, gating: 2 })
  })

  it('focuses D on the two mid nodes only — A stays dim two hops away', () => {
    const { graph } = diamond()
    const { upstream, downstream } = focusSets(graph, 'd')

    expect([...upstream].sort()).toEqual(['b', 'c'])
    expect(upstream.has('a')).toBe(false)
    expect(downstream.size).toBe(0)
  })

  it('focuses A on the two mid nodes only — D stays dim two hops away', () => {
    const { graph } = diamond()
    const { upstream, downstream } = focusSets(graph, 'a')

    expect([...downstream].sort()).toEqual(['b', 'c'])
    expect(downstream.has('d')).toBe(false)
    expect(upstream.size).toBe(0)
  })

  it('resolves D\u2019s blocker rows to the real mid-node tasks', () => {
    const { graph, index } = diamond({ b: 'done' })
    const resolved = rows(graph, 'd', index)

    expect(resolved.map(row => [row.id, row.status, row.missing])).toEqual([
      ['b', 'done', false],
      ['c', 'todo', false]
    ])
  })

  it('partitions D\u2019s rows into one satisfied and one gating', () => {
    const { graph, index } = diamond({ b: 'done' })
    const { gating, satisfied } = partitionBlockers(rows(graph, 'd', index))

    expect(satisfied.map(row => row.id)).toEqual(['b'])
    expect(gating.map(row => row.id)).toEqual(['c'])
  })
})
