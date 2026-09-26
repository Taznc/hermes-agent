/**
 * The focus-mode answer bar + status-coloured lines, mounted on the real
 * KanbanBoardPage: the bar replaces the focus hint while a trace is live,
 * says in words why the focused card is stuck, lists its links most-stuck
 * first, isolates a line when its row is hovered, and moves the focus when a
 * row is clicked. Data layer mocked at the module boundary, same shape as
 * board.dependency-arrows.test.tsx. i18n is echoed, so text assertions read
 * the dotted key.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { $boardSlug, $depChevrons, $depFlow } from './api'
import { KanbanBoardPage } from './board'
import { $hotEdge } from './board-arrows-layer'
import { linkTone } from './focus-verdict'
import type { KanbanBoard } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchBoardMock = vi.fn()

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    fetchBoard: (...args: unknown[]) => fetchBoardMock(...args),
    fetchBoards: vi.fn().mockResolvedValue({ boards: [], current: 'default' }),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] })
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
  $hotEdge.set(null)
  $depChevrons.set(true)
  $depFlow.set(false)
})

/**
 * Two On-hold blockers and one done blocker hold `f` (a Stalled verdict); a
 * Blocked card holds `h1` (so focusing h1 reads Waiting); `f` blocks `c`.
 */
function stalledBoard(): KanbanBoard {
  const task = (id: string, title: string, status: string) => ({ id, status, title })

  return {
    assignees: [],
    columns: [
      { name: 'todo', tasks: [task('f', 'Release notes', 'todo')] },
      { name: 'ready', tasks: [task('c', 'Canary run', 'ready')] },
      { name: 'running', tasks: [] },
      { name: 'blocked', tasks: [task('x', 'Redeploy coverage', 'blocked')] },
      { name: 'on_hold', tasks: [task('h1', 'Salvage escalation', 'on_hold'), task('h2', 'Salvage gate', 'on_hold')] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [task('d', 'Allowlist refresh', 'done')] }
    ],
    latest_event_id: 0,
    link_edges: [
      ['h1', 'f'],
      ['h2', 'f'],
      ['d', 'f'],
      ['x', 'h1'],
      ['f', 'c']
    ],
    now: 0,
    tenants: []
  } as KanbanBoard
}

let root: HTMLElement

async function mount() {
  fetchBoardMock.mockResolvedValue(stalledBoard())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  root = render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  ).container

  await screen.findByText('Release notes')
}

const cardByKey = (key: string) => root.querySelector<HTMLElement>(`[data-card-key="${key}"]`)!

const focusCard = (key: string) =>
  fireEvent.click(
    Array.from(cardByKey(key).querySelectorAll('button')).find(b =>
      /depFocusHint|depClearFocus/.test(b.getAttribute('aria-label') ?? '')
    )!
  )

const bar = () => root.querySelector<HTMLElement>('[data-answer-bar]')
const line = (edge: string) => root.querySelector<SVGGElement>(`[data-board-arrows] [data-edge="${edge}"]`)

const rowKeys = (list: string) =>
  Array.from(bar()!.querySelectorAll(`[${list}] [data-link-row]`)).map(row => row.getAttribute('data-link-row'))

describe('focus answer bar', () => {
  it('appears only while a trace is live, with the verdict, lists and legend', async () => {
    await mount()
    expect(bar()).toBeNull()

    focusCard('f')
    await waitFor(() => expect(bar()).not.toBeNull())

    expect(bar()!.querySelector('[data-verdict]')!.getAttribute('data-verdict')).toBe('stalled')
    expect(bar()!.textContent).toContain('depVerdictStalled')
    // Most-stuck first: the two On-hold blockers lead, the satisfied one trails.
    expect(rowKeys('data-blocker-rows')).toEqual(['h1', 'h2', 'd'])
    expect(rowKeys('data-dependant-rows')).toEqual(['c'])
    expect(bar()!.textContent).toContain('depLegendLead')

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(bar()).toBeNull())
  })

  it('the focused card carries the same verdict as a roll-up', async () => {
    await mount()
    focusCard('f')

    await waitFor(() => expect(cardByKey('f').querySelector('[data-focus-rollup]')).not.toBeNull())
    expect(cardByKey('f').querySelector('[data-focus-rollup]')!.getAttribute('data-focus-rollup')).toBe('stalled')
    // Nowhere else: only the focused card answers for itself.
    expect(root.querySelectorAll('[data-focus-rollup]')).toHaveLength(1)
  })

  it('colours each line by its BLOCKER status; a satisfied blocker is dashed', async () => {
    await mount()
    focusCard('f')
    await waitFor(() => expect(line('h1->f')).not.toBeNull())

    const stroke = (edge: string) => line(edge)!.querySelector('.kanban-dep-line')!

    expect(line('h1->f')!.getAttribute('data-status')).toBe('on_hold')
    expect(stroke('h1->f').getAttribute('stroke')).toBe(linkTone('on_hold'))
    // The focus's own outgoing line takes the FOCUSED card's status (it is the blocker there).
    expect(stroke('f->c').getAttribute('stroke')).toBe(linkTone('todo'))
    // Satisfied blocker: the "done" dash (1.6w / 1.4w on a 5px direct line),
    // distinct from whatever a gating line draws. (jsdom lays every card out
    // at 0×0, so gating lines here carry the offscreen dash instead of none.)
    expect(line('d->f')!.getAttribute('data-gating')).toBe('false')
    expect(stroke('d->f').getAttribute('stroke-dasharray')).toBe('8 7')
    expect(stroke('h1->f').getAttribute('stroke-dasharray')).not.toBe('8 7')
    // Every drawn edge gets a big arrowhead on the held-up card.
    expect(root.querySelectorAll('[data-board-arrows] [data-head]')).toHaveLength(4)
  })

  it('hovering a row isolates its line and rings its two cards', async () => {
    await mount()
    focusCard('f')
    await waitFor(() => expect(line('h2->f')).not.toBeNull())

    const row = bar()!.querySelector<HTMLElement>('[data-link-row="h2"]')!

    fireEvent.mouseEnter(row)

    await waitFor(() => expect(line('h2->f')!.hasAttribute('data-hot')).toBe(true))
    expect(root.querySelector('[data-board-arrows]')!.hasAttribute('data-hovering')).toBe(true)
    expect(line('h1->f')!.hasAttribute('data-hot')).toBe(false)
    expect(cardByKey('h2').hasAttribute('data-dep-hot')).toBe(true)
    expect(cardByKey('f').hasAttribute('data-dep-hot')).toBe(true)
    expect(cardByKey('h1').hasAttribute('data-dep-hot')).toBe(false)

    fireEvent.mouseLeave(row)

    await waitFor(() => expect(root.querySelector('[data-board-arrows]')!.hasAttribute('data-hovering')).toBe(false))
    expect(cardByKey('h2').hasAttribute('data-dep-hot')).toBe(false)
  })

  it('clicking a row moves the focus to that card (it does not clear it)', async () => {
    await mount()
    focusCard('f')
    await waitFor(() => expect(bar()).not.toBeNull())

    fireEvent.click(bar()!.querySelector<HTMLElement>('[data-link-row="h1"]')!)

    // h1 is held by a Blocked card: Waiting, not Stalled.
    await waitFor(() => expect(bar()!.querySelector('[data-verdict]')!.getAttribute('data-verdict')).toBe('waiting'))
    expect(rowKeys('data-blocker-rows')).toEqual(['x'])
    expect(rowKeys('data-dependant-rows')).toEqual(['f'])
    await waitFor(() => expect(line('x->h1')).not.toBeNull())
  })

  it('the legend toggles chevrons off and moving dots on, and they persist in the stores', async () => {
    await mount()
    focusCard('f')
    await waitFor(() => expect(line('h1->f')).not.toBeNull())

    expect(root.querySelector('.kanban-dep-flow')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /depFlow/ }))
    await waitFor(() => expect(root.querySelector('.kanban-dep-flow')).not.toBeNull())
    expect($depFlow.get()).toBe(true)
    // Flow rides only on lines whose blocker still gates.
    expect(line('d->f')!.querySelector('.kanban-dep-flow')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /depChevrons/ }))
    expect($depChevrons.get()).toBe(false)
    await waitFor(() => expect(root.querySelector('[data-chevron]')).toBeNull())
  })

  it('a hover left over from one trace does not isolate a line in the next', async () => {
    await mount()
    focusCard('f')
    await waitFor(() => expect(line('h1->f')).not.toBeNull())

    act(() => $hotEdge.set('h1->f'))
    await waitFor(() => expect(line('h1->f')!.hasAttribute('data-hot')).toBe(true))

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(bar()).toBeNull())
    expect($hotEdge.get()).toBeNull()
  })
})
