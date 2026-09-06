/**
 * All Boards mode WRITE routing (t_70f6ac4e). The consolidated view's
 * `$boardSlug` is the `'*'` sentinel, which `withBoard` deliberately omits from
 * the query string — correct for reads, silently wrong for writes, because the
 * server then falls back to whatever board is currently ACTIVE.
 *
 * Two defects live here and they compound: a create that misroutes puts a card
 * on the wrong board, and a dependency drawn to that card then becomes an
 * INTRA-board link the user never intended, reported as success. So the
 * assertions below pin the create target and the link candidates together.
 *
 * Companion to board.all-boards.test.tsx (which covers reads, badges, chips,
 * and the already-migrated move/delete paths).
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, ALL_BOARDS } from './api'
import type { KanbanBoard, KanbanTask, KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const createTaskMock = vi.fn()
const patchTaskMock = vi.fn()
const linkTasksMock = vi.fn()
const fetchTaskMock = vi.fn()

// Hoisted with the vi.mock factory below, which references it.
const { BOARDS } = vi.hoisted(() => ({
  BOARDS: {
    boards: [
      { is_current: true, name: 'Shipping', slug: 'shipping' },
      { name: 'Homelab', slug: 'homelab' }
    ],
    current: 'shipping'
  }
}))

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    createTask: (...args: unknown[]) => createTaskMock(...args),
    fetchAllBoards: vi.fn(),
    fetchBoard: vi.fn(),
    fetchBoards: vi.fn().mockResolvedValue(BOARDS),
    fetchLog: vi.fn().mockResolvedValue({ text: '' }),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    fetchTask: (...args: unknown[]) => fetchTaskMock(...args),
    linkTasks: (...args: unknown[]) => linkTasksMock(...args),
    patchTask: (...args: unknown[]) => patchTaskMock(...args)
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  createTaskMock.mockReset().mockResolvedValue({ task: { id: 't_new', status: 'ready' } as KanbanTask })
  patchTaskMock.mockReset().mockResolvedValue({})
  linkTasksMock.mockReset().mockResolvedValue({})
  fetchTaskMock.mockReset()
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

function client() {
  return new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
}

// ── #2: the create target ────────────────────────────────────────────────────

async function renderNewTaskDialog() {
  const { NewTaskDialog } = await import('./board')

  render(
    <QueryClientProvider client={client()}>
      <NewTaskDialog onClose={vi.fn()} parents={[]} target="ready" />
    </QueryClientProvider>
  )
}

const typeTitle = (value: string) =>
  fireEvent.change(screen.getByPlaceholderText('titlePlaceholder'), { target: { value } })

describe('new-task dialog: the board a card is created on (All Boards mode)', () => {
  it('offers an explicit board picker under the sentinel', async () => {
    $boardSlug.set(ALL_BOARDS)
    await renderNewTaskDialog()

    expect(await screen.findByText('pickBoardHint')).toBeTruthy()
  })

  it('single-board mode shows no picker and sends no explicit board (unchanged)', async () => {
    $boardSlug.set('')
    await renderNewTaskDialog()

    typeTitle('A plain task')
    fireEvent.click(screen.getByText('createTask'))

    await waitFor(() => expect(createTaskMock).toHaveBeenCalledTimes(1))
    // Second positional arg is the explicit board — absent here, so `withBoard`
    // still resolves from `$boardSlug` exactly as it always did.
    expect(createTaskMock.mock.calls[0][1]).toBeUndefined()
    expect(screen.queryByText('pickBoardHint')).toBeNull()
  })

  it('creates on the picked board, never the sentinel and never an implicit default', async () => {
    $boardSlug.set(ALL_BOARDS)
    await renderNewTaskDialog()

    await screen.findByText('pickBoardHint')
    typeTitle('Homelab card')

    // Default is the server's own current board — pre-selected and VISIBLE.
    await waitFor(() => expect(screen.getByText('createTask').closest('button')?.disabled).toBe(false))

    fireEvent.click(screen.getByText('createTask'))

    await waitFor(() => expect(createTaskMock).toHaveBeenCalledTimes(1))

    const [, board] = createTaskMock.mock.calls[0]

    expect(board).toBe('shipping')
    expect(board).not.toBe(ALL_BOARDS)
    expect(board).toBeTruthy()
  })

  it("the follow-up status patch lands on the SAME board as the create", async () => {
    $boardSlug.set(ALL_BOARDS)
    // create() answers 'ready'; the dialog's target lane is 'ready' too, so
    // force a mismatch to exercise the follow-up patch.
    createTaskMock.mockResolvedValue({ task: { id: 't_new', status: 'triage' } as KanbanTask })

    await renderNewTaskDialog()

    await screen.findByText('pickBoardHint')
    typeTitle('Needs a move after create')
    fireEvent.click(screen.getByText('createTask'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalledTimes(1))

    const [, , patchBoard] = patchTaskMock.mock.calls[0]
    const [, createBoard] = createTaskMock.mock.calls[0]

    expect(patchBoard).toBe(createBoard)
    expect(patchBoard).not.toBe(ALL_BOARDS)
  })

  // Radix renders `SelectContent` only once opened, so assert on whether the
  // parent field EXISTS at all: it is gated on there being at least one
  // same-board candidate, which is the behavior that matters.
  it('offers no parent field when every candidate is on another board', async () => {
    $boardSlug.set(ALL_BOARDS)

    const { NewTaskDialog } = await import('./board')

    render(
      <QueryClientProvider client={client()}>
        <NewTaskDialog
          onClose={vi.fn()}
          parents={[{ board: 'homelab', id: 't_home_parent', title: 'Homelab parent' }]}
          target="ready"
        />
      </QueryClientProvider>
    )

    // Target board defaults to `shipping` (the server's current), and a
    // cross-board parent is rejected by the backend, which only ever sees one
    // board's DB per request — so it must not be offered.
    await screen.findByText('pickBoardHint')
    expect(screen.queryByText('parent')).toBeNull()
  })

  it('offers the parent field when a candidate shares the target board', async () => {
    $boardSlug.set(ALL_BOARDS)

    const { NewTaskDialog } = await import('./board')

    render(
      <QueryClientProvider client={client()}>
        <NewTaskDialog
          onClose={vi.fn()}
          parents={[
            { board: 'shipping', id: 't_ship_parent', title: 'Shipping parent' },
            { board: 'homelab', id: 't_home_parent', title: 'Homelab parent' }
          ]}
          target="ready"
        />
      </QueryClientProvider>
    )

    await screen.findByText('pickBoardHint')
    await waitFor(() => expect(screen.queryByText('parent')).toBeTruthy())
  })

  it('single-board mode offers every parent, board-less (unchanged)', async () => {
    $boardSlug.set('')

    const { NewTaskDialog } = await import('./board')

    render(
      <QueryClientProvider client={client()}>
        <NewTaskDialog onClose={vi.fn()} parents={[{ id: 't_parent', title: 'A parent' }]} target="ready" />
      </QueryClientProvider>
    )

    await waitFor(() => expect(screen.queryByText('parent')).toBeTruthy())
  })
})

// ── #3: the dependency picker's candidates ───────────────────────────────────

/** The merged All Boards cache the drawer resolves candidates against. */
function seedMergedBoard(qc: QueryClient) {
  const merged: KanbanBoard = {
    assignees: [],
    boards: [
      { color: '', icon: '', name: 'Shipping', slug: 'shipping', task_count: 2 },
      { color: '', icon: '', name: 'Homelab', slug: 'homelab', task_count: 1 }
    ],
    columns: [
      {
        name: 'todo',
        tasks: [
          { board: 'homelab', board_name: 'Homelab', id: 't_home_open', status: 'todo', title: 'Homelab open card' },
          { board: 'homelab', board_name: 'Homelab', id: 't_home_other', status: 'todo', title: 'Homelab sibling' },
          { board: 'shipping', board_name: 'Shipping', id: 't_ship_foreign', status: 'todo', title: 'Shipping stranger' }
        ]
      }
    ],
    latest_event_id: 0,
    now: 0,
    tenants: []
  }

  qc.setQueryData(['kanban', 'board', ALL_BOARDS, false], merged)
}

const detail = (): KanbanTaskDetail => ({
  attachments: [],
  comments: [],
  events: [],
  links: { children: [], parents: [] },
  runs: [],
  task: { id: 't_home_open', status: 'todo', title: 'Homelab open card' }
})

async function renderDrawerOnHomelabCard() {
  const { TaskDrawer } = await import('./drawer')
  const qc = client()

  seedMergedBoard(qc)
  fetchTaskMock.mockResolvedValue(detail())
  $boardSlug.set(ALL_BOARDS)

  render(
    <QueryClientProvider client={qc}>
      <TaskDrawer board="homelab" columns={['todo', 'ready']} id="t_home_open" onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

  // Open the inline "link a blocker" picker.
  const addBlocker = await screen.findByLabelText('parent')

  fireEvent.click(addBlocker)
}

describe('dependency picker: candidates are same-board only (All Boards mode)', () => {
  it('offers a sibling on the same board', async () => {
    await renderDrawerOnHomelabCard()

    expect(await screen.findByText('Homelab sibling')).toBeTruthy()
  })

  it('never offers a card from another board — that link always 400s', async () => {
    await renderDrawerOnHomelabCard()

    await screen.findByText('Homelab sibling')
    expect(screen.queryByText('Shipping stranger')).toBeNull()
  })

  it('a picked candidate links parent and child on ONE board', async () => {
    await renderDrawerOnHomelabCard()

    fireEvent.click(await screen.findByText('Homelab sibling'))

    await waitFor(() => expect(linkTasksMock).toHaveBeenCalledTimes(1))

    const [parentId, childId, board] = linkTasksMock.mock.calls[0]

    // Both ends are homelab cards and the write is pinned to homelab, so the
    // link the user drew is the link that lands.
    expect(parentId).toBe('t_home_other')
    expect(childId).toBe('t_home_open')
    expect(board).toBe('homelab')
    expect(board).not.toBe(ALL_BOARDS)
  })
})
