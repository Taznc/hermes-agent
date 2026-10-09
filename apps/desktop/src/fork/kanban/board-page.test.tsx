/**
 * Focus mode + All Boards, end to end on upstream's real Kanban page and
 * board switcher, wired through `host.fork.kanban` exactly as production is.
 * Only the two network doors are faked: upstream's plugin REST door (handed
 * to `bindApi`, as the plugin loader does) and the fork backend's
 * `pluginRest('fork-kanban', …)`. Adapted from dev's board.all-boards,
 * board.answer-bar, board.dependency-arrows and board.focus-depth tests.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PluginRestOptions } from '@/api/plugins'
import { registerPluginLocales } from '@/i18n/plugin-i18n'
import { $boardSlug, bindApi } from '@/plugins/kanban/api'
import { KanbanBoardPage } from '@/plugins/kanban/board'
import { BoardSwitcher } from '@/plugins/kanban/board-switcher'
import { KANBAN_LOCALES } from '@/plugins/kanban/i18n'
import { $notifications, clearNotifications } from '@/store/notifications'

import { ALL_BOARDS, routeCall } from './all-boards'
import { resetForkBackend } from './backend'
import { $hotEdge } from './board-arrows-layer'
import { $focusDepth, $hiddenBoards, EMPTY_IDS, resetFocus } from './state'

// ── fakes ────────────────────────────────────────────────────────────────────

const NOT_FOUND = "Error invoking remote method 'hermes:api': Error: 404: {\"detail\":\"Not Found\"}"

let forkUp = true

const task = (id: string, title: string, status: string, extra: Record<string, unknown> = {}) => ({
  id,
  status,
  title,
  ...extra
})

/** `f` is held by `h1` and a done `d`, and blocks `c`; `x` holds `h1`.
 *  `h2` shares h1's lane but links to nothing. */
function singleBoard() {
  return {
    assignees: [],
    columns: [
      { name: 'todo', tasks: [task('f', 'Release notes', 'todo')] },
      { name: 'ready', tasks: [task('c', 'Canary run', 'ready')] },
      { name: 'blocked', tasks: [task('h1', 'Salvage escalation', 'blocked'), task('h2', 'Salvage gate', 'blocked')] },
      { name: 'review', tasks: [task('x', 'Redeploy coverage', 'review')] },
      { name: 'done', tasks: [task('d', 'Allowlist refresh', 'done')] }
    ],
    latest_event_id: 3,
    now: 0,
    tenants: []
  }
}

const SINGLE_EDGES: Array<[string, string]> = [
  ['h1', 'f'],
  ['d', 'f'],
  ['x', 'h1'],
  ['f', 'c']
]

function allBoardsPayload() {
  return {
    assignees: [],
    boards: [
      { color: '', name: 'Shipping', slug: 'shipping', task_count: 1 },
      { color: '', name: 'Homelab', slug: 'homelab', task_count: 2 }
    ],
    columns: [
      {
        name: 'todo',
        tasks: [task('t_ship01', 'Ship the feature', 'todo', { board: 'shipping', board_name: 'Shipping' })]
      },
      {
        name: 'ready',
        tasks: [
          task('t_home01', 'Fix the router', 'ready', { board: 'homelab', board_name: 'Homelab' }),
          task('t_home02', 'Flash firmware', 'ready', { board: 'homelab', board_name: 'Homelab' })
        ]
      },
      { name: 'done', tasks: [] }
    ],
    cursors: { homelab: 9, shipping: 5 },
    errors: [],
    latest_event_id: 5,
    link_edges: [{ board: 'homelab', child: 't_home01', parent: 't_home02' }],
    now: 0,
    tenants: []
  }
}

const forkRest = vi.fn(async (_plugin: string, path: string): Promise<unknown> => {
  if (!forkUp) {
    throw new Error(NOT_FOUND)
  }

  if (path.startsWith('/link-edges')) {
    return { board: null, edges: SINGLE_EDGES }
  }

  if (path.startsWith('/board/all')) {
    return allBoardsPayload()
  }

  throw new Error(`unexpected fork path ${path}`)
})

const forkSocket = vi.fn((_plugin: string, _path: string, _onMessage: (data: unknown) => void) => () => undefined)

vi.mock('@/api/plugins', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pluginRest: (plugin: string, path: string) => forkRest(plugin, path),
  pluginSocket: (plugin: string, path: string, onMessage: (data: unknown) => void) =>
    forkSocket(plugin, path, onMessage)
}))

const upstreamRest = vi.fn(async (path: string, _opts?: PluginRestOptions): Promise<unknown> => {
  const [pathname] = path.split('?')

  if (pathname === '/board') {
    return singleBoard()
  }

  if (pathname === '/boards') {
    return { boards: [{ name: 'Default', slug: 'default', total: 6 }], current: 'default' }
  }

  if (pathname === '/profiles') {
    return { profiles: [] }
  }

  if (pathname === '/orchestration') {
    return { default_assignee: '' }
  }

  if (pathname === '/tasks/bulk') {
    return { results: [] }
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
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})

afterEach(() => {
  cleanup()
  disposeApi()
  disposeLocales()
  resetForkBackend()
  resetFocus()
  $focusDepth.set('direct')
  $hiddenBoards.set(EMPTY_IDS)
  $hotEdge.set(null)
  $boardSlug.set('')
  clearNotifications()
  vi.clearAllMocks()
})

function mount({ slug = '', switcher = false }: { slug?: string; switcher?: boolean } = {}) {
  disposeApi = bindApi(
    async <T,>(path: string, opts?: PluginRestOptions) => (await upstreamRest(path, opts)) as T,
    { get: <T,>(key: string, fallback: T) => (key === 'boardSlug' ? (slug as T) : fallback), remove: vi.fn(), set: vi.fn() },
    () => () => undefined
  )

  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  root = render(
    <QueryClientProvider client={client}>
      {switcher && <BoardSwitcher />}
      <KanbanBoardPage />
    </QueryClientProvider>
  ).container
}

/** Match by value, not selector: merged keys contain NUL, which CSS
 *  selectors rewrite to U+FFFD. */
const cardByKey = (key: string) =>
  Array.from(root.querySelectorAll<HTMLElement>('[data-card-key]')).find(el => el.dataset.cardKey === key) ?? null

const bar = () => root.querySelector<HTMLElement>('[data-answer-bar]')
const gaps = () => Array.from(root.querySelectorAll<HTMLElement>('[data-lane-gap]'))
const traceButton = (key: string) => cardByKey(key)?.querySelector<HTMLButtonElement>('button[aria-pressed]') ?? null

/** Wait until the fork backend's edges arrived and the trace affordance shows. */
async function ready(key = 'f') {
  await screen.findByText('Release notes')
  await waitFor(() => expect(traceButton(key)).not.toBeNull())
}

async function openSwitcher() {
  const trigger = await screen.findByRole('button', { name: /^Board:/ })

  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
}

// ── focus mode ───────────────────────────────────────────────────────────────

describe('focus mode', () => {
  it('tags every card for the arrows layer and offers a trace only on linked cards', async () => {
    mount()
    await ready()

    for (const key of ['f', 'c', 'h1', 'h2', 'x', 'd']) {
      expect(cardByKey(key), key).not.toBeNull()
    }

    expect(traceButton('f')).not.toBeNull()
    expect(traceButton('h2')).toBeNull()
    expect(forkRest).toHaveBeenCalledWith('fork-kanban', '/link-edges')
  })

  it('focusing a card rings its links, dims the rest, and opens the answer bar', async () => {
    mount()
    await ready('h1')
    expect(bar()).toBeNull()

    fireEvent.click(traceButton('h1')!)

    await waitFor(() => expect(bar()).not.toBeNull())
    expect(cardByKey('h1')!.getAttribute('data-focus-role')).toBe('focused')
    expect(cardByKey('x')!.getAttribute('data-focus-role')).toBe('upstream')
    expect(cardByKey('f')!.getAttribute('data-focus-role')).toBe('downstream')
    expect(bar()!.textContent).toContain('Salvage escalation')
    expect(bar()!.querySelector('[data-blocker-rows]')!.textContent).toContain('Redeploy coverage')
    expect(bar()!.querySelector('[data-dependant-rows]')!.textContent).toContain('Release notes')

    // An unrelated card, once its fold is opened, reads dimmed.
    const blockedGap = gaps().find(gap => gap.parentElement?.contains(cardByKey('h1')))!

    fireEvent.click(blockedGap)
    await waitFor(() => expect(cardByKey('h2')).not.toBeNull())
    expect(cardByKey('h2')!.getAttribute('data-focus-role')).toBe('dimmed')
    expect(bar()).not.toBeNull()
  })

  it('draws a dependency line per direct link while a trace is live', async () => {
    mount()
    await ready()
    expect(root.querySelector('[data-board-arrows]')).toBeNull()

    fireEvent.click(traceButton('f')!)

    await waitFor(() =>
      expect(
        Array.from(root.querySelectorAll('[data-board-arrows] [data-edge]'))
          .map(el => el.getAttribute('data-edge'))
          .sort()
      ).toEqual(['d->f', 'f->c', 'h1->f'])
    )
  })

  it('Esc clears the focus, the bar, and every fold', async () => {
    mount()
    await ready()
    fireEvent.click(traceButton('f')!)
    await waitFor(() => expect(bar()).not.toBeNull())
    expect(gaps().length).toBeGreaterThan(0)

    fireEvent.keyDown(window, { key: 'Escape' })

    await waitFor(() => expect(bar()).toBeNull())
    expect(gaps()).toHaveLength(0)
    expect(root.querySelectorAll('[data-focus-role="dimmed"]')).toHaveLength(0)
    expect(root.querySelectorAll('[data-card-key]')).toHaveLength(6)
  })

  it('pressing the focused card\'s toggle again clears the trace', async () => {
    mount()
    await ready()
    fireEvent.click(traceButton('f')!)
    await waitFor(() => expect(bar()).not.toBeNull())

    fireEvent.click(traceButton('f')!)

    await waitFor(() => expect(bar()).toBeNull())
  })

  it('folds unrelated cards into "+N" gaps that account for the whole lane, and expands on click', async () => {
    mount()
    await ready('h1')

    // Focus h1: its links are x (blocker) and f (dependant). h2 (same lane),
    // c and d are unrelated, so they fold; linked cards stay.
    fireEvent.click(traceButton('h1')!)
    await waitFor(() => expect(gaps().length).toBeGreaterThan(0))

    for (const key of ['h1', 'x', 'f']) {
      expect(cardByKey(key), key).not.toBeNull()
    }

    for (const key of ['h2', 'c', 'd']) {
      expect(cardByKey(key), key).toBeNull()
    }

    const folded = gaps().reduce((sum, gap) => sum + Number(gap.getAttribute('data-lane-gap')), 0)

    expect(folded + root.querySelectorAll('[data-card-key]').length).toBe(6)
    expect(gaps().map(gap => gap.textContent)).toContain('+1 card')

    // Opening a gap shows its card and does NOT count as a click off the board.
    fireEvent.click(gaps().find(gap => gap.parentElement?.contains(cardByKey('h1')))!)
    await waitFor(() => expect(cardByKey('h2')).not.toBeNull())
    expect(bar()).not.toBeNull()
  })

  it('Full chain lights the transitive blocker that Direct links leaves folded', async () => {
    mount()
    await ready()
    fireEvent.click(traceButton('f')!)
    await waitFor(() => expect(bar()).not.toBeNull())

    // x holds h1 which holds f: two hops away.
    expect(cardByKey('x')).toBeNull()

    act(() => $focusDepth.set('chain'))

    await waitFor(() => expect(cardByKey('x')?.getAttribute('data-focus-role')).toBe('upstream'))
  })
})

// ── All Boards ───────────────────────────────────────────────────────────────

describe('All Boards', () => {
  it('appears in the switcher only once the fork backend answers', async () => {
    mount({ switcher: true })
    await ready()
    await openSwitcher()

    expect(await screen.findByText('All Boards')).toBeTruthy()
  })

  it('selecting it serves the merged payload and routes per-task calls to each card\'s own board', async () => {
    mount({ switcher: true })
    await ready()
    await openSwitcher()
    fireEvent.click(await screen.findByText('All Boards'))

    expect(await screen.findByText('Ship the feature')).toBeTruthy()
    expect($boardSlug.get()).toBe(ALL_BOARDS)
    expect(forkRest).toHaveBeenCalledWith('fork-kanban', '/board/all')
    expect(screen.getByRole('button', { name: 'Board: All Boards' })).toBeTruthy()
    // Board badges + filter chips.
    expect(cardByKey('homelab\u0000t_home01')?.querySelector('[data-board-badge="homelab"]')).not.toBeNull()
    expect(root.querySelector('[data-board-chip="shipping"]')).not.toBeNull()
    // Upstream's socket follows one board; the fork follows each of them.
    await waitFor(() =>
      expect(forkSocket).toHaveBeenCalledWith('kanban', '/events?board=homelab&since=9', expect.any(Function))
    )
    expect(forkSocket).toHaveBeenCalledWith('kanban', '/events?board=shipping&since=5', expect.any(Function))

    // Move a homelab card: the PATCH carries its own board, never the sentinel.
    fireEvent.contextMenu(screen.getByText('Fix the router'))
    fireEvent.click(await screen.findByText('Move to Done'))

    await waitFor(() =>
      expect(upstreamRest).toHaveBeenCalledWith('/tasks/t_home01?board=homelab', expect.objectContaining({ method: 'PATCH' }))
    )
    expect(upstreamRest.mock.calls.some(([path]) => path.includes('board=*') || path.includes('board=%2A'))).toBe(false)
  })

  it('traces a dependency across the merged payload by (board, id)', async () => {
    mount({ slug: ALL_BOARDS })
    await screen.findByText('Fix the router')

    const key = 'homelab\u0000t_home01'

    await waitFor(() => expect(traceButton(key)).not.toBeNull())
    fireEvent.click(traceButton(key)!)

    await waitFor(() => expect(bar()).not.toBeNull())
    expect(cardByKey('homelab\u0000t_home02')!.getAttribute('data-focus-role')).toBe('upstream')
    // The shipping card is unrelated, so its lane folds.
    expect(cardByKey('shipping\u0000t_ship01')).toBeNull()
  })

  it('a board chip hides and restores that board\'s cards', async () => {
    mount({ slug: ALL_BOARDS })
    await screen.findByText('Ship the feature')

    fireEvent.click(root.querySelector('[data-board-chip="shipping"]')!)
    await waitFor(() => expect(screen.queryByText('Ship the feature')).toBeNull())

    fireEvent.click(root.querySelector('[data-board-chip="shipping"]')!)
    expect(await screen.findByText('Ship the feature')).toBeTruthy()
  })
})

describe('routeCall (pure)', () => {
  it('is a pass-through outside All Boards', async () => {
    const send = vi.fn(async (path: string) => path)

    await expect(routeCall('/tasks/a?board=ops', 'ops', send)).resolves.toBe('/tasks/a?board=ops')
  })

  it('drops the sentinel for board-less writes (create → server current board)', async () => {
    const send = vi.fn(async (path: string) => path)

    await expect(routeCall('/tasks?board=*', ALL_BOARDS, send)).resolves.toBe('/tasks')
  })

  it('fans a bulk patch out once per board and merges the results', async () => {
    // Index the merged payload (task → board) through the real fetch.
    await routeCall('/board?board=*', ALL_BOARDS, vi.fn())

    const send = vi.fn(async (path: string, opts?: PluginRestOptions) => ({
      results: ((opts?.body as { ids: string[] }).ids ?? []).map(id => ({ id, ok: true, path }))
    }))

    const reply = (await routeCall('/tasks/bulk?board=*', ALL_BOARDS, send, {
      body: { ids: ['t_ship01', 't_home01', 't_home02'], status: 'done' },
      method: 'POST'
    })) as { results: Array<{ id: string; path: string }> }

    expect(send).toHaveBeenCalledTimes(2)
    expect(reply.results.map(r => [r.id, r.path]).sort()).toEqual([
      ['t_home01', '/tasks/bulk?board=homelab'],
      ['t_home02', '/tasks/bulk?board=homelab'],
      ['t_ship01', '/tasks/bulk?board=shipping']
    ])
  })
})

// ── graceful degradation ─────────────────────────────────────────────────────

describe('without the fork-kanban backend (404)', () => {
  it('renders upstream\'s board unchanged: no All Boards, no trace, no errors', async () => {
    forkUp = false
    mount({ switcher: true })
    await screen.findByText('Release notes')
    await waitFor(() => expect(forkRest).toHaveBeenCalled())
    await act(async () => undefined)

    expect(root.querySelectorAll('button[aria-pressed]')).toHaveLength(0)
    expect(bar()).toBeNull()

    await openSwitcher()
    expect(await screen.findByText('Default', { selector: '[role="menuitem"]' })).toBeTruthy()
    expect(screen.queryByText('All Boards')).toBeNull()
    expect($notifications.get()).toHaveLength(0)
  })

  it('a persisted All Boards selection falls back to the current board silently', async () => {
    forkUp = false
    mount({ slug: ALL_BOARDS })

    expect(await screen.findByText('Release notes')).toBeTruthy()
    await waitFor(() => expect($boardSlug.get()).toBe(''))
    expect($notifications.get()).toHaveLength(0)
  })
})
