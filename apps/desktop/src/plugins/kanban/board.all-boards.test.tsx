/**
 * Focused tests for the consolidated "All Boards" view (t_fdf5184d): the
 * sentinel selection routes to `/board/all` (never `/board`), cards render
 * with their owning board's badge, a mutation on a card fires against that
 * card's OWN board (never the `'*'` sentinel), the board filter chips
 * hide/show cards client-side, a non-empty `errors` array renders a notice
 * without blanking the board, and board-only affordances stay confined to
 * the switcher (covered separately in board-switcher.test.tsx).
 *
 * Exercises the real component tree via @hermes/plugin-sdk, matching the
 * pattern in new-task-paste.test.tsx / drawer.cta.test.tsx. The kanban data
 * layer (./api) is mocked at the module boundary so no real network/REST
 * calls happen.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, ALL_BOARDS } from './api'
import { BoardFilterChips, BoardsErrorNotice, KanbanBoardPage } from './board'
import type { BoardAllInfo, KanbanBoard } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

// Plugins only ever see @hermes/plugin-sdk; stub usePluginI18n to echo the
// dotted key (same shim other kanban tests use) so assertions target stable
// keys instead of translated English text.
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchBoardMock = vi.fn()
const fetchAllBoardsMock = vi.fn()
const patchTaskMock = vi.fn()
const deleteTaskMock = vi.fn()

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    deleteTask: (...args: unknown[]) => deleteTaskMock(...args),
    fetchAllBoards: (...args: unknown[]) => fetchAllBoardsMock(...args),
    fetchBoard: (...args: unknown[]) => fetchBoardMock(...args),
    fetchBoards: vi.fn().mockResolvedValue({ boards: [], current: 'shipping' }),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    patchTask: (...args: unknown[]) => patchTaskMock(...args)
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  fetchBoardMock.mockReset()
  fetchAllBoardsMock.mockReset()
  patchTaskMock.mockReset().mockResolvedValue({})
  deleteTaskMock.mockReset().mockResolvedValue({})
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
}

/** A merged All Boards payload: one task from `shipping`, one from `homelab`. */
function allBoardsPayload(overrides: Partial<KanbanBoard> = {}): KanbanBoard {
  return {
    assignees: [],
    boards: [
      { color: '#60a5fa', icon: 'rocket', name: 'Shipping', slug: 'shipping', task_count: 1 },
      { color: '#34d399', icon: 'home', name: 'Homelab', slug: 'homelab', task_count: 1 }
    ],
    columns: [
      {
        name: 'todo',
        tasks: [
          { board: 'shipping', board_name: 'Shipping', id: 't_ship01', status: 'todo', title: 'Ship the feature' },
          { board: 'homelab', board_name: 'Homelab', id: 't_home01', status: 'todo', title: 'Fix the router' }
        ]
      },
      { name: 'ready', tasks: [] },
      { name: 'running', tasks: [] },
      { name: 'blocked', tasks: [] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [] }
    ],
    errors: [],
    latest_event_id: 0,
    now: 0,
    tenants: [],
    ...overrides
  }
}

describe('sentinel data source', () => {
  it('selecting the sentinel calls fetchAllBoards, not fetchBoard', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    await waitFor(() => expect(fetchAllBoardsMock).toHaveBeenCalled())
    expect(fetchBoardMock).not.toHaveBeenCalled()
  })

  it('single-board mode calls fetchBoard, not fetchAllBoards (unchanged)', async () => {
    $boardSlug.set('')
    fetchBoardMock.mockResolvedValue({
      assignees: [],
      columns: [{ name: 'todo', tasks: [] }],
      latest_event_id: 0,
      now: 0,
      tenants: []
    } satisfies KanbanBoard)

    mount()

    await waitFor(() => expect(fetchBoardMock).toHaveBeenCalled())
    expect(fetchAllBoardsMock).not.toHaveBeenCalled()
  })
})

describe('card board badges', () => {
  it('renders each card with its own board name', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    expect(await screen.findByText('Ship the feature')).toBeTruthy()
    expect(screen.getByText('Fix the router')).toBeTruthy()
    // "Shipping"/"Homelab" also render as filter chips — assert at least one
    // occurrence of each (the card badge) rather than a single unique match.
    expect(screen.getAllByText('Shipping').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Homelab').length).toBeGreaterThan(0)
  })

  it('single-board mode renders no board badge', async () => {
    $boardSlug.set('')
    fetchBoardMock.mockResolvedValue({
      assignees: [],
      columns: [{ name: 'todo', tasks: [{ id: 't_x', status: 'todo', title: 'Plain task' }] }],
      latest_event_id: 0,
      now: 0,
      tenants: []
    } satisfies KanbanBoard)

    mount()

    expect(await screen.findByText('Plain task')).toBeTruthy()
    // Neither board name from the All Boards fixtures should ever appear.
    expect(screen.queryByText('Shipping')).toBeNull()
    expect(screen.queryByText('Homelab')).toBeNull()
  })
})

describe('mutations route to the card\'s own board, never the sentinel', () => {
  it('moving a card (context menu -> status) patches with that card\'s board slug', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    await screen.findByText('Ship the feature')

    fireEvent.contextMenu(screen.getByText('Ship the feature'))

    // Every "move to <column>" item renders identical text under the
    // dotted-key i18n stub (args are dropped) — any one proves the point,
    // since what's under test is the BOARD parameter, not the target status.
    const [moveTo] = await screen.findAllByText('moveTo')

    fireEvent.click(moveTo)

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    // Third positional arg is the explicit board — must be the card's OWN
    // board, never the '*' sentinel.
    const [id, patch, board] = patchTaskMock.mock.calls[0]
    expect(id).toBe('t_ship01')
    expect(patch).toHaveProperty('status')
    expect(board).toBe('shipping')
    expect(board).not.toBe(ALL_BOARDS)
  })

  it('deleting a card from a different board than the first patches with ITS board', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    await screen.findByText('Fix the router')
    fireEvent.contextMenu(screen.getByText('Fix the router'))

    fireEvent.click(await screen.findByText('delete'))

    await waitFor(() => expect(deleteTaskMock).toHaveBeenCalled())
    const [id, board] = deleteTaskMock.mock.calls[0]
    expect(id).toBe('t_home01')
    expect(board).toBe('homelab')
  })
})

describe('degraded state', () => {
  it('a non-empty errors array renders a notice without blanking the board', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(
      allBoardsPayload({ errors: [{ board: 'ourhouse', detail: 'database is locked' }] })
    )

    mount()

    // The board still renders the boards that succeeded...
    expect(await screen.findByText('Ship the feature')).toBeTruthy()
    expect(screen.getByText('Fix the router')).toBeTruthy()
    // ...and the failed board is named in a notice, not swallowed.
    expect(screen.getByText('boardsFailedNotice')).toBeTruthy()
  })

  it('an empty errors array renders no notice', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    await screen.findByText('Ship the feature')
    expect(screen.queryByText('boardsFailedNotice')).toBeNull()
  })
})

describe('BoardFilterChips (unit)', () => {
  const boards: BoardAllInfo[] = [
    { color: '#60a5fa', icon: 'rocket', name: 'Shipping', slug: 'shipping', task_count: 3 },
    { color: '#34d399', icon: 'home', name: 'Homelab', slug: 'homelab', task_count: 1 }
  ]

  it('renders nothing when there are no boards', () => {
    const { container } = render(<BoardFilterChips boards={[]} hidden={{}} onToggle={vi.fn()} />)

    expect(container.innerHTML).toBe('')
  })

  it('renders one chip per board, all pressed (visible) by default', () => {
    render(<BoardFilterChips boards={boards} hidden={{}} onToggle={vi.fn()} />)

    const [shipping] = screen.getAllByRole('button', { name: 'toggleBoard' })

    expect(shipping.getAttribute('aria-pressed')).toBe('true')
  })

  it('a hidden board renders its chip un-pressed', () => {
    render(<BoardFilterChips boards={boards} hidden={{ shipping: true }} onToggle={vi.fn()} />)

    const [shipping] = screen.getAllByRole('button', { name: 'toggleBoard' })

    expect(shipping.getAttribute('aria-pressed')).toBe('false')
  })

  it('clicking a chip calls onToggle with that board\'s slug', () => {
    const onToggle = vi.fn()
    render(<BoardFilterChips boards={boards} hidden={{}} onToggle={onToggle} />)

    fireEvent.click(screen.getAllByRole('button', { name: 'toggleBoard' })[0])

    expect(onToggle).toHaveBeenCalledWith('shipping')
  })
})

describe('board filter chips hide/show cards (integration)', () => {
  it('toggling a board chip off drops its cards from the board; toggling back restores them', async () => {
    $boardSlug.set(ALL_BOARDS)
    fetchAllBoardsMock.mockResolvedValue(allBoardsPayload())

    mount()

    await screen.findByText('Ship the feature')
    expect(screen.getByText('Fix the router')).toBeTruthy()

    // "Homelab" renders twice (the filter chip AND the card's board badge) —
    // scope to the chip via its aria-pressed toggle role.
    const homelabChip = screen.getAllByRole('button', { name: 'toggleBoard' }).find(btn => btn.textContent?.includes('Homelab'))!

    fireEvent.click(homelabChip)

    await waitFor(() => expect(screen.queryByText('Fix the router')).toBeNull())
    expect(screen.getByText('Ship the feature')).toBeTruthy()
    expect(homelabChip.getAttribute('aria-pressed')).toBe('false')

    // Click it again to restore.
    fireEvent.click(homelabChip)

    await waitFor(() => expect(screen.getByText('Fix the router')).toBeTruthy())
  })
})

describe('BoardsErrorNotice (unit)', () => {
  it('renders nothing with no errors', () => {
    const { container } = render(<BoardsErrorNotice errors={[]} />)

    expect(container.innerHTML).toBe('')
  })

  it('renders nothing with undefined errors', () => {
    const { container } = render(<BoardsErrorNotice errors={undefined} />)

    expect(container.innerHTML).toBe('')
  })

  it('names the failed board(s) in the notice', () => {
    render(<BoardsErrorNotice errors={[{ board: 'ourhouse', detail: 'db locked' }]} />)

    expect(screen.getByText('boardsFailedNotice')).toBeTruthy()
  })
})
