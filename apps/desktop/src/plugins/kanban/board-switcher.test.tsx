import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type * as KanbanApi from './api'
import { $boardSlug, ALL_BOARDS } from './api'
import { BoardSwitcher } from './board-switcher'

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  fetchBoards: vi.fn(async () => ({
    boards: [
      { name: 'Shipping', project_id: null, slug: 'shipping', total: 3 },
      { name: 'Homelab', project_id: null, slug: 'homelab', total: 2 }
    ],
    current: 'shipping'
  }))
}))

// usePluginI18n echoes the dotted key (same shim other kanban tests use) so
// assertions target stable keys instead of translated English text — except
// for "All Boards", asserted on the real English copy since it IS the label
// under test (board-switcher.test.tsx's own convention pre-dates this file).
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => (key === 'allBoards' ? 'All Boards' : key) }
})

// Radix dropdown internals jsdom doesn't implement.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

const mount = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <BoardSwitcher />
    </QueryClientProvider>
  )

const openMenu = async () => {
  const trigger = await screen.findByRole('button')

  // Radix's dropdown trigger opens on pointerdown (a synthetic 'click' fireEvent
  // alone won't do it), so fire the full mouse sequence a real click produces —
  // same technique as project-menu.test.tsx (#67500).
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

describe('board switcher', () => {
  // The rename and settings dialogs stay mounted while closed, so they render
  // with a null board on every pass. Reading the slug inside their mutation
  // callback used to crash the whole contribution, because the React Compiler
  // lifts a callback's property reads into its render-time dependency check.
  it('renders while its dialogs are closed', async () => {
    mount()

    expect(await screen.findByText('Shipping')).toBeTruthy()
  })

  it('an "All Boards" entry sits at the top of the list and shows the summed live count', async () => {
    mount()
    await openMenu()

    const allBoardsRow = await screen.findByText('All Boards')

    expect(allBoardsRow).toBeTruthy()
    // 3 (shipping) + 2 (homelab) — the summed live card count across boards.
    expect(screen.getByText('5')).toBeTruthy()
  })

  it('selecting "All Boards" sets $boardSlug to the sentinel', async () => {
    mount()
    await openMenu()

    fireEvent.click(await screen.findByText('All Boards'))

    expect($boardSlug.get()).toBe(ALL_BOARDS)
  })

  it('board-only actions (rename, settings, export, delete) are hidden while the sentinel is active', async () => {
    $boardSlug.set(ALL_BOARDS)
    mount()
    await openMenu()

    // The trigger itself reads "All Boards", not a real board name.
    expect(screen.getAllByText('All Boards').length).toBeGreaterThan(0)

    // Both real boards are still listed as selectable rows...
    expect(screen.getByText('Shipping')).toBeTruthy()
    expect(screen.getByText('Homelab')).toBeTruthy()

    // ...but every board-scoped action, which only renders when `current` is
    // resolved (never true for the sentinel), is absent.
    expect(screen.queryByText('renameDots')).toBeNull()
    expect(screen.queryByText('settingsDots')).toBeNull()
    expect(screen.queryByText('exportDots')).toBeNull()
    expect(screen.queryByText('delete')).toBeNull()

    // New board + import stay available — they aren't board-scoped.
    expect(screen.getByText('newBoardDots')).toBeTruthy()
    expect(screen.getByText('importDots')).toBeTruthy()
  })

  it('picking a real board from the sentinel clears it back to a concrete slug', async () => {
    $boardSlug.set(ALL_BOARDS)
    mount()
    await openMenu()

    fireEvent.click(screen.getByText('Homelab'))

    expect($boardSlug.get()).toBe('homelab')
  })
})
