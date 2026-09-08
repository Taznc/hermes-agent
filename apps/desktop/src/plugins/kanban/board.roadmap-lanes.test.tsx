/**
 * Focused tests for the Ideas/Roadmap wishlist lanes on the Kanban board
 * (t_7a22329e — the Desktop half of the Kanban-native roadmap).
 *
 * Three contracts, all of which a user would notice breaking:
 *
 *  1. **Render + order.** The backend appends `idea`/`roadmap` to its
 *     BOARD_COLUMNS, but they belong LEFTMOST — upstream of the authorization
 *     line — and they must carry real labels/icons instead of falling through
 *     to `columnLabel`'s raw-status fallback.
 *  2. **The visibility toggle.** Hiding removes both lanes ENTIRELY (not the
 *     thin `$collapsedLanes` rail), drops their cards out of the header count,
 *     persists per board, and leaves a pill carrying the parked count.
 *  3. **The drag matrix.** Refine/demote/spawn are allowed; an idea cannot
 *     skip refinement into the work queue, and NOTHING live may be dropped
 *     into a lane. A refusal must not fire a write, and a server-side refusal
 *     must roll the optimistic card back rather than leaving a phantom.
 *
 * Exercises the real component tree through @hermes/plugin-sdk (the pattern in
 * board.all-boards.test.tsx); only ./api is mocked, at the module boundary.
 */
import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, $roadmapHidden, bindApi } from './api'
import { KanbanBoardPage } from './board'
import type { KanbanBoard, KanbanTask } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

// usePluginI18n echoes the dotted key so assertions read the component's OWN
// key choice rather than translated prose (the convention across this plugin).
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchBoardMock = vi.fn()
const fetchAllBoardsMock = vi.fn()
const patchTaskMock = vi.fn()

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    deleteTask: vi.fn().mockResolvedValue({}),
    fetchAllBoards: (...args: unknown[]) => fetchAllBoardsMock(...args),
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
  fetchAllBoardsMock.mockReset()
  patchTaskMock.mockReset().mockResolvedValue({})
  $roadmapHidden.set({})
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  $boardSlug.set('')
  $roadmapHidden.set({})
  window.localStorage.clear()
})

/** A real `localStorage`-backed `PluginStorage`, scoped exactly like the
 *  plugin host's own `createPluginStorage('kanban')` — so a test that binds
 *  through it exercises the SAME persistence path production uses, not an
 *  in-memory stand-in that would pass even if `bindApi`'s wiring broke. */
function localStoragePluginStorage(): Parameters<typeof bindApi>[1] {
  const scoped = (key: string) => `hermes.plugin.kanban.${key}`

  return {
    get: (key, fallback) => {
      const raw = window.localStorage.getItem(scoped(key))

      if (raw === null) {
        return fallback
      }

      try {
        return JSON.parse(raw)
      } catch {
        return fallback
      }
    },
    remove: key => window.localStorage.removeItem(scoped(key)),
    set: (key, value) => window.localStorage.setItem(scoped(key), JSON.stringify(value))
  }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
}

const card = (id: string, status: string, title: string): KanbanTask => ({ id, status, title })

/**
 * A board in the BACKEND's own column order — lanes LAST, matching
 * `plugin_api.BOARD_COLUMNS` (`…, "done", "idea", "roadmap"`). The reorder is
 * the client's job, so the fixture must not pre-sort it or the ordering test
 * would assert nothing.
 */
function boardPayload(overrides: Partial<KanbanBoard> = {}): KanbanBoard {
  return {
    assignees: [],
    columns: [
      { name: 'triage', tasks: [card('t_tri', 'triage', 'Triage card')] },
      { name: 'todo', tasks: [card('t_todo', 'todo', 'Todo card')] },
      { name: 'ready', tasks: [card('t_ready', 'ready', 'Ready card')] },
      { name: 'running', tasks: [] },
      { name: 'blocked', tasks: [] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [] },
      { name: 'idea', tasks: [card('t_idea', 'idea', 'Rough idea card')] },
      { name: 'roadmap', tasks: [card('t_road', 'roadmap', 'Roadmap card')] }
    ],
    latest_event_id: 0,
    now: 0,
    tenants: [],
    ...overrides
  }
}

/** Lane header labels, in the order they are painted on the board. */
function renderedLaneOrder(): string[] {
  return [...globalThis.document.querySelectorAll('[aria-label^="collapse"], [aria-label^="expand"]')].map(
    el => el.getAttribute('aria-label') ?? ''
  )
}

describe('lane rendering', () => {
  it('renders both wishlist lanes with real labels, not the raw status fallback', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()

    await screen.findByText('Rough idea card')
    expect(screen.getByText('Roadmap card')).toBeTruthy()

    // `columnLabel` falls back to the raw status string for an unknown column;
    // seeing the bare ids as lane headers is exactly the pre-fix symptom.
    const headers = [...globalThis.document.querySelectorAll('header span')].map(el => el.textContent)
    expect(headers).not.toContain('idea')
    expect(headers).not.toContain('roadmap')
  })

  it('paints the lanes LEFTMOST even though the backend sends them last', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')

    // Lane order is read off the rendered DOM, not the fixture: the fixture is
    // deliberately in the backend's own (lanes-last) order.
    const laneNodes = [...globalThis.document.querySelectorAll('.group\\/col, [aria-label^="expand"]')]
    const titles = [...globalThis.document.querySelectorAll('.line-clamp-2')].map(el => el.textContent)
    const ideaIndex = titles.indexOf('Rough idea card')
    const roadmapIndex = titles.indexOf('Roadmap card')
    const triageIndex = titles.indexOf('Triage card')

    expect(laneNodes.length).toBeGreaterThan(0)
    expect(ideaIndex).toBeGreaterThanOrEqual(0)
    expect(ideaIndex).toBeLessThan(roadmapIndex)
    expect(roadmapIndex).toBeLessThan(triageIndex)
  })

  it('gives lane cards the stripped chrome: no assignee avatar, no age, no summary preview, no id chip, no board badge', async () => {
    fetchBoardMock.mockResolvedValue(
      boardPayload({
        columns: [
          { name: 'todo', tasks: [] },
          {
            name: 'idea',
            tasks: [
              {
                assignee: 'someone',
                board: 'homelab',
                board_name: 'Homelab',
                body: 'A long body that would otherwise render as a preview line',
                created_at: 1,
                id: 't_idea',
                latest_summary: 'a summary that must not render',
                status: 'idea',
                title: 'Rough idea card'
              }
            ]
          }
        ]
      })
    )

    mount()
    await screen.findByText('Rough idea card')

    expect(screen.queryByText('a summary that must not render')).toBeNull()
    expect(screen.queryByText(/A long body that would otherwise/)).toBeNull()
    // The assignee avatar carries the profile name as its title/aria; the lane
    // footer renders no assignee at all.
    expect(screen.queryByText('someone')).toBeNull()
    // The reviewer-flagged extras: the pinned chrome contract is "title +
    // priority + parent link only" — no id chip (`IdChip` renders the full
    // task id as text) and no board-attribution badge, even though this
    // fixture carries both `board`/`board_name` (the All Boards case).
    expect(screen.queryByText('t_idea')).toBeNull()
    expect(screen.queryByText('Homelab')).toBeNull()
  })

  it('offers no "new task in this lane" button — the capture dialog is the only door', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    const ideaCard = await screen.findByText('Rough idea card')
    const roadmapCard = screen.getByText('Roadmap card')

    // Scope to each lane's own column element (`group/col`) rather than
    // counting board-wide: an empty lane collapses to a rail and renders no
    // add button either, which would make a global count assert nothing.
    const laneOf = (node: HTMLElement) => node.closest('.group\\/col')
    const ideaLane = laneOf(ideaCard)
    const roadmapLane = laneOf(roadmapCard)

    expect(ideaLane).toBeTruthy()
    expect(roadmapLane).toBeTruthy()
    expect(ideaLane!.querySelector('[aria-label="newTaskIn"]')).toBeNull()
    expect(roadmapLane!.querySelector('[aria-label="newTaskIn"]')).toBeNull()

    // Control: a live lane with cards DOES offer it, so the assertion above is
    // about the lane variant, not about the button being globally absent.
    expect(laneOf(screen.getByText('Todo card'))!.querySelector('[aria-label="newTaskIn"]')).toBeTruthy()
  })
})

describe('roadmap visibility toggle', () => {
  it('defaults to SHOWN', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()

    expect(await screen.findByText('Rough idea card')).toBeTruthy()
    expect(screen.getByLabelText('roadmapHideLanes')).toBeTruthy()
  })

  it('hiding removes BOTH lanes entirely — not a collapsed rail', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')

    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))

    await waitFor(() => expect(screen.queryByText('Rough idea card')).toBeNull())
    expect(screen.queryByText('Roadmap card')).toBeNull()
    // A collapsed lane still renders its expand affordance; a hidden one does
    // not exist at all. This is the distinction the card asked for.
    expect(renderedLaneOrder().some(label => label.includes('idea'))).toBe(false)
    // Live lanes are untouched.
    expect(screen.getByText('Triage card')).toBeTruthy()
  })

  it('hidden lanes drop out of the header total', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')

    // 5 cards total: triage + todo + ready + idea + roadmap.
    expect(screen.getByText('5')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))

    await waitFor(() => expect(screen.getByText('3')).toBeTruthy())
  })

  it('shows a pill carrying the parked count, which toggles the lanes back', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')
    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))

    const pill = await screen.findByLabelText('roadmapShowLanes')
    expect(pill.textContent).toContain('roadmapPill')

    fireEvent.click(pill)

    expect(await screen.findByText('Rough idea card')).toBeTruthy()
    expect(screen.getByText('Roadmap card')).toBeTruthy()
  })

  it('persists per board: hiding on one board leaves another board showing', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())
    $boardSlug.set('shipping')

    const view = mount()
    await screen.findByText('Rough idea card')
    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))
    await waitFor(() => expect(screen.queryByText('Rough idea card')).toBeNull())

    // The store is what survives a reload, so assert the scoped shape directly
    // as well as the re-render behavior below.
    expect($roadmapHidden.get()).toEqual({ shipping: true })

    view.unmount()
    $boardSlug.set('homelab')
    mount()

    // Another board never inherited the hide.
    expect(await screen.findByText('Rough idea card')).toBeTruthy()
  })

  it('survives a real storage rehydrate — a fresh bindApi() rereads localStorage, not the in-memory atom', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    // Bind through the SAME persistence path production uses: bindApi's
    // `persist()` hydrates $roadmapHidden (and $boardSlug) from storage at
    // bind time and writes every change back to it — so the board slug is
    // set AFTER binding, exactly like a real selection made once the plugin
    // has loaded. A prior version of this test only remounted React while
    // retaining the same in-memory atom, which would stay green even if
    // bindApi's storage wiring were entirely broken.
    const noopSocket = () => () => undefined
    let dispose = bindApi(vi.fn().mockRejectedValue(new Error('unused')), localStoragePluginStorage(), noopSocket)
    $boardSlug.set('shipping')

    const view = mount()
    await screen.findByText('Rough idea card')
    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))
    await waitFor(() => expect(screen.queryByText('Rough idea card')).toBeNull())

    // Confirm the write actually landed in localStorage under the plugin's
    // real key, not just in the atom.
    expect(JSON.parse(window.localStorage.getItem('hermes.plugin.kanban.roadmapHidden') ?? '{}')).toEqual({
      shipping: true
    })

    view.unmount()
    dispose()
    // A process reload starts from module defaults. Reset while the first
    // binding is disposed so these cold values are NOT written back over the
    // stored preferences; the next bind must restore both atoms from storage.
    $boardSlug.set('')
    $roadmapHidden.set({})
    expect($boardSlug.get()).toBe('')
    expect($roadmapHidden.get()).toEqual({})

    // Simulate an app reload: a brand-new bind, which re-hydrates every
    // persisted atom from storage exactly as plugin load does on boot —
    // including $boardSlug itself, so this is a genuine cold rehydrate.
    dispose = bindApi(vi.fn().mockRejectedValue(new Error('unused')), localStoragePluginStorage(), noopSocket)

    expect($boardSlug.get()).toBe('shipping')
    expect($roadmapHidden.get()).toEqual({ shipping: true })

    mount()

    expect(await screen.findByLabelText('roadmapShowLanes')).toBeTruthy()
    expect(screen.queryByText('Rough idea card')).toBeNull()

    dispose()
  })
})

describe('hiding while a wishlist card is selected', () => {
  it('prunes hidden wishlist cards out of the selection, so the SelectionBar cannot act on invisible cards', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')

    // Select the idea card (⌘/Ctrl-click, same gesture the board wires up).
    fireEvent.click(screen.getByText('Rough idea card'), { ctrlKey: true })
    expect(await screen.findByText('nSelected')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))

    // The lanes disappear AND the floating bulk bar goes with them — a
    // selected-but-now-invisible card must not remain assign/archive/
    // delete-able through a bar the user can no longer see the target of.
    await waitFor(() => expect(screen.queryByText('Rough idea card')).toBeNull())
    expect(screen.queryByText('nSelected')).toBeNull()
  })

  it('leaves a selected LIVE card untouched by the hide toggle', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await screen.findByText('Rough idea card')

    fireEvent.click(screen.getByText('Todo card'), { ctrlKey: true })
    expect(await screen.findByText('nSelected')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('roadmapHideLanes'))

    await waitFor(() => expect(screen.queryByText('Rough idea card')).toBeNull())
    // The live selection survives — hiding wishlist lanes must not clear an
    // unrelated in-progress bulk selection.
    expect(screen.getByText('nSelected')).toBeTruthy()
  })
})

/**
 * The drag matrix, driven through the card context menu — the same `onMove`
 * handler the lane drop target calls, so this exercises the real gate rather
 * than a parallel one. Each case asserts on whether a WRITE happened, which is
 * what "no phantom card" reduces to: a refused move never mutates the cache.
 */
describe('drag matrix', () => {
  const openMenu = async (title: string) => {
    fireEvent.contextMenu(await screen.findByText(title))
  }

  it('idea → roadmap refines (a write with status: roadmap)', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Rough idea card')

    fireEvent.click(await screen.findByText('laneRefine'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    expect(patchTaskMock.mock.calls[0][0]).toBe('t_idea')
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'roadmap' })
  })

  it('roadmap → idea demotes', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Roadmap card')

    fireEvent.click(await screen.findByText('laneDemote'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'idea' })
  })

  it('roadmap → triage spawns immediately, with no confirm', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Roadmap card')

    fireEvent.click(await screen.findByText('laneSpawnTriage'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'triage' })
  })

  it('roadmap → ready asks first, and only writes after the confirm', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Roadmap card')

    fireEvent.click(await screen.findByText('laneSpawnReady'))

    // The confirm is up and NOTHING has been written yet — skipping
    // auto-decompose is the decision the dialog exists to take.
    expect(await screen.findByText('spawnReadyBody')).toBeTruthy()
    expect(patchTaskMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('spawnReadyConfirm'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    expect(patchTaskMock.mock.calls[0][1]).toEqual({ status: 'ready' })
  })

  it('an idea card is never offered a move into the work queue', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Rough idea card')

    await screen.findByText('laneRefine')
    // Refine + Archive are the only moves; the generic "move to <lane>" rows
    // (which would include triage/ready) must not be offered at all.
    expect(screen.queryByText('moveTo')).toBeNull()
    expect(screen.queryByText('laneSpawnTriage')).toBeNull()
    expect(screen.queryByText('laneSpawnReady')).toBeNull()
  })

  it('a live card is never offered a move INTO a lane', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    await openMenu('Todo card')

    const moveRows = await screen.findAllByText('moveTo')
    // Under the i18n stub every row reads `moveTo`, so count them: the live
    // targets a `todo` card may reach are triage, ready, done and archived —
    // never idea or roadmap.
    expect(moveRows.length).toBe(4)
    expect(screen.queryByText('laneRefine')).toBeNull()
    expect(screen.queryByText('laneDemote')).toBeNull()
  })

  it('rejects an actual idea → triage drop before any optimistic write and shows invalid-drop feedback', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())
    const notify = vi.spyOn(host, 'notify')

    mount()
    await screen.findByText('Rough idea card')
    const triageLane = screen.getByText('Triage card').closest('.group\\/col')

    expect(triageLane).toBeTruthy()
    fireEvent.drop(triageLane!, {
      dataTransfer: { dropEffect: 'move', getData: () => 't_idea' }
    })

    expect(patchTaskMock).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith({ kind: 'warning', message: 'laneDropRefused' })
  })

  it('bulk movement cannot bypass the per-card wishlist transition rules', async () => {
    fetchBoardMock.mockResolvedValue(boardPayload())

    mount()
    fireEvent.click(await screen.findByText('Rough idea card'), { ctrlKey: true })
    const moveTrigger = await screen.findByText('moveToShort')

    // Radix opens dropdown triggers on pointerdown; a click alone is not a
    // faithful user gesture in jsdom.
    fireEvent.pointerDown(moveTrigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(moveTrigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(moveTrigger)
    await screen.findByRole('menu')

    // Wishlist exits are deliberately per-card: an idea needs Refine and a
    // roadmap→Ready spawn needs confirmation. The bulk endpoint offers neither
    // semantic, so it must not expose any target for a lane-card selection.
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })

  it('a server-side refusal rolls the card back into its source lane and surfaces the verbatim detail', async () => {
    // The engine answers a refused lane transition with a 400 whose detail is
    // the DB layer's own `'<from>' -> '<to>'` message.
    patchTaskMock.mockRejectedValue(new Error('400 {"detail":"invalid roadmap lane transition"}'))
    fetchBoardMock.mockResolvedValue(boardPayload())
    const notify = vi.spyOn(host, 'notify')

    mount()
    await openMenu('Rough idea card')
    const ideaLaneBefore = screen.getByText('Rough idea card').closest('.group\\/col')

    expect(ideaLaneBefore).toBeTruthy()
    fireEvent.click(await screen.findByText('laneRefine'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())

    // The optimistic edit is rolled back: exactly one copy of the card
    // exists, it is back inside the Ideas lane it started in (not stranded
    // in Roadmap, the optimistic target), and the counts are unchanged.
    await waitFor(() => expect(screen.getAllByText('Rough idea card')).toHaveLength(1))
    const ideaLaneAfter = screen.getByText('Rough idea card').closest('.group\\/col')

    expect(ideaLaneAfter).toBe(ideaLaneBefore)
    expect(screen.getByText('5')).toBeTruthy()

    // The server's own detail string reaches the toast verbatim — not the
    // client-side laneDropRefused copy, which only fires for a REFUSAL this
    // component catches before ever writing (see the drop-target case above).
    expect(notify).toHaveBeenCalledWith({ kind: 'error', message: 'invalid roadmap lane transition' })
  })
})
