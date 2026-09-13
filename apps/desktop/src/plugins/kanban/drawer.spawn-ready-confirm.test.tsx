/**
 * Roadmap → Ready confirmation from the drawer's StatusMenu (t_7a22329e
 * review round 1, finding 2): the drawer exposes Ready in the status picker
 * for a `roadmap` card, and the confirm — "Skip auto-decompose and dispatch
 * as-is?" — must gate that move exactly like the board's drag/menu path.
 * Bypassing it (calling `moveMut.mutate` directly, fire-and-forget) was the
 * regression this file pins against.
 *
 * Exercises the real StatusMenu + ConfirmDialog through @hermes/plugin-sdk,
 * matching the pattern in drawer.tabs.test.tsx.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
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
const patchTaskMock = vi.fn()

vi.mock('./api', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./api')

  return {
    ...actual,
    fetchLog: (...args: unknown[]) => fetchLogMock(...args),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    fetchTask: (...args: unknown[]) => fetchTaskMock(...args),
    patchTask: (...args: unknown[]) => patchTaskMock(...args)
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  fetchTaskMock.mockReset()
  fetchLogMock.mockReset().mockResolvedValue({ content: '', exists: true, size_bytes: 0, truncated: false })
  patchTaskMock.mockReset()
})

afterEach(() => {
  cleanup()
})

const detail = (overrides: Partial<KanbanTaskDetail> = {}): KanbanTaskDetail => ({
  attachments: [],
  comments: [],
  events: [],
  links: { children: [], parents: [] },
  runs: [],
  task: { id: 't_road', status: 'roadmap', title: 'A roadmap card' },
  ...overrides
})

function mount(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function drawer() {
  return <TaskDrawer columns={['idea', 'roadmap', 'triage', 'ready']} id="t_road" onClose={vi.fn()} onOpen={vi.fn()} />
}

const openStatusMenu = async () => {
  // StatusMenu's trigger is a `DropdownMenuTrigger asChild` — Radix opens
  // dropdown triggers on pointerdown, so a bare click is not a faithful user
  // gesture in jsdom (same pattern as the board's SelectionBar move-menu test).
  const trigger = await screen.findByText('col.roadmap.label')

  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
  await screen.findByRole('menu')
}

describe('drawer StatusMenu — roadmap → ready confirmation', () => {
  it('picking Ready opens the confirm and writes NOTHING until confirmed', async () => {
    fetchTaskMock.mockResolvedValue(detail())

    mount(drawer())
    await openStatusMenu()

    fireEvent.click(screen.getByText('col.ready.label'))

    // The confirm is up and no write has fired — same contract as the
    // board's spawn-Ready gate (spawnReadyBody / spawnReadyConfirm copy).
    expect(await screen.findByText('spawnReadyBody')).toBeTruthy()
    expect(patchTaskMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('spawnReadyConfirm'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalledWith('t_road', { status: 'ready' }, undefined))
  })

  it('a rejected confirm stays open and shows the failure — the dialog does not close on a failed spawn', async () => {
    fetchTaskMock.mockResolvedValue(detail())
    patchTaskMock.mockRejectedValue(new Error('400 {"detail":"invalid roadmap lane transition"}'))

    mount(drawer())
    await openStatusMenu()
    fireEvent.click(screen.getByText('col.ready.label'))
    await screen.findByText('spawnReadyBody')

    fireEvent.click(screen.getByText('spawnReadyConfirm'))

    await waitFor(() => expect(patchTaskMock).toHaveBeenCalled())
    // ConfirmDialog's own contract: onConfirm rejecting surfaces the error
    // inline (the dialog's own catch, same as ArchiveDoneControl's) and the
    // dialog stays open (`status` falls back to 'idle', not 'done') rather
    // than closing as if the spawn had succeeded.
    await waitFor(() => expect(screen.getByText(/invalid roadmap lane transition/)).toBeTruthy())
    expect(screen.getByText('spawnReadyBody')).toBeTruthy()
  })

  it('Cancel dismisses without writing', async () => {
    fetchTaskMock.mockResolvedValue(detail())

    mount(drawer())
    await openStatusMenu()
    fireEvent.click(screen.getByText('col.ready.label'))
    await screen.findByText('spawnReadyBody')

    fireEvent.click(screen.getByText('Cancel'))

    await waitFor(() => expect(screen.queryByText('spawnReadyBody')).toBeNull())
    expect(patchTaskMock).not.toHaveBeenCalled()
  })
})
