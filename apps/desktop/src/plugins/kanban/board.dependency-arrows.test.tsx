/**
 * The on-board dependency arrows, mounted on the real KanbanBoardPage: they
 * exist exactly while a trace is live, follow the Direct/Full chain toggle,
 * and never point at a card the board isn't rendering. The data layer
 * (./api) is mocked at the module boundary, same shape as
 * board.focus-depth.test.tsx.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { $boardSlug } from './api'
import { KanbanBoardPage } from './board'
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
})

/** gp → p → f → c, spread across lanes; gp is already done. */
function chainBoard(): KanbanBoard {
  const task = (id: string, title: string, status: string) => ({ id, status, title })

  return {
    assignees: [],
    columns: [
      { name: 'todo', tasks: [task('p', 'Parent task', 'todo'), task('f', 'Focus task', 'todo')] },
      { name: 'ready', tasks: [task('c', 'Child', 'ready')] },
      { name: 'running', tasks: [] },
      { name: 'blocked', tasks: [] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [task('gp', 'Grandparent', 'done')] }
    ],
    latest_event_id: 0,
    link_edges: [
      ['gp', 'p'],
      ['p', 'f'],
      ['f', 'c']
    ],
    now: 0,
    tenants: []
  } as KanbanBoard
}

/** The rendered page root — queries stay inside it rather than the global
 *  document. */
let root: HTMLElement

async function mount() {
  fetchBoardMock.mockResolvedValue(chainBoard())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  root = render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  ).container

  await screen.findByText('Focus task')
}

/** The card container for a title — the draggable node the rings live on.
 *  Scoped to cards: the answer bar repeats linked cards' titles. */
const cardOf = (title: string) =>
  screen
    .queryAllByText(title)
    .map(el => el.closest<HTMLElement>('[draggable="true"]'))
    .find(Boolean) as HTMLElement

const traceButton = (title: string) =>
  Array.from(cardOf(title).querySelectorAll('button')).find(b =>
    /depFocusHint|depClearFocus/.test(b.getAttribute('aria-label') ?? '')
  )!

const drawnEdges = () =>
  Array.from(root.querySelectorAll('[data-board-arrows] [data-edge]'))
    .map(path => path.getAttribute('data-edge'))
    .sort()

describe('dependency arrows on the board', () => {
  it('draws nothing until a card is focused, and clears with the trace', async () => {
    await mount()

    expect(root.querySelector('[data-board-arrows]')).toBeNull()

    fireEvent.click(traceButton('Focus task'))
    await waitFor(() => expect(drawnEdges()).toEqual(['f->c', 'p->f']))

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(root.querySelector('[data-board-arrows]')).toBeNull())
  })

  it('Full chain adds the transitive link; a satisfied blocker is drawn muted', async () => {
    await mount()

    fireEvent.click(traceButton('Focus task'))
    await waitFor(() => expect(drawnEdges()).toEqual(['f->c', 'p->f']))

    fireEvent.click(screen.getByRole('button', { name: 'depFocusChain' }))
    await waitFor(() => expect(drawnEdges()).toEqual(['f->c', 'gp->p', 'p->f']))

    const edge = (id: string) => root.querySelector(`[data-board-arrows] [data-edge="${id}"]`)!

    expect(edge('gp->p').getAttribute('data-gating')).toBe('false')
    expect(edge('p->f').getAttribute('data-gating')).toBe('true')
    // Blocker side and waiting side read in the same colours as the rings.
    expect(edge('p->f').getAttribute('data-side')).toBe('upstream')
    expect(edge('f->c').getAttribute('data-side')).toBe('downstream')
  })

  it('an edge to a card the filter hid is not drawn', async () => {
    await mount()

    fireEvent.click(traceButton('Focus task'))
    await waitFor(() => expect(drawnEdges()).toEqual(['f->c', 'p->f']))

    // "task" keeps Focus and its blocker; Child leaves the board.
    fireEvent.change(screen.getByPlaceholderText('filterCards'), { target: { value: 'task' } })
    await waitFor(() => expect(cardOf('Child')).toBeUndefined())

    await waitFor(() => expect(drawnEdges()).toEqual(['p->f']))
  })
})
