import { describe, expect, it } from 'vitest'

import { buildGraph, indexBoard } from './deps'
import { BLOCKER_ORDER, focusLinks, focusVerdict, LEGEND_STATUSES, linkTone } from './focus-verdict'
import type { KanbanBoard } from './types'

/** One board, every card in its own status; `edges` are [blocker, blocked]. */
function board(cards: Array<[id: string, status: string]>, edges: Array<[string, string]>): KanbanBoard {
  return {
    columns: [{ name: 'todo', tasks: cards.map(([id, status]) => ({ id, status, title: `T ${id}` })) }],
    link_edges: edges
  } as unknown as KanbanBoard
}

const verdictOf = (b: KanbanBoard, key: string) => focusVerdict(buildGraph(b), indexBoard(b), key)

describe('focusVerdict', () => {
  it('none: a card nothing blocks', () => {
    expect(verdictOf(board([['f', 'todo']], []), 'f').kind).toBe('none')
  })

  it('clear: every blocker is satisfied (done, archived or a wishlist lane)', () => {
    const b = board(
      [
        ['a', 'done'],
        ['b', 'archived'],
        ['f', 'todo']
      ],
      [
        ['a', 'f'],
        ['b', 'f']
      ]
    )

    expect(verdictOf(b, 'f')).toMatchObject({ cleared: 2, kind: 'clear', open: 0 })
  })

  it('stalled: every OPEN blocker is On hold, satisfied ones do not count against it', () => {
    const b = board(
      [
        ['h1', 'on_hold'],
        ['h2', 'on_hold'],
        ['d', 'done'],
        ['f', 'running']
      ],
      [
        ['h1', 'f'],
        ['h2', 'f'],
        ['d', 'f']
      ]
    )

    expect(verdictOf(b, 'f')).toMatchObject({ byStatus: [['on_hold', 2]], cleared: 1, kind: 'stalled', open: 2 })
  })

  it('waiting: any open blocker not On hold; counts grouped most-stuck first', () => {
    const b = board(
      [
        ['r', 'running'],
        ['x', 'blocked'],
        ['h', 'on_hold'],
        ['r2', 'running'],
        ['f', 'todo']
      ],
      [
        ['r', 'f'],
        ['x', 'f'],
        ['h', 'f'],
        ['r2', 'f']
      ]
    )

    expect(verdictOf(b, 'f')).toMatchObject({
      byStatus: [
        ['blocked', 1],
        ['on_hold', 1],
        ['running', 2]
      ],
      kind: 'waiting',
      open: 4
    })
  })

  it('a blocker missing from the board still gates (its link may still be enforced)', () => {
    const b = board([['f', 'todo']], [['gone', 'f']])
    const verdict = verdictOf(b, 'f')

    expect(verdict).toMatchObject({ byStatus: [['unknown', 1]], kind: 'waiting', open: 1 })
    expect(verdict.blockers[0]).toMatchObject({ key: 'gone', missing: true })
  })
})

describe('focusLinks', () => {
  it('orders blockers most-stuck first and resolves titles', () => {
    const b = board(
      [
        ['d', 'done'],
        ['x', 'blocked'],
        ['r', 'ready'],
        ['f', 'todo'],
        ['c', 'review']
      ],
      [
        ['d', 'f'],
        ['r', 'f'],
        ['x', 'f'],
        ['f', 'c']
      ]
    )

    const { blockers, dependants } = focusLinks(buildGraph(b), indexBoard(b), 'f')

    expect(blockers.map(link => link.key)).toEqual(['x', 'r', 'd'])
    expect(dependants).toEqual([{ key: 'c', missing: false, status: 'review', title: 'T c' }])
  })

  it('an unrecognised status sorts before the satisfied tail, never after done', () => {
    const b = board(
      [
        ['d', 'done'],
        ['n', 'brand_new'],
        ['f', 'todo']
      ],
      [
        ['d', 'f'],
        ['n', 'f']
      ]
    )

    expect(focusLinks(buildGraph(b), indexBoard(b), 'f').blockers.map(link => link.key)).toEqual(['n', 'd'])
  })
})

describe('line palette', () => {
  it('gives every legend status (and every ordered status) a colour, On hold its own', () => {
    for (const status of [...LEGEND_STATUSES, ...BLOCKER_ORDER]) {
      expect(linkTone(status)).toMatch(/^#[0-9a-f]{6}$/)
    }

    const holds = linkTone('on_hold')

    expect(['blocked', 'todo', 'review', 'running', 'ready', 'done'].map(linkTone)).not.toContain(holds)
  })

  it('an unknown future status still gets a colour', () => {
    expect(linkTone('brand_new')).toBe(linkTone('todo'))
  })
})
