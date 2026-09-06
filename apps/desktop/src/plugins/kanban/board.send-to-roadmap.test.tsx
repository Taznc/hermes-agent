/**
 * Focused tests for the per-card "send to roadmap ideas" context-menu action
 * (Phase 2.15 follow-up to the free-typed board-header capture in
 * board.idea-capture.test.tsx). Exercises the real Card component + its
 * ContextMenu, matching the right-click pattern in app-context-menu.test.tsx.
 * usePluginI18n is stubbed to echo the dotted key so assertions match on
 * stable keys instead of translated English text.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, ALL_BOARDS } from './api'
import { Card } from './board'
import type { KanbanTask } from './types'

const { addRoadmapIdea, notify } = vi.hoisted(() => ({ addRoadmapIdea: vi.fn(), notify: vi.fn() }))

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return { ...actual, addRoadmapIdea: (...args: unknown[]) => addRoadmapIdea(...args) }
})

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return {
    ...actual,
    host: { ...(actual.host as object), notify },
    usePluginI18n: () => (key: string) => key
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  addRoadmapIdea.mockReset()
  notify.mockReset()
})

afterEach(() => {
  cleanup()
  $boardSlug.set('')
})

const task: KanbanTask = {
  id: 't_ab12cd34',
  title: 'Fix the flaky dispatcher test',
  status: 'todo'
}

function Harness({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function renderCard(overrides: Partial<KanbanTask> = {}) {
  return render(
    <Harness>
      <Card
        columns={['todo', 'ready', 'done']}
        onDelete={vi.fn()}
        onMove={vi.fn()}
        onOpen={vi.fn()}
        onSetPriority={vi.fn()}
        onToggleSelect={vi.fn()}
        selected={false}
        task={{ ...task, ...overrides }}
      />
    </Harness>
  )
}

const openMenu = async () => {
  fireEvent.contextMenu(screen.getByText(task.title))
  await screen.findByText('sendToRoadmap')
}

describe('Card — send to roadmap ideas (Phase 2.15 follow-up)', () => {
  it('sends only the card title and id as provenance — never the body', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: true, reason: null })
    renderCard({ body: 'Sensitive internal notes and a token: sk-secret' })

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() => expect(addRoadmapIdea).toHaveBeenCalledTimes(1))

    const [text, sourceId, ...rest] = addRoadmapIdea.mock.calls[0]

    expect(text).toBe(task.title)
    expect(sourceId).toBe(task.id)
    // Only the optional board may follow — never the body, and never more.
    expect(rest).toHaveLength(1)
    expect(JSON.stringify(addRoadmapIdea.mock.calls[0])).not.toContain('Sensitive internal notes')
  })

  // Board routing (t_70f6ac4e #1). POST /roadmap/idea falls back to the ACTIVE
  // board when no board is sent, and roadmap-sync maps each slug to a
  // DIFFERENT file — so an idea from an All Boards card must carry that card's
  // own board or it is appended to the wrong ROADMAP on disk, silently.
  it("carries the card's OWN board, never the all-boards sentinel", async () => {
    addRoadmapIdea.mockResolvedValue({ ok: true, reason: null })
    $boardSlug.set(ALL_BOARDS)
    renderCard({ board: 'homelab', board_name: 'Homelab' })

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() => expect(addRoadmapIdea).toHaveBeenCalledTimes(1))

    const [, , board] = addRoadmapIdea.mock.calls[0]

    expect(board).toBe('homelab')
    expect(board).not.toBe(ALL_BOARDS)
  })

  it('single-board mode sends no board (server resolves its own current board)', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: true, reason: null })
    renderCard()

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() => expect(addRoadmapIdea).toHaveBeenCalledTimes(1))
    expect(addRoadmapIdea.mock.calls[0][2]).toBeUndefined()
  })

  it('on success: notifies with the shared ideaSaved message', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: true, reason: null })
    renderCard()

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', message: 'ideaSaved' }))
    )
  })

  it('on roadmap_unavailable: notifies a distinct warning, not success', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: false, reason: 'roadmap_unavailable' })
    renderCard()

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning', message: 'ideaUnavailable' }))
    )
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
  })

  it('on a thrown network error: notifies an error, does not claim success', async () => {
    addRoadmapIdea.mockRejectedValue(new Error('network down'))
    renderCard()

    await openMenu()
    fireEvent.click(screen.getByText('sendToRoadmap'))

    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'error' })))
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
  })
})
