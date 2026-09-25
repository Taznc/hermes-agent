/**
 * Behaviour contracts for the dependency-graph overlay: a chain renders one
 * node per member card and one arrowed edge per link, clicking a node asks
 * the board to re-centre on it, and the empty state replaces the canvas when
 * there is nothing to draw. Same mounting pattern as drawer.cta.test.tsx
 * (real @hermes/plugin-sdk, usePluginI18n stubbed to echo the key).
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DependencyGraphDialog } from './dependency-graph-dialog'
import { buildGraph, indexBoard } from './deps'
import type { KanbanBoard, KanbanTask } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

afterEach(() => {
  cleanup()
})

const task = (id: string, status: string): KanbanTask => ({ id, status, title: `Task ${id}`, assignee: null })

function board(tasks: KanbanTask[], edges: Array<[string, string]>): KanbanBoard {
  return {
    columns: [{ name: 'todo', tasks }],
    tenants: [],
    assignees: [],
    latest_event_id: 1,
    now: 1,
    link_edges: edges
  }
}

function mount(b: KanbanBoard, focusedKey: null | string, hasEdges = true) {
  const onRecentre = vi.fn()
  const onOpenCard = vi.fn()
  const onClose = vi.fn()

  render(
    <DependencyGraphDialog
      focusedKey={focusedKey}
      graph={buildGraph(b)}
      hasEdges={hasEdges}
      index={indexBoard(b)}
      onClose={onClose}
      onOpenCard={onOpenCard}
      onRecentre={onRecentre}
    />
  )

  return { onClose, onOpenCard, onRecentre }
}

const chain = board([task('a', 'done'), task('b', 'todo'), task('c', 'todo')], [
  ['a', 'b'],
  ['b', 'c']
])

describe('DependencyGraphDialog', () => {
  it('renders one node per chain member and one arrowed edge per link', () => {
    mount(chain, 'b')

    const dialog = screen.getByRole('dialog')

    expect(dialog.querySelectorAll('[data-node-key]')).toHaveLength(3)

    const edges = dialog.querySelectorAll('svg path[data-edge]')

    expect(edges).toHaveLength(2)

    for (const edge of edges) {
      expect(edge.getAttribute('marker-end')).toMatch(/^url\(#/)
    }

    expect(dialog.querySelectorAll('svg defs marker').length).toBeGreaterThan(0)
  })

  it('points each arrow from the blocker to the card it blocks', () => {
    mount(chain, 'b')

    const ids = [...screen.getByRole('dialog').querySelectorAll('svg path[data-edge]')].map(p => p.getAttribute('data-edge'))

    expect(ids).toEqual(expect.arrayContaining(['a->b', 'b->c']))
  })

  it('mutes an edge whose blocker is already done and emphasises one that still gates', () => {
    mount(chain, 'b')

    const dialog = screen.getByRole('dialog')

    expect(dialog.querySelector('path[data-edge="a->b"]')?.getAttribute('data-gating')).toBe('false')
    expect(dialog.querySelector('path[data-edge="b->c"]')?.getAttribute('data-gating')).toBe('true')
  })

  it('re-centres on a clicked node', () => {
    const { onRecentre } = mount(chain, 'b')

    fireEvent.click(screen.getByRole('button', { name: 'Task c' }))

    expect(onRecentre).toHaveBeenCalledWith('c')
  })

  it('opens the card drawer from the node action and closes itself', () => {
    const { onClose, onOpenCard, onRecentre } = mount(chain, 'b')

    const node = screen.getByRole('dialog').querySelector('[data-node-key="a"]')!

    fireEvent.click(node.querySelector('button[aria-label="depGraphOpenCard"]')!)

    expect(onOpenCard).toHaveBeenCalledWith('a')
    expect(onClose).toHaveBeenCalled()
    expect(onRecentre).not.toHaveBeenCalled()
  })

  it('shows the empty state instead of a canvas when the card has no links', () => {
    mount(board([task('solo', 'todo')], []), 'solo')

    const dialog = screen.getByRole('dialog')

    expect(screen.getByText('depGraphEmpty')).toBeTruthy()
    expect(dialog.querySelector('[data-edge-layer]')).toBeNull()
  })

  it('shows the empty state on a backend without link_edges even if links exist', () => {
    mount(chain, 'b', false)

    expect(screen.getByText('depGraphEmpty')).toBeTruthy()
    expect(screen.getByRole('dialog').querySelector('[data-edge-layer]')).toBeNull()
  })

  it('renders nothing while closed', () => {
    mount(chain, null)

    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
