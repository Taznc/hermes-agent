/**
 * Dispatch pause control — the maintenance drain panel.
 *
 * Behaviour contracts: the control renders the board's live pause state and
 * running count from the API, pause/resume hit the right board-scoped
 * endpoints, and the "safe to restart" signal only appears once the board has
 * actually drained to zero running workers.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, bindApi } from './api'
import { DispatchPauseControl } from './orchestration'

const { notify, translate } = vi.hoisted(() => ({
  notify: vi.fn(),
  translate: vi.fn((key: string, ...args: unknown[]) => `${key}(${args.join(',')})`)
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

interface StatusPayload {
  paused: boolean
  state: null | Record<string, unknown>
  running_count: number
  message: null | string
}

let disposeApi: () => void
let rest: ReturnType<typeof vi.fn>
let status: StatusPayload

const RUNNING: StatusPayload = { message: null, paused: false, running_count: 2, state: null }

const PAUSED: StatusPayload = {
  message: 'paused for maintenance (by=claudecode; note=gateway restart)',
  paused: true,
  running_count: 2,
  state: { note: 'gateway restart', paused_at: 1_788_800_000, paused_by: 'claudecode', reason: 'operator_paused' }
}

beforeEach(() => {
  status = { ...RUNNING }
  rest = vi.fn((path: string, options?: { method?: string }) => {
    if (path.startsWith('/dispatch/status')) {
      return Promise.resolve(status)
    }

    if (path.startsWith('/dispatch/pause') && options?.method === 'POST') {
      status = { ...PAUSED }

      return Promise.resolve({ paused: true, state: PAUSED.state })
    }

    if (path.startsWith('/dispatch/resume') && options?.method === 'POST') {
      status = { ...RUNNING }

      return Promise.resolve({ previous: PAUSED.state, resumed: true, was_paused: true })
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

  render(
    <QueryClientProvider client={client}>
      <DispatchPauseControl />
    </QueryClientProvider>
  )
}

describe('Dispatch pause control', () => {
  it('reads the selected board status and offers Pause while dispatch is running', async () => {
    mount()

    await waitFor(() => expect(rest).toHaveBeenCalledWith('/dispatch/status?board=shipping', undefined))
    expect(await screen.findByRole('button', { name: 'pauseDispatch()' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'resumeDispatch()' })).toBeNull()
  })

  it('shows the running count as still draining while paused workers remain', async () => {
    status = { ...PAUSED, running_count: 2 }
    mount()

    expect(await screen.findByText('draining(2)')).toBeTruthy()
    expect(screen.queryByText('safeToRestart()')).toBeNull()
  })

  it('does not call a normally-dispatching board draining', async () => {
    // 2 running with dispatch live is ordinary throughput, not a drain — saying
    // "draining" there would tell the operator a restart is pending when it is not.
    mount()

    expect(await screen.findByText('dispatchRunning()')).toBeTruthy()
    expect(screen.queryByText(/^draining/)).toBeNull()
    expect(screen.queryByText('safeToRestart()')).toBeNull()
  })

  it('reports safe to restart only once the board has drained to zero', async () => {
    status = { ...PAUSED, running_count: 0 }
    mount()

    expect(await screen.findByText('safeToRestart()')).toBeTruthy()
    expect(screen.queryByText(/^draining/)).toBeNull()
  })

  it('pauses the selected board through the board-scoped endpoint', async () => {
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'pauseDispatch()' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/pause?board=shipping', {
        body: { note: null },
        method: 'POST'
      })
    )
    expect(await screen.findByRole('button', { name: 'resumeDispatch()' })).toBeTruthy()
  })

  it('renders the paused reason and note so the operator knows why', async () => {
    status = { ...PAUSED }
    mount()

    expect(await screen.findByText(PAUSED.message!)).toBeTruthy()
  })

  it('resumes through the board-scoped endpoint and returns to the Pause affordance', async () => {
    status = { ...PAUSED }
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'resumeDispatch()' }))

    await waitFor(() => expect(rest).toHaveBeenCalledWith('/dispatch/resume?board=shipping', { method: 'POST' }))
    expect(await screen.findByRole('button', { name: 'pauseDispatch()' })).toBeTruthy()
  })

  it('surfaces a failed pause instead of showing a board as paused', async () => {
    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path.startsWith('/dispatch/status')) {
        return Promise.resolve(status)
      }

      return options?.method === 'POST'
        ? Promise.reject(new Error('dispatch tick in progress'))
        : Promise.reject(new Error('unexpected request'))
    })
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'pauseDispatch()' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'error', message: 'dispatch tick in progress' }))
    expect(screen.getByRole('button', { name: 'pauseDispatch()' })).toBeTruthy()
  })

  it('reports a refused pause as not paused rather than silently succeeding', async () => {
    // The backend refuses a contended board with {paused: false} and HTTP 200 —
    // an operator must never read that as a drained board.
    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path.startsWith('/dispatch/status')) {
        return Promise.resolve(status)
      }

      if (options?.method === 'POST') {
        return Promise.resolve({ paused: false, reason: 'dispatch_in_progress', state: null })
      }

      return Promise.reject(new Error('unexpected request'))
    })
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'pauseDispatch()' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'warning', message: 'pauseBusy()' }))
    expect(screen.getByRole('button', { name: 'pauseDispatch()' })).toBeTruthy()
  })
})
