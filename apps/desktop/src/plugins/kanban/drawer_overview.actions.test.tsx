import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BOARDS_KEY } from './api'
import { DependenciesSection, HermesActionsSection } from './drawer_overview'
import type { BoardMeta, KanbanTaskDetail } from './types'

const board: BoardMeta = {
  default_workdir: '/board/project',
  name: 'Shipping Board',
  project_id: 'project-42',
  project_name: 'Hermes Desktop',
  slug: 'shipping'
}

const detail = (status = 'idea'): KanbanTaskDetail => ({
  attachments: [],
  comments: [],
  events: [],
  links: { children: [], parents: [] },
  runs: [],
  task: {
    id: 't_exact123',
    status,
    title: 'Seeded action card',
    workspace_path: '/card/worktree'
  }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('HermesActionsSection', () => {
  it('clicks through to a fresh, focused session with an editable unsent draft', () => {
    const open = vi.spyOn(host, 'newChatWithContext').mockImplementation(() => undefined)
    const loaded = detail()

    render(<HermesActionsSection board={board} detail={loaded} task={loaded.task} />)
    fireEvent.click(screen.getByRole('button', { name: 'hermesActionRough' }))

    expect(open).toHaveBeenCalledOnce()
    expect(open).toHaveBeenCalledWith({
      cwd: '/card/worktree',
      draft: expect.stringMatching(/t_exact123[\s\S]*kanban-card-workflow §2c/),
      openTab: true
    })
    expect(open.mock.calls[0][0]).not.toHaveProperty('send')
    expect(open.mock.calls[0][0]).not.toHaveProperty('submit')
  })

  it('opens an editable detached draft when no card or board workdir exists', () => {
    const open = vi.spyOn(host, 'newChatWithContext').mockImplementation(() => undefined)
    const loaded = detail('blocked')
    loaded.task.workspace_path = ' '

    render(<HermesActionsSection board={{ ...board, default_workdir: null }} detail={loaded} task={loaded.task} />)

    const explain = screen.getByRole('button', { name: 'hermesActionExplain' }) as HTMLButtonElement
    const unblock = screen.getByRole('button', { name: 'hermesActionUnblock' }) as HTMLButtonElement
    expect(explain.disabled).toBe(false)
    expect(unblock.disabled).toBe(false)
    fireEvent.click(explain)
    expect(open).toHaveBeenCalledWith({
      cwd: undefined,
      draft: expect.stringMatching(/t_exact123[\s\S]*board shipping/),
      openTab: true
    })
    expect(open.mock.calls[0][0]).not.toHaveProperty('send')
    expect(open.mock.calls[0][0]).not.toHaveProperty('submit')
  })

  it('reacts to already-owned board cache updates without fetching action metadata', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const open = vi.spyOn(host, 'newChatWithContext').mockImplementation(() => undefined)
    const loaded = detail('ready')
    loaded.task.workspace_path = null

    render(
      <QueryClientProvider client={client}>
        <DependenciesSection
          detail={loaded}
          onLink={vi.fn()}
          onOpen={vi.fn()}
          onUnlink={vi.fn()}
          slug="shipping"
          task={loaded.task}
        />
      </QueryClientProvider>
    )

    const explain = screen.getByRole('button', { name: 'hermesActionExplain' }) as HTMLButtonElement
    expect(explain.disabled).toBe(false)

    await act(() => client.setQueryData(BOARDS_KEY, { boards: [board], current: 'shipping' }))
    expect(explain.disabled).toBe(false)

    fireEvent.click(explain)
    expect(open).toHaveBeenCalledWith({
      cwd: '/board/project',
      draft: expect.stringMatching(/board shipping[\s\S]*Hermes Desktop \/ project-42/),
      openTab: true
    })
  })
})
