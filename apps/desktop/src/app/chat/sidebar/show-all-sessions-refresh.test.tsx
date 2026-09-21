// @vitest-environment jsdom
//
// Regression (review round 1 on t_778b580c): SidebarWorkspaceSection read
// $sidebarShowAllSessions to widen the renderer's own slice/preview limit,
// but nothing in the component actually re-issued the `projects.tree`
// request at the wider preview_limit. Upstream commit a6474766c8 wires the
// toggle to refreshProjectTree() precisely so checking "Show all sessions"
// after the initial (preview_limit: 3) tree has already loaded fetches the
// rest of the rows instead of only ever showing what was already cached.
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $sidebarShowAllSessions, resetSidebarView, setSidebarGrouping } from '@/store/layout'
import { refreshProjectTree } from '@/store/projects'
import { setGatewayState } from '@/store/session'

import { ChatSidebar } from './index'

vi.mock('@/store/projects', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  refreshProjectTree: vi.fn(async () => {})
}))

const refreshProjectTreeMock = vi.mocked(refreshProjectTree)

const noop = () => {}

const noopAsync = async () => {}

const renderSidebar = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onTriggerCronJob={noopAsync}
          onUnarchiveSession={noop}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

describe('Show all sessions toggle refreshes the project tree', () => {
  beforeEach(() => {
    refreshProjectTreeMock.mockClear()
    setGatewayState('open')
    setSidebarGrouping('project')
  })

  afterEach(() => {
    cleanup()
    resetSidebarView()
    setGatewayState('idle')
  })

  it('re-issues projects.tree at the widened preview when the setting is turned on', async () => {
    renderSidebar()

    // The initial mount already refreshes the tree at the default preview
    // limit (grouping is 'project' before render) — that call is not what
    // this test is about, so it's excluded before the assertion.
    refreshProjectTreeMock.mockClear()

    await act(async () => {
      $sidebarShowAllSessions.set(true)
      await Promise.resolve()
    })

    expect(refreshProjectTreeMock).toHaveBeenCalled()
  })

  it('does not refetch when Show all sessions changes while grouping is not project', async () => {
    setSidebarGrouping('date')
    renderSidebar()
    refreshProjectTreeMock.mockClear()

    await act(async () => {
      $sidebarShowAllSessions.set(true)
      await Promise.resolve()
    })

    expect(refreshProjectTreeMock).not.toHaveBeenCalled()
  })
})
