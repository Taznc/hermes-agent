import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, ALL_BOARDS, type ArchiveDoneResult, bindApi } from './api'
import { ArchiveDoneControl } from './board'

const { notify, translate } = vi.hoisted(() => ({
  notify: vi.fn(),
  translate: vi.fn((key: string, ...args: unknown[]) => `${key}(${args.join(',')})`)
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
    usePluginI18n: () => translate
  }
})

const storage = {
  get: <T,>(_key: string, fallback: T) => fallback,
  remove: vi.fn(),
  set: vi.fn()
}

let disposeApi: () => void
let rest: ReturnType<typeof vi.fn>

let mutationResult: ArchiveDoneResult = {
  archived_count: 2,
  boards: ['shipping'],
  candidate_count: 2,
  failures: [],
  scope: { kind: 'board' as const, label: 'Shipping' },
  skipped_count: 0
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  mutationResult = {
    archived_count: 2,
    boards: ['shipping'],
    candidate_count: 2,
    failures: [],
    scope: { kind: 'board', label: 'Shipping' },
    skipped_count: 0
  }
  rest = vi.fn((path: string, options?: { method?: string }) => {
    const allBoards = path.includes('boards=*')

    if (path.includes('/preflight')) {
      return Promise.resolve({
        done_count: allBoards ? 3 : 2,
        scope: allBoards ? { kind: 'all_boards', label: 'All Boards' } : { kind: 'board', label: 'Shipping' }
      })
    }

    if (options?.method === 'POST') {
      return Promise.resolve(mutationResult)
    }

    return Promise.reject(new Error(`unexpected request: ${path}`))
  })
  disposeApi = bindApi(rest as Parameters<typeof bindApi>[0], storage, () => vi.fn())
  notify.mockReset()
  translate.mockClear()
  $boardSlug.set('shipping')
})

afterEach(() => {
  cleanup()
  disposeApi()
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
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'archiveDone()' }).hasAttribute('disabled')).toBe(false)
  )
  fireEvent.click(screen.getByRole('button', { name: 'archiveDone()' }))

  return screen.findByRole('dialog')
}

describe('Archive Done', () => {
  it('uses the individual-board REST scope and renders its completed-card count', async () => {
    mount()
    const dialog = await openConfirmation()

    expect(rest).toHaveBeenCalledWith('/tasks/archive-done/preflight?board=shipping', undefined)
    expect(within(dialog).getByText('archiveDoneConfirm(2,Shipping)')).toBeTruthy()

    fireEvent.click(within(dialog).getByRole('button', { name: 'archiveDone()' }))
    await waitFor(() => expect(rest).toHaveBeenCalledWith('/tasks/archive-done?board=shipping', { method: 'POST' }))
  })

  it('uses the existing All Boards REST scope without client-side fan-out', async () => {
    $boardSlug.set(ALL_BOARDS)
    mount()
    const dialog = await openConfirmation()

    expect(rest).toHaveBeenCalledWith('/tasks/archive-done/preflight?boards=*', undefined)
    expect(within(dialog).getByText('archiveDoneConfirm(3,All Boards)')).toBeTruthy()

    fireEvent.click(within(dialog).getByRole('button', { name: 'archiveDone()' }))
    await waitFor(() => expect(rest).toHaveBeenCalledWith('/tasks/archive-done?boards=*', { method: 'POST' }))
    expect(rest.mock.calls.filter(([path]) => String(path).includes('/tasks/archive-done?'))).toHaveLength(1)
  })

  it('stays disabled when the preflight reports no completed cards', async () => {
    rest.mockImplementation((path: string) =>
      Promise.resolve({ done_count: 0, scope: { kind: 'board', label: 'Shipping' }, path })
    )
    mount()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'archiveDone()' }).hasAttribute('disabled')).toBe(true)
    )
    expect(rest).toHaveBeenCalledWith('/tasks/archive-done/preflight?board=shipping', undefined)
  })

  it('cancels without mutating', async () => {
    mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(rest.mock.calls.some(([, options]) => (options as { method?: string } | undefined)?.method === 'POST')).toBe(
      false
    )
  })

  it('reconciles boards and badges immediately after success and reports the actual count', async () => {
    const { invalidate } = mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getByRole('button', { name: 'archiveDone()' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board'] }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'boards'] })
    expect(notify).toHaveBeenCalledWith({ kind: 'success', message: 'archiveDoneSuccess(2)' })
  })

  it('surfaces partial failures without a success notification', async () => {
    mutationResult = {
      archived_count: 1,
      boards: ['shipping', 'homelab'],
      candidate_count: 3,
      failures: [{ board: 'homelab', error: 'locked', task_id: 't_2' }],
      scope: { kind: 'all_boards', label: 'All Boards' },
      skipped_count: 1
    }
    const { invalidate } = mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getByRole('button', { name: 'archiveDone()' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'warning', message: 'archiveDonePartial(1,1,1)' }))
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board'] })
  })

  it('keeps the confirmation open with its inline error when archiving fails', async () => {
    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path.includes('/preflight')) {
        return Promise.resolve({ done_count: 2, scope: { kind: 'board', label: 'Shipping' } })
      }

      return options?.method === 'POST'
        ? Promise.reject(new Error('archive unavailable'))
        : Promise.reject(new Error('unexpected request'))
    })
    mount()
    const dialog = await openConfirmation()

    fireEvent.click(within(dialog).getByRole('button', { name: 'archiveDone()' }))

    expect(await within(dialog).findByText('archive unavailable')).toBeTruthy()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
  })
})
