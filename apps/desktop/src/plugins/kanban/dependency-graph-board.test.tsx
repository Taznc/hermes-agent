/**
 * Board-level interplay between the dependency trace, the Full chain toggle,
 * and the graph dialog: one Esc closes one layer (dialog first, then the
 * trace), closing the dialog keeps the trace, and Full chain widens the lit
 * set from direct neighbours to the transitive chain.
 *
 * Mounts the real KanbanBoardPage; the data layer (./api) is mocked at the
 * module boundary, same shape as board.all-boards.test.tsx.
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

/** gp → p → f → c: a four-card chain, all in todo. */
function chainBoard(): KanbanBoard {
  const task = (id: string, title: string) => ({ id, status: 'todo', title })

  return {
    assignees: [],
    columns: [
      {
        name: 'todo',
        tasks: [task('gp', 'Grandparent'), task('p', 'Parent'), task('f', 'Focus'), task('c', 'Child')]
      },
      { name: 'ready', tasks: [] },
      { name: 'running', tasks: [] },
      { name: 'blocked', tasks: [] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [] }
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

async function mount() {
  fetchBoardMock.mockResolvedValue(chainBoard())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )

  await screen.findByText('Focus')
}

/** The card container for a title — the draggable node the rings live on. */
const cardOf = (title: string) => screen.getByText(title).closest('[draggable="true"]') as HTMLElement

/** A trace is live iff the focus hint bar is up. `hidden: true` because an
 *  open Radix modal marks everything outside it aria-hidden. The focused
 *  card's own trace button shares the label, hence getAll. */
const tracing = () => screen.queryAllByRole('button', { hidden: true, name: 'depClearFocus' }).length > 0

/** Buttons are labelled by the dotted i18n key (usePluginI18n is echoed). */
const buttonsIn = (el: HTMLElement, label: RegExp) =>
  Array.from(el.querySelectorAll('button')).filter(b => label.test(b.getAttribute('aria-label') ?? ''))

describe('dependency graph dialog on the board', () => {
  it('Esc closes the dialog first and keeps the trace; a second Esc clears the trace', async () => {
    await mount()

    fireEvent.click(buttonsIn(cardOf('Focus'), /depGraphHint/)[0])

    await screen.findByRole('dialog')
    expect(tracing()).toBe(true)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(tracing()).toBe(true)

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(tracing()).toBe(false))
  })

  it('Full chain lights the grandparent that Direct links leaves dimmed', async () => {
    await mount()

    fireEvent.click(buttonsIn(cardOf('Focus'), /depFocusHint/)[0])
    await waitFor(() => expect(tracing()).toBe(true))

    const dimmed = (title: string) => cardOf(title).classList.contains('opacity-35')

    // Direct: parent lit, grandparent dimmed.
    expect(dimmed('Parent')).toBe(false)
    expect(dimmed('Grandparent')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'depFocusChain' }))

    await waitFor(() => expect(dimmed('Grandparent')).toBe(false))
  })
})
