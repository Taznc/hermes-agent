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
  all_paused?: boolean
  board_count?: number
  paused_count?: number
  post_drain?: null | Record<string, unknown>
  post_drain_actions?: Array<{ action_kind: string; targets: string[] }>
}

let disposeApi: () => void
let rest: ReturnType<typeof vi.fn>
let status: StatusPayload

const RUNNING: StatusPayload = { message: null, paused: false, running_count: 2, state: null }

const ALL_RUNNING: StatusPayload = {
  all_paused: false,
  board_count: 2,
  message: null,
  paused: false,
  paused_count: 0,
  running_count: 2,
  state: null
}

const ALL_PAUSED: StatusPayload = {
  all_paused: true,
  board_count: 2,
  message: null,
  paused: true,
  paused_count: 2,
  running_count: 2,
  state: null
}

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
      if (path.includes('boards=*')) {
        status = { ...ALL_PAUSED }

        return Promise.resolve({ board_count: 2, failures: [], paused: true, paused_count: 2, results: [] })
      }

      status = { ...PAUSED }

      return Promise.resolve({ paused: true, state: PAUSED.state })
    }

    if (path.startsWith('/dispatch/resume') && options?.method === 'POST') {
      if (path.includes('boards=*')) {
        status = { ...ALL_RUNNING }

        return Promise.resolve({ board_count: 2, failures: [], resumed: true, resumed_count: 2, results: [] })
      }

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

  it('reports a refused resume as still paused rather than silently succeeding', async () => {
    // Symmetric to the refused pause: the backend refuses a contended board
    // with {resumed: false} and HTTP 200. Treating that as success would leave
    // the operator believing a drained board is claiming work again.
    status = { ...PAUSED }
    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path.startsWith('/dispatch/status')) {
        return Promise.resolve(status)
      }

      if (options?.method === 'POST') {
        return Promise.resolve({ previous: PAUSED.state, reason: 'dispatch_in_progress', resumed: false, was_paused: true })
      }

      return Promise.reject(new Error('unexpected request'))
    })
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'resumeDispatch()' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'warning', message: 'resumeBusy()' }))
    expect(screen.getByRole('button', { name: 'resumeDispatch()' })).toBeTruthy()
  })

  it('pauses and resumes every board while All Boards is selected', async () => {
    $boardSlug.set('*')
    status = { ...ALL_RUNNING }
    mount()

    await waitFor(() => expect(rest).toHaveBeenCalledWith('/dispatch/status?boards=*', undefined))
    const pauseAll = await screen.findByRole('button', { name: 'pauseAllBoards()' })
    const resumeAll = screen.getByRole('button', { name: 'resumeAllBoards()' })

    expect(pauseAll.hasAttribute('disabled')).toBe(false)
    expect(resumeAll.hasAttribute('disabled')).toBe(true)

    fireEvent.click(pauseAll)

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/pause?boards=*', {
        body: { note: null },
        method: 'POST'
      })
    )
    await waitFor(() => expect(resumeAll.hasAttribute('disabled')).toBe(false))

    fireEvent.click(resumeAll)

    await waitFor(() => expect(rest).toHaveBeenCalledWith('/dispatch/resume?boards=*', { method: 'POST' }))
  })

  it('fans out to explicit boards when the backend predates aggregate dispatch responses', async () => {
    $boardSlug.set('*')
    let legacyPaused = false

    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === '/boards') {
        return Promise.resolve({
          boards: [
            { name: 'Shipping', slug: 'shipping' },
            { name: 'Homelab', slug: 'homelab' }
          ],
          current: 'shipping'
        })
      }

      if (path === '/dispatch/status?boards=*') {
        return Promise.resolve({ ...(legacyPaused ? PAUSED : RUNNING) })
      }

      if (path.startsWith('/dispatch/status?board=')) {
        return Promise.resolve({ ...(legacyPaused ? PAUSED : RUNNING), running_count: 1 })
      }

      if (path === '/dispatch/pause?boards=*' && options?.method === 'POST') {
        legacyPaused = true

        return Promise.resolve({ paused: true, state: PAUSED.state })
      }

      if (path.startsWith('/dispatch/pause?board=') && options?.method === 'POST') {
        legacyPaused = true

        return Promise.resolve({ paused: true, state: PAUSED.state })
      }

      if (path.startsWith('/dispatch/resume?board=') && options?.method === 'POST') {
        legacyPaused = false

        return Promise.resolve({ resumed: true, was_paused: true })
      }

      return Promise.reject(new Error(`unexpected request: ${path}`))
    })
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'pauseAllBoards()' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/pause?board=shipping', {
        body: { note: null },
        method: 'POST'
      })
    )
    expect(rest).not.toHaveBeenCalledWith('/dispatch/pause?boards=*', expect.anything())
    expect(rest).toHaveBeenCalledWith('/dispatch/pause?board=homelab', {
      body: { note: null },
      method: 'POST'
    })

    fireEvent.click(await screen.findByRole('button', { name: 'resumeAllBoards()' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/resume?board=shipping', {
        method: 'POST'
      })
    )
    expect(rest).not.toHaveBeenCalledWith('/dispatch/resume?boards=*', expect.anything())
    expect(rest).toHaveBeenCalledWith('/dispatch/resume?board=homelab', {
      method: 'POST'
    })
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

/**
 * The "after drain" action queue.
 *
 * The trigger itself is server-side (the dispatcher tick fires it with no
 * browser connected), so these contracts are about the operator's half: queue
 * the intent against the right scope, render what is armed alongside the live
 * running count and time remaining, require a second step before a reboot, and
 * never present a queued action as fired.
 */
describe('Post-drain action queue', () => {
  /** Radix's dropdown trigger opens on pointerdown — a synthetic `click` alone
   *  won't do it, so fire the full mouse sequence a real click produces (same
   *  technique as board-switcher.test.tsx / project-menu.test.tsx, #67500). */
  const openAfterDrainMenu = async () => {
    const trigger = await screen.findByRole('button', { name: 'queuePostDrain()' })

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
  }

  const ACTIONS = [
    { action_kind: 'service_restart', targets: ['hermes-gateway.service'] },
    { action_kind: 'reboot', targets: [] }
  ]

  const QUEUED = {
    action_kind: 'reboot',
    expires_in_seconds: 2_520,
    expires_at: 1_788_802_520,
    requested_at: 1_788_800_000,
    requested_by: 'claudecode',
    state: 'waiting',
    target: null
  }

  beforeEach(() => {
    status = { ...PAUSED, post_drain: null, post_drain_actions: ACTIONS, running_count: 3 }
  })

  it('offers the after-drain selector next to the existing pause control', async () => {
    mount()

    expect(await screen.findByRole('button', { name: 'queuePostDrain()' })).toBeTruthy()
  })

  it('only offers actions this host will actually accept', async () => {
    // The backend rejects service_restart when no unit is allowlisted, so
    // offering it here would render a button that can only ever 400.
    status = { ...status, post_drain_actions: [{ action_kind: 'reboot', targets: [] }] }
    mount()

    await openAfterDrainMenu()

    expect(await screen.findByRole('menuitem', { name: 'actionReboot()' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /actionServiceRestart/ })).toBeNull()
  })

  it('queues a service restart in one click', async () => {
    mount()

    await openAfterDrainMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionServiceRestart(hermes-gateway.service)' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?board=shipping', {
        body: { action_kind: 'service_restart', expires_in_seconds: null, target: 'hermes-gateway.service' },
        method: 'POST'
      })
    )
  })

  it('queues an allowlisted script by name, and labels it as a script', async () => {
    // The third kind must be labelled by its OWN string rather than falling
    // through to whatever the last branch happened to be — a `run_script`
    // entry rendered as "reboot this machine" is the dangerous confusion.
    status = {
      ...status,
      post_drain_actions: [
        { action_kind: 'run_script', targets: ['fork-sync', 'log-rotate'] },
        { action_kind: 'reboot', targets: [] }
      ]
    }
    mount()

    await openAfterDrainMenu()

    expect(await screen.findByRole('menuitem', { name: 'actionRunScript(fork-sync)' })).toBeTruthy()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionRunScript(log-rotate)' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?board=shipping', {
        body: { action_kind: 'run_script', expires_in_seconds: null, target: 'log-rotate' },
        method: 'POST'
      })
    )
  })

  it('reports a settled script failure under the script label', async () => {
    status = {
      ...status,
      post_drain: {
        ...QUEUED,
        action_kind: 'run_script',
        error: 'script exited 1: remote rejected the push',
        state: 'failed',
        target: 'fork-sync'
      }
    }
    mount()

    expect(
      await screen.findByText(
        'postDrainFailed(actionRunScript(fork-sync),script exited 1: remote rejected the push)'
      )
    ).toBeTruthy()
  })

  it('falls back to the raw kind id when a newer backend offers an unknown one', async () => {
    // The backend's registry is open, so an unknown kind must render as itself
    // rather than being mislabelled as one of the kinds this build knows.
    status = { ...status, post_drain_actions: [{ action_kind: 'defrag_the_cache', targets: [] }] }
    mount()

    await openAfterDrainMenu()

    expect(await screen.findByRole('menuitem', { name: 'defrag_the_cache' })).toBeTruthy()
  })

  it('offers every allowlisted restart target, not just the first', async () => {
    // The catalog carries one row per KIND with all its allowlisted units. A
    // selector that only ever submitted `targets[0]` would leave every later
    // unit in the operator's config unreachable from the Desktop.
    status = {
      ...status,
      post_drain_actions: [
        { action_kind: 'service_restart', targets: ['hermes-gateway.service', 'hermes-webdesktop.service'] },
        { action_kind: 'reboot', targets: [] }
      ]
    }
    mount()

    await openAfterDrainMenu()

    expect(await screen.findByRole('menuitem', { name: 'actionServiceRestart(hermes-gateway.service)' })).toBeTruthy()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionServiceRestart(hermes-webdesktop.service)' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?board=shipping', {
        body: { action_kind: 'service_restart', expires_in_seconds: null, target: 'hermes-webdesktop.service' },
        method: 'POST'
      })
    )
  })

  it('requires an explicit second step before arming a reboot', async () => {
    mount()

    await openAfterDrainMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionReboot()' }))

    // The first click only asks. Arming a host reboot on a single menu click is
    // the mistake this confirmation exists to prevent.
    expect(rest).not.toHaveBeenCalledWith(expect.stringContaining('/dispatch/post-drain'), expect.anything())
    expect(await screen.findByText('confirmRebootPrompt()')).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'confirmReboot()' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?board=shipping', {
        body: { action_kind: 'reboot', expires_in_seconds: null, target: null },
        method: 'POST'
      })
    )
  })

  it('abandons an unconfirmed reboot without queueing anything', async () => {
    mount()

    await openAfterDrainMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionReboot()' }))
    fireEvent.click(await screen.findByRole('button', { name: 'cancelConfirm()' }))

    await waitFor(() => expect(screen.queryByText('confirmRebootPrompt()')).toBeNull())
    expect(rest).not.toHaveBeenCalledWith(expect.stringContaining('/dispatch/post-drain'), expect.anything())
  })

  it('shows the armed action with the live running count and time remaining', async () => {
    status = { ...status, post_drain: QUEUED, running_count: 3 }
    mount()

    // One line, visible without hover: what fires, how far from firing, and
    // how long the operator has before it expires instead.
    expect(await screen.findByText('postDrainArmed(actionReboot(),3,42m)')).toBeTruthy()
  })

  it('reports a drained board as about to fire rather than as still waiting', async () => {
    status = { ...status, post_drain: QUEUED, running_count: 0 }
    mount()

    expect(await screen.findByText('postDrainArmedDrained(actionReboot(),42m)')).toBeTruthy()
  })

  it('offers Cancel while an action is waiting and clears it through the scoped endpoint', async () => {
    status = { ...status, post_drain: QUEUED }
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'cancelPostDrain()' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?board=shipping', { method: 'DELETE' })
    )
  })

  it('does not offer Cancel once the action has already fired', async () => {
    // Cancelling is only meaningful while waiting; a fired action is history.
    status = { ...status, post_drain: { ...QUEUED, state: 'firing' } }
    mount()

    await screen.findByText(/^postDrainFiring/)
    expect(screen.queryByRole('button', { name: 'cancelPostDrain()' })).toBeNull()
  })

  it('reports a failed action instead of leaving the operator to assume it worked', async () => {
    status = {
      ...status,
      post_drain: { ...QUEUED, error: 'unit did not come back active', state: 'failed' }
    }
    mount()

    expect(await screen.findByText('postDrainFailed(actionReboot(),unit did not come back active)')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'cancelPostDrain()' })).toBeNull()
  })

  it('reports an expired action as expired, never as fired', async () => {
    status = { ...status, post_drain: { ...QUEUED, expires_in_seconds: 0, state: 'expired' } }
    mount()

    expect(await screen.findByText('postDrainExpired(actionReboot())')).toBeTruthy()
  })

  it('does not offer the selector while an action is already queued', async () => {
    status = { ...status, post_drain: QUEUED }
    mount()

    await screen.findByRole('button', { name: 'cancelPostDrain()' })
    expect(screen.queryByRole('button', { name: 'queuePostDrain()' })).toBeNull()
  })

  it('queues and cancels across every board while All Boards is selected', async () => {
    $boardSlug.set('*')
    status = { ...ALL_PAUSED, post_drain: null, post_drain_actions: ACTIONS, running_count: 3 }
    mount()

    await openAfterDrainMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionServiceRestart(hermes-gateway.service)' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?boards=*', {
        body: { action_kind: 'service_restart', expires_in_seconds: null, target: 'hermes-gateway.service' },
        method: 'POST'
      })
    )

    status = { ...status, post_drain: QUEUED }
    fireEvent.click(await screen.findByRole('button', { name: 'cancelPostDrain()' }))

    await waitFor(() => expect(rest).toHaveBeenCalledWith('/dispatch/post-drain?boards=*', { method: 'DELETE' }))
  })

  it('surfaces a rejected queue request instead of showing an action as armed', async () => {
    rest.mockImplementation((path: string, options?: { method?: string }) => {
      if (path.startsWith('/dispatch/status')) {
        return Promise.resolve(status)
      }

      return options?.method === 'POST'
        ? Promise.reject(new Error('service_restart target is not in the allowlist'))
        : Promise.reject(new Error('unexpected request'))
    })
    mount()

    await openAfterDrainMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'actionServiceRestart(hermes-gateway.service)' }))

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith({
        kind: 'error',
        message: 'service_restart target is not in the allowlist'
      })
    )
    expect(screen.queryByRole('button', { name: 'cancelPostDrain()' })).toBeNull()
  })

  it('reuses the existing status poll rather than adding a second loop', async () => {
    // The card is explicit: one 8s cadence for the whole panel. A dedicated
    // queue poll would double the request rate on an idle dashboard.
    status = { ...status, post_drain: QUEUED }
    mount()

    await screen.findByRole('button', { name: 'cancelPostDrain()' })

    const queueReads = rest.mock.calls.filter(
      call => String(call[0]).startsWith('/dispatch/post-drain') && (call[1]?.method ?? 'GET') === 'GET'
    )

    expect(queueReads).toEqual([])
  })
})
