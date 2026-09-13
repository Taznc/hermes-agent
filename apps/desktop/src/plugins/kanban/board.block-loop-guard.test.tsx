/**
 * Dragging a loop-broken triage card back into the work queue must ask first.
 *
 * The backend's unblock-loop breaker parks a task that re-blocks on the same
 * unanswered `needs_input` question into `triage`, so a human decides before it
 * resumes. `PATCH /tasks/{id}` now refuses that move with a 409 unless the
 * request carries `acknowledge_block_loop` — but an API refusal alone reaches
 * the user as a bare error toast that does not say WHY. This dialog is the
 * board's half: it intercepts the drag, explains the loop, and re-sends the
 * move only after a deliberate confirmation.
 *
 * The scoping under test matters as much as the guard: an ordinary triage card
 * (no block history) and a loop whose cause is `capability`/`transient` must
 * drag with no dialog at all, exactly as before.
 *
 * Exercises the real component tree; the kanban data layer (./api) is mocked at
 * the module boundary so no REST calls happen (pattern: board.all-boards.test.tsx).
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug } from './api'
import { KanbanBoardPage } from './board'
import { needsBlockLoopAck } from './status-guidance'
import type { KanbanBoard, KanbanTask } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

// Plugins only ever see @hermes/plugin-sdk; stub usePluginI18n to echo the
// dotted key so assertions target stable keys, not translated English.
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchBoardMock = vi.fn()
const patchTaskMock = vi.fn()

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    fetchAllBoards: vi.fn(),
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
  patchTaskMock.mockReset().mockResolvedValue({})
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

function mount() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
}

/** A single-board payload holding one triage card. Only `triage` and `ready`
 *  are offered as columns, so the card's context menu has exactly ONE
 *  "move to" item (the card's own column is filtered out) and clicking it is
 *  unambiguously a move into the work queue. */
function boardWith(task: Partial<KanbanTask>): KanbanBoard {
  return {
    assignees: [],
    columns: [
      {
        name: 'triage',
        tasks: [{ id: 't_looped', status: 'triage', title: 'Needs a decision', ...task }]
      },
      { name: 'ready', tasks: [] }
    ],
    latest_event_id: 0,
    now: 0,
    tenants: []
  }
}

const LOOPED = { block_kind: 'needs_input', block_recurrences: 2 }

async function dragToReady() {
  await screen.findByText('Needs a decision')
  fireEvent.contextMenu(screen.getByText('Needs a decision'))

  const [moveTo] = await screen.findAllByText('moveTo')

  fireEvent.click(moveTo)
}

describe('needsBlockLoopAck — the predicate the board gates on', () => {
  it('is true only for a needs_input loop moving into the work queue', () => {
    const looped = { status: 'triage', ...LOOPED }

    expect(needsBlockLoopAck(looped, 'ready')).toBe(true)
    // `todo` too: recompute_ready() promotes a parent-satisfied todo card to
    // ready on the next tick, so a ready-only gate is bypassed by dropping the
    // card one lane to the left.
    expect(needsBlockLoopAck(looped, 'todo')).toBe(true)
  })

  it('is false for every other cause, count, source status, and target', () => {
    // Ordinary triage card — no block history at all.
    expect(needsBlockLoopAck({ status: 'triage' }, 'ready')).toBe(false)
    // Below the recurrence limit: one block + unblock is ordinary work.
    expect(needsBlockLoopAck({ block_kind: 'needs_input', block_recurrences: 1, status: 'triage' }, 'ready')).toBe(false)
    // A loop with a different cause — a tighter spec or a re-run may genuinely
    // fix those, so they stay ordinary triage cards (same scoping the backend
    // sweeps use).
    expect(needsBlockLoopAck({ block_kind: 'capability', block_recurrences: 2, status: 'triage' }, 'ready')).toBe(false)
    expect(needsBlockLoopAck({ block_kind: 'transient', block_recurrences: 2, status: 'triage' }, 'ready')).toBe(false)
    // Loop history on a card that is NOT parked in triage.
    expect(needsBlockLoopAck({ status: 'blocked', ...LOOPED }, 'ready')).toBe(false)
    // Targets outside the work queue are none of this guard's business.
    expect(needsBlockLoopAck({ status: 'triage', ...LOOPED }, 'done')).toBe(false)
    expect(needsBlockLoopAck({ status: 'triage', ...LOOPED }, 'idea')).toBe(false)
  })
})

describe('dragging a loop-broken triage card into the work queue', () => {
  it('asks for confirmation instead of moving it', async () => {
    fetchBoardMock.mockResolvedValue(boardWith(LOOPED))
    mount()

    await dragToReady()

    expect(await screen.findByText('blockLoopConfirmTitle')).toBeTruthy()
    // The whole point: nothing was written while the human is still deciding.
    expect(patchTaskMock).not.toHaveBeenCalled()
  })

  it('cancelling leaves the card exactly where it was', async () => {
    fetchBoardMock.mockResolvedValue(boardWith(LOOPED))
    mount()

    await dragToReady()
    await screen.findByText('blockLoopConfirmTitle')

    fireEvent.click(screen.getByText('cancel'))

    await waitFor(() => expect(screen.queryByText('blockLoopConfirmTitle')).toBeNull())
    expect(patchTaskMock).not.toHaveBeenCalled()
  })

  it('confirming re-sends the move WITH the acknowledgment the backend requires', async () => {
    fetchBoardMock.mockResolvedValue(boardWith(LOOPED))
    mount()

    await dragToReady()
    await screen.findByText('blockLoopConfirmTitle')

    fireEvent.click(screen.getByText('blockLoopConfirmAction'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalledTimes(1))

    const [id, patch] = patchTaskMock.mock.calls[0]

    expect(id).toBe('t_looped')
    expect(patch).toEqual({ acknowledge_block_loop: true, status: 'ready' })
  })
})

describe('cards the guard must not touch', () => {
  it('an ordinary triage card moves straight through, with no dialog', async () => {
    fetchBoardMock.mockResolvedValue(boardWith({}))
    mount()

    await dragToReady()

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalledTimes(1))
    // No acknowledgment field at all — the ordinary payload is unchanged.
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'ready' })
    expect(screen.queryByText('blockLoopConfirmTitle')).toBeNull()
  })

  it('a loop whose cause is not needs_input moves straight through', async () => {
    fetchBoardMock.mockResolvedValue(boardWith({ block_kind: 'capability', block_recurrences: 3 }))
    mount()

    await dragToReady()

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalledTimes(1))
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'ready' })
    expect(screen.queryByText('blockLoopConfirmTitle')).toBeNull()
  })
})
