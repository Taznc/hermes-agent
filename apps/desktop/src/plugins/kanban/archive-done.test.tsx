import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, ALL_BOARDS } from './api'
import { ArchiveDoneControl } from './board'

const { archiveDoneMock, notify, preflightMock } = vi.hoisted(() => ({
  archiveDoneMock: vi.fn(),
  notify: vi.fn(),
  preflightMock: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return {
    ...actual,
    host: { ...(actual.host as object), notify },
    usePluginI18n: () => (key: string) => key
  }
})

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    archiveDone: () => archiveDoneMock(),
    fetchArchiveDonePreflight: () => preflightMock()
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  preflightMock.mockReset().mockResolvedValue({ done_count: 2, scope: { kind: 'board', label: 'Shipping' } })
  archiveDoneMock.mockReset().mockResolvedValue({
    archived_count: 2,
    boards: ['shipping'],
    candidate_count: 2,
    failures: [],
    scope: { kind: 'board', label: 'Shipping' },
    skipped_count: 0
  })
  notify.mockReset()
  $boardSlug.set('shipping')
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

function mount() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')

  render(
    <QueryClientProvider client={client}>
      <ArchiveDoneControl />
    </QueryClientProvider>
  )

  return { invalidate }
}

async function openConfirmation() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'archiveDone' }).hasAttribute('disabled')).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'archiveDone' }))

  return screen.findByRole('dialog')
}

describe('Archive Done', () => {
  it('uses the individual-board preflight scope and confirms its completed-card count', async () => {
    mount()
    const dialog = await openConfirmation()

    expect(preflightMock).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByText('archiveDoneConfirm')).toBeTruthy()
  })

  it('uses the existing All Boards scope without client-side fan-out', async () => {
    $boardSlug.set(ALL_BOARDS)
    preflightMock.mockResolvedValue({ done_count: 3, scope: { kind: 'all_boards', label: 'All Boards' } })
    mount()
    const dialog = await openConfirmation()

    expect(within(dialog).getByText('archiveDoneConfirm')).toBeTruthy()
    fireEvent.click(within(dialog).getAllByRole('button', { name: 'archiveDone' })[0])
    await waitFor(() => expect(archiveDoneMock).toHaveBeenCalledTimes(1))
  })

  it('stays disabled when the preflight reports no completed cards', async () => {
    preflightMock.mockResolvedValue({ done_count: 0, scope: { kind: 'board', label: 'Shipping' } })
    mount()

    await waitFor(() => expect(screen.getByRole('button', { name: 'archiveDone' }).hasAttribute('disabled')).toBe(true))
    expect(archiveDoneMock).not.toHaveBeenCalled()
  })

  it('cancels without mutating', async () => {
    mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getByRole('button', { name: 'cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(archiveDoneMock).not.toHaveBeenCalled()
  })

  it('reconciles boards and badges immediately after success and reports the actual count', async () => {
    const { invalidate } = mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getAllByRole('button', { name: 'archiveDone' })[0])
    await waitFor(() => expect(archiveDoneMock).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board'] }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'boards'] })
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', message: 'archiveDoneSuccess' }))
  })

  it('surfaces partial failures without a success notification', async () => {
    archiveDoneMock.mockResolvedValue({
      archived_count: 1,
      boards: ['shipping', 'homelab'],
      candidate_count: 3,
      failures: [{ board: 'homelab', error: 'locked', task_id: 't_2' }],
      scope: { kind: 'all_boards', label: 'All Boards' },
      skipped_count: 1
    })
    const { invalidate } = mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getAllByRole('button', { name: 'archiveDone' })[0])
    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning', message: 'archiveDonePartial' })))
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board'] })
  })
})
