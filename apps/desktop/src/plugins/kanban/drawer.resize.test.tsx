/**
 * Drawer width sash contracts.
 *
 * The Log tab shows raw worker output, so the 26rem default is the thing the
 * user drags away from. Four properties are what make that drag trustworthy,
 * and each is a distinct way it can silently break:
 *
 *  1. The drag WIDENS as the pointer travels LEFT. The drawer is right-
 *     anchored, so the sign is inverted from the shell's left rail — get it
 *     backwards and dragging outward shrinks the drawer.
 *  2. The width is CLAMPED at both ends: never below a readable 24rem, never
 *     wide enough to swallow the board.
 *  3. Double-click CLEARS the override rather than writing the default px, so
 *     the drawer falls back to its authored `w-[26rem]` class and later
 *     default changes still reach a user who has reset.
 *  4. The drag tears down on pointercancel, not just pointerup — the same
 *     contract as the shell's sashes (master-detail.test.tsx). A live
 *     pointermove listener after a cancelled stream resizes with no button
 *     held.
 *
 * Plus the regression this change is most likely to cause: the open/close
 * animation classes and the Esc-to-close handler must survive the container
 * gaining a sash and an inline width.
 */
import { $paneWidthOverride, setPaneWidthOverride } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { TaskDrawer } from './drawer'
import type { KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchTaskMock = vi.fn()
const fetchLogMock = vi.fn()

vi.mock('./api', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./api')

  return {
    ...actual,
    fetchLog: (...args: unknown[]) => fetchLogMock(...args),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    fetchTask: (...args: unknown[]) => fetchTaskMock(...args)
  }
})

const PANE_ID = 'kanban.taskDrawer'
const DEFAULT_PX = 416
const MIN_PX = 384
// Pinned so the clamp assertions below are arithmetic, not window-dependent.
const VIEWPORT_PX = 1600
const MAX_PX = Math.round(VIEWPORT_PX * 0.68)

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  // jsdom ships no rAF; the Loader's self-scheduling render needs one, and it
  // must be ASYNC or that render recurses without bound.
  window.requestAnimationFrame = (cb: FrameRequestCallback) =>
    setTimeout(() => cb(performance.now()), 0) as unknown as number
  window.cancelAnimationFrame = (handle: number) => clearTimeout(handle as unknown as NodeJS.Timeout)
})

beforeEach(() => {
  setPaneWidthOverride(PANE_ID, undefined)
  fetchTaskMock.mockReset()
  fetchLogMock.mockReset()
  fetchTaskMock.mockResolvedValue(detail())
  fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: VIEWPORT_PX, writable: true })
})

afterEach(() => {
  cleanup()
  setPaneWidthOverride(PANE_ID, undefined)
})

const detail = (): KanbanTaskDetail => ({
  attachments: [],
  comments: [],
  events: [{ created_at: 10, id: 1, kind: 'spawned', payload: { pid: 42 } }],
  links: { children: [], parents: [] },
  runs: [],
  task: { body: 'body', id: 't_abc123', status: 'running', title: 'A running card' }
})

const dispatch = (event: Event) => act(() => void window.dispatchEvent(event))

const width = () => $paneWidthOverride(PANE_ID).get()

async function mountDrawer(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const { container } = render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['ready', 'running', 'done']} id="t_abc123" onClose={onClose} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

  const panel = container.querySelector('.animate-in')
  const sash = container.querySelector('[data-kanban-drawer-sash="true"]')

  if (!(panel instanceof HTMLElement) || !(sash instanceof HTMLElement)) {
    throw new Error('drawer panel or sash not rendered')
  }

  await waitFor(() => expect(fetchTaskMock).toHaveBeenCalled())

  return { onClose, panel, sash }
}

describe('task drawer width sash', () => {
  it('widens as the pointer travels left (right-anchored drag), and the panel paints the new width', async () => {
    const { panel, sash } = await mountDrawer()

    // No override yet: width comes from the authored w-[26rem] class.
    expect(width()).toBeUndefined()
    expect(panel.style.width).toBe('')

    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 800 }))

    expect(width()).toBe(DEFAULT_PX + 100)
    await waitFor(() => expect(panel.style.width).toBe(`${DEFAULT_PX + 100}px`))
  })

  it('narrows as the pointer travels right, and continues from the persisted width on a second drag', async () => {
    const { sash } = await mountDrawer()

    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 700 }))
    dispatch(new PointerEvent('pointerup', {}))
    expect(width()).toBe(DEFAULT_PX + 200)

    // A second drag must start from the STORED width, not the default —
    // otherwise every drag snaps the drawer back to 26rem first.
    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 950 }))

    expect(width()).toBe(DEFAULT_PX + 150)
  })

  it('clamps at a readable minimum and below a board-swallowing maximum', async () => {
    const { sash } = await mountDrawer()

    // Yank far right: floors at the minimum, never a sliver.
    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 5000 }))
    expect(width()).toBe(MIN_PX)

    // Yank far left: caps below the viewport, never the whole board.
    dispatch(new PointerEvent('pointermove', { clientX: -5000 }))
    expect(width()).toBe(MAX_PX)
    expect(MAX_PX).toBeLessThan(VIEWPORT_PX)
  })

  it('double-click clears the override rather than writing the default width', async () => {
    const { panel, sash } = await mountDrawer()

    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 700 }))
    dispatch(new PointerEvent('pointerup', {}))
    expect(width()).toBe(DEFAULT_PX + 200)

    fireEvent.doubleClick(sash)

    // `undefined`, not 416: the panel falls back to its authored class, so a
    // later default change still reaches a user who reset.
    expect(width()).toBeUndefined()
    await waitFor(() => expect(panel.style.width).toBe(''))
    expect(panel.className).toContain('w-[26rem]')
  })

  it('tears the drag down on pointercancel, not just pointerup', async () => {
    const { sash } = await mountDrawer()

    fireEvent.pointerDown(sash, { button: 0, clientX: 900 })
    dispatch(new Event('pointercancel'))
    // The stream is dead: a stray pointermove must not keep resizing.
    dispatch(new PointerEvent('pointermove', { clientX: 700 }))

    expect(width()).toBeUndefined()
  })

  it('ignores non-primary buttons so a right-click near the edge never resizes', async () => {
    const { sash } = await mountDrawer()

    fireEvent.pointerDown(sash, { button: 2, clientX: 900 })
    dispatch(new PointerEvent('pointermove', { clientX: 700 }))

    expect(width()).toBeUndefined()
  })

  it('keeps the open animation and the Esc-to-close handler intact', async () => {
    const { onClose, panel } = await mountDrawer()

    expect(panel.className).toContain('animate-in')
    expect(panel.className).toContain('slide-in-from-right-4')

    dispatch(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(onClose).toHaveBeenCalled()
  })
})
