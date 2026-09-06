/**
 * Behavior contracts for the redesigned, tabbed task drawer.
 *
 * Two things the tab split can silently break, and one the collapsed
 * description can:
 *
 *  1. The CTA banner's "Reply" is a DEEP LINK. Comments moved to the Activity
 *     tab, so clicking Reply from Overview must switch tabs and land focus in
 *     the composer. A silently dead affordance is the most likely regression
 *     in this change, so it is asserted end to end against the real drawer.
 *  2. Every tab's content must be reachable in one click — Activity's feed and
 *     Log's worker output are the whole point of the redesign.
 *  3. Inline description editing must hand the user the RAW markdown source,
 *     never the rendered output.
 *
 * Exercises the real component tree via @hermes/plugin-sdk, matching the
 * pattern in drawer.cta.test.tsx / drawer.images.test.tsx.
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

vi.mock('./api', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./api')

  return {
    ...actual,
    fetchLog: (...args: unknown[]) => fetchLogMock(...args),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    fetchTask: (...args: unknown[]) => fetchTaskMock(...args)
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  // jsdom ships no rAF; the drawer's deep-link focus and the Loader animation
  // both need one. It must be ASYNC — a synchronous shim turns the Loader's
  // self-scheduling render into unbounded recursion.
  window.requestAnimationFrame = (cb: FrameRequestCallback) =>
    setTimeout(() => cb(performance.now()), 0) as unknown as number
  window.cancelAnimationFrame = (handle: number) => clearTimeout(handle as unknown as NodeJS.Timeout)
})

beforeEach(() => {
  fetchTaskMock.mockReset()
  fetchLogMock.mockReset()
})

afterEach(() => {
  cleanup()
})

const LONG_MARKDOWN = ['## A heading', '', 'Body with `code` and a fence:', '', '```sh', 'echo hi', '```'].join('\n')

// A manual `kanban_block(reason=...)` event — the CtaBanner resolves its
// origin through `resolveBlockCause` (status-guidance.ts), so a default
// fixture needs an actual `blocked` event to land on the manual arm (Reply +
// Unblock); a bare `created` event resolves to `unknown` and drops Reply.
const detail = (overrides: Partial<KanbanTaskDetail> = {}): KanbanTaskDetail => ({
  attachments: [],
  comments: [],
  events: [{ created_at: 10, id: 1, kind: 'blocked', payload: { reason: 'Which API key should I use?' } }],
  links: { children: [], parents: [] },
  runs: [],
  task: {
    block_kind: 'needs_input',
    body: LONG_MARKDOWN,
    id: 't_abc123',
    status: 'blocked',
    title: 'A blocked card'
  },
  ...overrides
})

function mount(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function drawer() {
  return <TaskDrawer columns={['ready', 'blocked', 'done']} id="t_abc123" onClose={vi.fn()} onOpen={vi.fn()} />
}

describe('tabbed task drawer', () => {
  it('starts on Overview with the description, and reaches Activity and Log in one click each', async () => {
    fetchTaskMock.mockResolvedValue(
      detail({ events: [{ created_at: 10, id: 1, kind: 'spawned', payload: { pid: 42 } }] })
    )
    fetchLogMock.mockResolvedValue({ content: 'worker stdout line', exists: true, size_bytes: 18, truncated: false })

    mount(drawer())

    // Overview: the description is rendered markdown, not literal source.
    await waitFor(() => expect(screen.getByText('A heading')).toBeTruthy())
    expect(screen.queryByText(/^## A heading$/)).toBeNull()
    // The Activity feed and the worker log are NOT on Overview.
    expect(screen.queryByText('worker stdout line')).toBeNull()

    // One click reaches Activity — the composer mounts with it.
    fireEvent.click(screen.getByRole('tab', { name: /tabActivity/ }))
    await waitFor(() => expect(globalThis.document.querySelector('[data-kanban-comment-input="true"]')).toBeTruthy())

    // One click reaches Log — the worker output is there.
    fireEvent.click(screen.getByRole('tab', { name: /tabLog/ }))
    await waitFor(() => expect(screen.getByText(/worker stdout line/)).toBeTruthy())
  })

  it('CTA "Reply" switches to Activity and focuses the comment composer', async () => {
    fetchTaskMock.mockResolvedValue(detail())
    fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })

    mount(drawer())

    // The banner is on Overview; the composer it targets is on Activity, so
    // before the click the composer must not be mounted at all.
    const reply = await screen.findByRole('button', { name: /ctaReply/ })
    expect(globalThis.document.querySelector('[data-kanban-comment-input="true"]')).toBeNull()

    fireEvent.click(reply)

    await waitFor(() => {
      const input = globalThis.document.querySelector('[data-kanban-comment-input="true"]')

      expect(input).toBeTruthy()
      expect(globalThis.document.activeElement).toBe(input)
    })
  })

  it('inline description editing exposes the raw markdown source, not the rendered output', async () => {
    fetchTaskMock.mockResolvedValue(detail())
    fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })

    mount(drawer())

    fireEvent.click(await screen.findByRole('button', { name: /editDescription/ }))

    const editor = await waitFor(() => {
      const el = globalThis.document.querySelector<HTMLTextAreaElement>('[data-kanban-description-input="true"]')

      expect(el).toBeTruthy()

      return el!
    })

    expect(editor.value).toBe(LONG_MARKDOWN)
  })

  it('separates a running attempt age from the card creation age and surfaces a retry count', async () => {
    const now = Math.floor(Date.now() / 1000)
    const base = detail()

    fetchTaskMock.mockResolvedValue(
      detail({
        runs: [
          { ended_at: now - 3_600, id: 1, started_at: now - 4_200, status: 'crashed' },
          { id: 2, started_at: now - 60, status: 'running' }
        ],
        task: { ...base.task, created_at: now - 14_400, started_at: now - 60, status: 'running', worker_pid: 42 }
      })
    )
    fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })

    mount(drawer())

    await screen.findByText('metaCreated')
    expect(screen.getByText('metaRunStarted')).toBeTruthy()
    expect(screen.getByText('metaRunCount')).toBeTruthy()
  })

  it('does not add a retry count to a running card with only one run', async () => {
    const now = Math.floor(Date.now() / 1000)
    const base = detail()

    fetchTaskMock.mockResolvedValue(
      detail({
        runs: [{ id: 1, started_at: now - 60, status: 'running' }],
        task: { ...base.task, created_at: now - 14_400, started_at: now - 60, status: 'running', worker_pid: 42 }
      })
    )
    fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })

    mount(drawer())

    await screen.findByText('metaCreated')
    expect(screen.getByText('metaRunStarted')).toBeTruthy()
    expect(screen.queryByText('metaRunCount')).toBeNull()
  })

  it('does not show a live run-start row on a terminal card', async () => {
    const now = Math.floor(Date.now() / 1000)
    const base = detail()

    fetchTaskMock.mockResolvedValue(
      detail({
        runs: [{ ended_at: now - 30, id: 1, started_at: now - 60, status: 'completed' }],
        task: { ...base.task, created_at: now - 14_400, status: 'done' }
      })
    )
    fetchLogMock.mockResolvedValue({ content: '', exists: false, size_bytes: 0, truncated: false })

    mount(drawer())

    await screen.findByText('metaCreated')
    expect(screen.queryByText('metaRunStarted')).toBeNull()
  })
})
