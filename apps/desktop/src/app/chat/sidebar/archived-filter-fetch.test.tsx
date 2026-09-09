// @vitest-environment jsdom
//
// Regression: turning the sidebar's "Archived" filter on showed an empty list.
// Archived rows are excluded from the sessions query by design, so they live in
// their own store ($archivedSessions) which ONLY loadArchivedSessions() fills.
// This fork's sidebar lost that call site during the workspace-section
// divergence from upstream, so $sidebarScopedSessions swapped to a store nobody
// populated — permanently empty, no matter how many archived sessions existed.
//
// The contract asserted here is the relationship, not a snapshot: turning the
// filter ON fetches the archived set, and the rows it returns reach the store
// the Archived view reads.
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { listAllProfileSessions } from '@/hermes'
import { $sidebarShowArchived } from '@/store/layout'
import { $sessions } from '@/store/session'
import { $archivedSessions } from '@/store/sidebar-archive'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listAllProfileSessions: vi.fn()
}))

const listAllProfileSessionsMock = vi.mocked(listAllProfileSessions)

const noop = () => {}

const noopAsync = async () => {}

const archivedRow = makeSessionInfo({
  archived: true,
  id: 'archived-one',
  last_active: 2,
  profile: 'default',
  started_at: 1,
  title: 'Archived one'
})

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
        />
      </SidebarProvider>
    </MemoryRouter>
  )

describe('sidebar Archived filter', () => {
  beforeEach(() => {
    listAllProfileSessionsMock.mockReset()
    listAllProfileSessionsMock.mockResolvedValue({
      limit: 200,
      offset: 0,
      sessions: [archivedRow],
      total: 1
    })
    $sessions.set([])
    $archivedSessions.set([])
    $sidebarShowArchived.set(false)
  })

  afterEach(() => {
    cleanup()
    $sessions.set([])
    $archivedSessions.set([])
    $sidebarShowArchived.set(false)
  })

  it('does not fetch the archived set while the filter is off', () => {
    renderSidebar()

    expect(listAllProfileSessionsMock.mock.calls.some(call => call[2] === 'only')).toBe(false)
  })

  it('fetches archived sessions into the store the Archived view reads', async () => {
    renderSidebar()

    await act(async () => {
      $sidebarShowArchived.set(true)
      await Promise.resolve()
    })

    // The archived-only slice is its own query — asserted by the `archived`
    // argument rather than a call count, so unrelated sidebar fetches on the
    // same helper cannot make this pass or fail spuriously.
    expect(listAllProfileSessionsMock.mock.calls.some(call => call[2] === 'only')).toBe(true)
    expect($archivedSessions.get().map(session => session.id)).toEqual(['archived-one'])
  })
})
