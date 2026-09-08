import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as KanbanApi from './api'
import { KanbanCount } from './plugin'

const { fetchBoard } = vi.hoisted(() => ({
  fetchBoard: vi.fn(async () => ({ columns: [{ name: 'running', tasks: [{ id: 'task-1' }] }] }))
}))

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  fetchBoard: (...args: unknown[]) => fetchBoard(...(args as [])),
  fetchAllBoards: (...args: unknown[]) => fetchBoard(...(args as []))
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  fetchBoard.mockResolvedValue({ columns: [{ name: 'running', tasks: [{ id: 'task-1' }] }] })
  window.location.hash = '#/'
})

const mount = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <KanbanCount />
    </QueryClientProvider>
  )

describe('Kanban statusbar toggle', () => {
  it('returns to the route and query string from which the board was opened', async () => {
    window.location.hash = '#/session-1?view=workspace'
    mount()
    const count = await screen.findByRole('button')

    fireEvent.click(count)
    expect(window.location.hash).toBe('#/kanban')
    fireEvent.click(count)
    expect(window.location.hash).toBe('#/session-1?view=workspace')

    window.location.hash = '#/skills'
    fireEvent.click(count)
    fireEvent.click(count)
    expect(window.location.hash).toBe('#/skills')
  })

  it('returns to the workspace when mounted on the board without a prior route', async () => {
    window.location.hash = '#/kanban?board=shipping'
    mount()

    fireEvent.click(await screen.findByRole('button'))

    expect(window.location.hash).toBe('#/')
  })

  it.each(['profile', 'connectionId'] as const)('does not restore a route after the %s changes', async key => {
    const scope = vi.spyOn(host.state[key], 'get').mockReturnValue('first')
    window.location.hash = '#/session-1'
    mount()
    const count = await screen.findByRole('button')
    fireEvent.click(count)
    scope.mockReturnValue('second')

    fireEvent.click(count)

    expect(window.location.hash).toBe('#/')
  })
})

/**
 * The statusbar pill is the app's one always-visible fleet signal, so what
 * counts as "in flight" is a contract, not an implementation detail: exactly
 * `running + ready`. The wishlist lanes must never reach it — a 200-card
 * roadmap would otherwise light the pill on a completely idle fleet, which is
 * the same false-urgency the Ideas/Roadmap lanes exist to avoid.
 */
describe('Kanban statusbar count — what counts as in flight', () => {
  it('counts running + ready and nothing else', async () => {
    fetchBoard.mockResolvedValue({
      columns: [
        { name: 'idea', tasks: [{ id: 'i-1' }, { id: 'i-2' }] },
        { name: 'roadmap', tasks: [{ id: 'r-1' }] },
        { name: 'triage', tasks: [{ id: 't-1' }] },
        { name: 'ready', tasks: [{ id: 'y-1' }, { id: 'y-2' }] },
        { name: 'running', tasks: [{ id: 'n-1' }] },
        { name: 'done', tasks: [{ id: 'd-1' }] }
      ]
    })

    mount()

    expect(await screen.findByText('3')).toBeTruthy()
  })

  it('stays hidden on a board whose only cards are wishlist cards', async () => {
    fetchBoard.mockResolvedValue({
      columns: [
        { name: 'idea', tasks: Array.from({ length: 120 }, (_, i) => ({ id: `i-${i}` })) },
        { name: 'roadmap', tasks: Array.from({ length: 80 }, (_, i) => ({ id: `r-${i}` })) },
        { name: 'ready', tasks: [] },
        { name: 'running', tasks: [] }
      ]
    })

    mount()

    // The pill renders nothing at all when nothing is in flight; give the
    // query a tick to resolve so this isn't just asserting on the loading gap.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(screen.queryByRole('button')).toBeNull()
  })
})
