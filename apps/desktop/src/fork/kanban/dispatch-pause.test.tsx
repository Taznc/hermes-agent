/**
 * Dispatch pause/resume, end to end on upstream's real Kanban page and its
 * orchestration panel, wired through `host.fork.kanban` exactly as production
 * is (the `kanban-dispatch-pause` anchor in orchestration.tsx and the
 * `boardOverlay` anchor in board.tsx). Only the network doors are faked:
 * upstream's plugin REST door and the fork backend's `pluginRest('fork-kanban', …)`,
 * which here holds real per-board pause state so a click round-trips.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PluginRestOptions } from '@/api/plugins'
import { registerPluginLocales } from '@/i18n/plugin-i18n'
import { $boardSlug, bindApi } from '@/plugins/kanban/api'
import { KanbanBoardPage } from '@/plugins/kanban/board'
import { KANBAN_LOCALES } from '@/plugins/kanban/i18n'
import { $notifications, clearNotifications } from '@/store/notifications'

import { ALL_BOARDS } from './all-boards'
import { resetForkBackend } from './backend'
import { dispatchPath, type DispatchStatus, drainText } from './dispatch-pause'
import { resetFocus } from './state'
import type { KanbanText } from './text'

const NOT_FOUND = "Error invoking remote method 'hermes:api': Error: 404: {\"detail\":\"Not Found\"}"

// ── fake fork backend with real per-board pause state ────────────────────────

const BOARDS = ['alpha', 'beta']
let forkUp = true
let paused = new Set<string>()
let running: Record<string, number> = {}
let estop: null | { reason: string } = null
let busyNext = false
let unreadable = new Set<string>()

function boardStatus(board: string) {
  return {
    board,
    name: board,
    paused: paused.has(board),
    running_count: running[board] ?? 0,
    state: paused.has(board) ? { reason: 'operator_paused' } : null
  }
}

function targets(query: string): string[] {
  const board = new URLSearchParams(query).get('board')

  return board === '*' ? BOARDS : [board || 'alpha']
}

const forkRest = vi.fn(async (_plugin: string, path: string, opts?: PluginRestOptions): Promise<unknown> => {
  if (!forkUp) {
    throw new Error(NOT_FOUND)
  }

  const [pathname, query = ''] = path.split('?', 2)
  const all = new URLSearchParams(query).get('board') === '*'
  const slugs = targets(query)

  if (pathname === '/link-edges') {
    return { board: null, edges: [] }
  }

  if (pathname === '/dispatch/status') {
    // Mirrors fork-kanban: unreadable boards land in `errors`, count toward
    // board_count, and make running_count unknown (null).
    const boards = slugs.filter(s => !unreadable.has(s)).map(boardStatus)
    const errors = slugs.filter(s => unreadable.has(s)).map(board => ({ board, detail: 'disk gone' }))
    const pausedCount = boards.filter(b => b.paused).length

    return {
      board_count: slugs.length,
      boards,
      errors,
      estop,
      paused: pausedCount === slugs.length,
      paused_count: pausedCount,
      running_count: errors.length ? null : boards.reduce((sum, b) => sum + b.running_count, 0),
      scope: all ? 'all' : 'board'
    }
  }

  if (pathname === '/dispatch/pause' && opts?.method === 'POST') {
    if (busyNext) {
      busyNext = false

      return { busy: slugs, failures: [], paused: false }
    }

    slugs.forEach(s => paused.add(s))

    return { busy: [], failures: [], paused: true }
  }

  if (pathname === '/dispatch/resume' && opts?.method === 'POST') {
    slugs.forEach(s => paused.delete(s))

    return { failures: [], resumed: true }
  }

  throw new Error(`unexpected fork path ${path}`)
})

vi.mock('@/api/plugins', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pluginRest: (plugin: string, path: string, opts?: PluginRestOptions) => forkRest(plugin, path, opts),
  pluginSocket: () => () => undefined
}))

const upstreamRest = vi.fn(async (path: string): Promise<unknown> => {
  const [pathname] = path.split('?')

  if (pathname === '/board') {
    return { assignees: [], columns: [{ name: 'ready', tasks: [] }], latest_event_id: 0, now: 0, tenants: [] }
  }

  if (pathname === '/boards') {
    return { boards: BOARDS.map(slug => ({ name: slug, slug, total: 0 })), current: 'alpha' }
  }

  if (pathname === '/profiles') {
    return { profiles: [] }
  }

  if (pathname === '/orchestration') {
    return { auto_decompose: false, default_assignee: '', orchestrator_profile: '' }
  }

  return { ok: true }
})

// ── harness ──────────────────────────────────────────────────────────────────

let disposeApi: () => void = () => undefined
let disposeLocales: () => void = () => undefined
let root: HTMLElement

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  forkUp = true
  paused = new Set()
  running = {}
  estop = null
  busyNext = false
  unreadable = new Set()
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})

afterEach(() => {
  cleanup()
  disposeApi()
  disposeLocales()
  resetForkBackend()
  resetFocus()
  $boardSlug.set('')
  clearNotifications()
  vi.clearAllMocks()
})

async function mount(slug: string, { openSettings = true } = {}) {
  disposeApi = bindApi(
    async <T,>(path: string) => (await upstreamRest(path)) as T,
    { get: <T,>(key: string, fallback: T) => (key === 'boardSlug' ? (slug as T) : fallback), remove: vi.fn(), set: vi.fn() },
    () => () => undefined
  )

  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  root = render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  ).container

  if (openSettings) {
    fireEvent.click(await screen.findByRole('button', { name: 'Orchestration settings' }))
  }

  return client
}

const control = () => root.querySelector<HTMLElement>('[data-dispatch-control]')
const banner = () => root.querySelector<HTMLElement>('[data-dispatch-paused]')
const summary = () => root.querySelector<HTMLElement>('[data-dispatch-summary]')?.textContent ?? ''

const forkCalls = (method: string) =>
  forkRest.mock.calls.filter(([, , opts]) => (opts?.method ?? 'GET') === method).map(([, path]) => path)

// ── tests ────────────────────────────────────────────────────────────────────

describe('dispatchPath', () => {
  it('omits board for the current board and keeps the all-boards sentinel literal', () => {
    expect(dispatchPath('status', '')).toBe('/dispatch/status')
    expect(dispatchPath('pause', 'alpha')).toBe('/dispatch/pause?board=alpha')
    expect(dispatchPath('resume', ALL_BOARDS)).toBe('/dispatch/resume?board=*')
  })
})

describe('single board', () => {
  it('pauses and resumes the selected board, and the page shows the pause', async () => {
    running = { alpha: 2 }
    await mount('alpha')

    await waitFor(() => expect(control()).not.toBeNull())
    expect(summary()).toBe('Dispatching normally')
    expect(banner()).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Pause dispatch' }))

    await waitFor(() => expect(control()?.dataset.paused).toBe('true'))
    expect(forkCalls('POST')).toEqual(['/dispatch/pause?board=alpha'])
    expect(summary()).toBe('2 running — draining')
    expect(banner()?.textContent).toContain('Dispatch paused · 2 running — draining')
    expect(paused).toEqual(new Set(['alpha']))

    fireEvent.click(screen.getByRole('button', { name: 'Resume dispatch' }))

    await waitFor(() => expect(control()?.dataset.paused).toBe('false'))
    expect(forkCalls('POST')).toEqual(['/dispatch/pause?board=alpha', '/dispatch/resume?board=alpha'])
    await waitFor(() => expect(banner()).toBeNull())
    expect(paused.size).toBe(0)
  })

  it('shows a paused board on the page even with the settings panel closed', async () => {
    paused = new Set(['alpha'])
    await mount('alpha', { openSettings: false })

    await waitFor(() => expect(banner()?.textContent).toContain('Dispatch paused · 0 running — safe to restart'))
    expect(control()).toBeNull()
  })

  it('revokes restart clearance on both surfaces when a refresh of a paused board fails', async () => {
    paused = new Set(['alpha'])
    const client = await mount('alpha')
    await waitFor(() => expect(summary()).toBe('0 running — safe to restart'))
    expect(banner()?.textContent).toContain('Dispatch paused · 0 running — safe to restart')

    forkUp = false
    await client.invalidateQueries({ queryKey: ['fork-kanban', 'dispatch'] })
    await waitFor(() => expect(client.getQueryState(['fork-kanban', 'dispatch', 'local', 'alpha'])?.status).toBe('error'))
    expect(summary()).toBe('status refresh failed — last known state, running count unknown')
    expect(banner()?.textContent).toContain('Dispatch paused · status refresh failed')
    expect(control()?.dataset.stale).toBe('true')
    expect(root.textContent).not.toContain('safe to restart')
  })

  it('does not claim a live board is dispatching normally once its refresh fails', async () => {
    const client = await mount('alpha')
    await waitFor(() => expect(summary()).toBe('Dispatching normally'))

    forkUp = false
    await client.invalidateQueries({ queryKey: ['fork-kanban', 'dispatch'] })
    await waitFor(() => expect(control()?.dataset.stale).toBe('true'))
    expect(summary()).toBe('status refresh failed — last known state, running count unknown')
    // Nothing was paused, so the page banner stays out of the way.
    expect(banner()).toBeNull()
  })

  it('warns when a dispatch tick kept the lock and nothing was written', async () => {
    busyNext = true
    await mount('alpha')
    await waitFor(() => expect(control()).not.toBeNull())

    fireEvent.click(screen.getByRole('button', { name: 'Pause dispatch' }))

    await waitFor(() =>
      expect($notifications.get().some(n => n.kind === 'warning' && /dispatch tick is in progress/.test(n.message ?? ''))).toBe(
        true
      )
    )
    expect(control()?.dataset.paused).toBe('false')
  })
})

describe('All Boards', () => {
  it('revokes restart clearance after a previously successful status read fails', async () => {
    paused = new Set(BOARDS)
    const client = await mount(ALL_BOARDS)
    await waitFor(() => expect(summary()).toContain('safe to restart'))
    expect(banner()?.textContent).toContain('safe to restart')

    forkUp = false
    await client.invalidateQueries({ queryKey: ['fork-kanban', 'dispatch'] })
    await waitFor(() =>
      expect(client.getQueryState(['fork-kanban', 'dispatch', 'local', ALL_BOARDS])?.status).toBe('error')
    )
    expect(client.getQueryState(['fork-kanban', 'dispatch', 'local', ALL_BOARDS])?.data).toBeDefined()
    // The cached paused/zero-running data stays, but it is no longer a drain signal.
    expect(summary()).toBe('2 of 2 boards paused · status refresh failed — last known state, running count unknown')
    expect(banner()?.textContent).toContain('2 of 2 boards paused · status refresh failed')
    expect(control()?.dataset.stale).toBe('true')
    expect(control()?.textContent).toContain('404')
    expect(root.textContent).not.toContain('safe to restart')

    // A later good read restores clearance.
    forkUp = true
    await client.invalidateQueries({ queryKey: ['fork-kanban', 'dispatch'] })
    await waitFor(() => expect(summary()).toBe('2 of 2 boards paused · 0 running — safe to restart'))
    expect(control()?.dataset.stale).toBeUndefined()
  })

  it('pauses every board at once, then resumes them all', async () => {
    running = { alpha: 1, beta: 2 }
    await mount(ALL_BOARDS)

    await waitFor(() => expect(control()).not.toBeNull())
    expect(screen.getByRole('button', { name: 'Resume all boards' })).toHaveProperty('disabled', true)

    fireEvent.click(screen.getByRole('button', { name: 'Pause all boards' }))

    await waitFor(() => expect(control()?.dataset.paused).toBe('true'))
    expect(forkCalls('POST')).toEqual(['/dispatch/pause?board=*'])
    expect(paused).toEqual(new Set(BOARDS))
    expect(summary()).toBe('2 of 2 boards paused · 3 running — draining')
    expect(banner()?.textContent).toContain('2 of 2 boards paused · 3 running — draining')
    expect(screen.getByRole('button', { name: 'Pause all boards' })).toHaveProperty('disabled', true)

    fireEvent.click(screen.getByRole('button', { name: 'Resume all boards' }))

    await waitFor(() => expect(control()?.dataset.paused).toBe('false'))
    expect(forkCalls('POST')).toEqual(['/dispatch/pause?board=*', '/dispatch/resume?board=*'])
    expect(paused.size).toBe(0)
    await waitFor(() => expect(banner()).toBeNull())
  })

  it('reports a partial pause without restart clearance and keeps both actions available', async () => {
    // beta paused, alpha live, nothing running: alpha can claim work at any
    // moment, so this is a count, never "safe to restart".
    paused = new Set(['beta'])
    await mount(ALL_BOARDS)

    await waitFor(() => expect(summary()).toBe('1 of 2 boards paused · 0 running'))
    expect(control()?.dataset.paused).toBe('false')
    expect(screen.getByRole('button', { name: 'Pause all boards' })).toHaveProperty('disabled', false)
    expect(screen.getByRole('button', { name: 'Resume all boards' })).toHaveProperty('disabled', false)
    expect(banner()?.textContent).toContain('1 of 2 boards paused · 0 running')
    expect(root.textContent).not.toContain('safe to restart')
  })

  it('never clears a restart when a board could not be read, and names it', async () => {
    // Every board paused, but alpha's read failed: its workers are unknown,
    // so "0 running" from the readable boards is not a drain signal.
    paused = new Set(BOARDS)
    running = { alpha: 3 }
    unreadable = new Set(['alpha'])
    await mount(ALL_BOARDS)

    const unknown = 'running count unknown — could not read alpha'

    await waitFor(() => expect(summary()).toBe(`1 of 2 boards paused · ${unknown}`))
    expect(banner()?.textContent).toContain(`1 of 2 boards paused · ${unknown}`)
    expect(root.textContent).not.toContain('safe to restart')
    expect(control()?.dataset.paused).toBe('false')
    // A retry of the fan-out is still on offer, as is resume.
    expect(screen.getByRole('button', { name: 'Pause all boards' })).toHaveProperty('disabled', false)
    expect(screen.getByRole('button', { name: 'Resume all boards' })).toHaveProperty('disabled', false)
  })

  it('shows an unreadable board even when nothing is paused yet', async () => {
    unreadable = new Set(['beta'])
    await mount(ALL_BOARDS)

    await waitFor(() => expect(summary()).toBe('0 of 2 boards paused · running count unknown — could not read beta'))
  })
})

describe('drainText', () => {
  const k = {
    draining: (n: number) => `${n} draining`,
    runningCount: (n: number) => `${n} running`,
    safeToRestart: 'safe',
    statusStale: 'stale',
    statusUnknown: (b: string) => `unknown ${b}`
  } as unknown as KanbanText

  const status = (over: Partial<DispatchStatus>): DispatchStatus => ({
    board_count: 2,
    boards: [],
    errors: [],
    estop: null,
    paused: true,
    paused_count: 2,
    running_count: 0,
    scope: 'all',
    ...over
  })

  it('clears a restart only when every board is paused, all reads succeeded, and nothing runs', () => {
    expect(drainText(k, status({}))).toBe('safe')
    expect(drainText(k, status({ running_count: 2 }))).toBe('2 draining')
    expect(drainText(k, status({ paused: false, paused_count: 1 }))).toBe('0 running')
    expect(drainText(k, status({ errors: [{ board: 'a', detail: 'x' }], running_count: null }))).toBe('unknown a')
    expect(drainText(k, status({ running_count: null }))).toBe('unknown ')
    // A failed refresh over cached data clears nothing, whatever the cache says.
    expect(drainText(k, status({}), true)).toBe('stale')
    expect(drainText(k, status({ paused: false, paused_count: 0 }), true)).toBe('stale')
  })
})

describe('global emergency stop and degraded backends', () => {
  it('names an engaged `hermes pause` in the panel and on the page', async () => {
    estop = { reason: 'cutover' }
    await mount('alpha')

    await waitFor(() => expect(root.querySelector('[data-estop]')?.textContent).toContain('hermes pause: cutover'))
    expect(banner()?.textContent).toContain('no board dispatches until `hermes resume`')
  })

  it('renders upstream\'s panel unchanged without the fork backend', async () => {
    forkUp = false
    await mount('alpha')

    await screen.findByText('Auto-decompose triage tasks')
    await waitFor(() => expect(forkRest).toHaveBeenCalled())
    expect(control()).toBeNull()
    expect(banner()).toBeNull()
    expect(forkCalls('GET').some(path => path.startsWith('/dispatch'))).toBe(false)
  })
})
