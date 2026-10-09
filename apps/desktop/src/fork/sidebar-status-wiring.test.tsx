import { act, cleanup, render, screen } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatSidebar } from '@/app/chat/sidebar'
import { SidebarSessionsSection } from '@/app/chat/sidebar/sessions-section'
import { SidebarProvider } from '@/components/ui/sidebar'
import { $sidebarProfileFilter, $sidebarStatusFilter, setSidebarGrouping, setSidebarOrdering } from '@/store/layout'
import { $profiles, setShowAllProfiles } from '@/store/profile'
import { $sessions } from '@/store/session'
import type * as DotModule from '@/store/session-dot-state'
import { $sessionDotStateById } from '@/store/session-dot-state'
import type { SessionDotState } from '@/store/session-dot-state'
import { makeSessionInfo } from '@/test/session-info'

import { $forkSidebarListRows } from './sidebar-group-actions'

vi.mock('@/store/session-dot-state', async importOriginal => {
  const actual = await importOriginal<typeof DotModule>()
  const { atom } = await import('nanostores')

  return { ...actual, $sessionDotStateById: atom<Readonly<Record<string, SessionDotState>>>({}) }
})
const raw = $sessionDotStateById as WritableAtom<Readonly<Record<string, SessionDotState>>>

const noop = () => {}

const sessions = [
  makeSessionInfo({ id: 'root', profile: 'default', title: 'live-default', last_active: 10 }),
  makeSessionInfo({ id: 'tip', _lineage_root_id: 'root', profile: 'work', title: 'live-work', last_active: 20 }),
  makeSessionInfo({ id: 'background', profile: 'work', title: 'background-work', last_active: 30 }),
  makeSessionInfo({ id: 'unknown', profile: 'default', title: 'idle-default', last_active: 40 })
]

function reset() {
  $sessions.set([])
  raw.set({})
  $sidebarStatusFilter.set([])
  $sidebarProfileFilter.set([])
  setSidebarGrouping('date')
  setSidebarOrdering('updated')
  setShowAllProfiles(false)
}

beforeEach(reset)
afterEach(() => {
  cleanup()
  reset()
})

const wrap = (children: React.ReactNode) => (
  <MemoryRouter>
    <SidebarProvider>{children}</SidebarProvider>
  </MemoryRouter>
)

describe('actual sidebar components retain grouping/filter contracts', () => {
  it('section status grouping tracks live membership, not background; manual busy-first order and active filtering remain intact', () => {
    raw.set({ root: 'working', tip: 'needs-input', background: 'background' })

    const props = {
      label: 'Probe',
      open: true,
      pinned: false,
      sessions,
      grouping: 'status' as const,
      activeSessionId: null,
      emptyState: null,
      onToggle: noop,
      onTogglePin: noop,
      onToggleUnread: noop,
      onResumeSession: noop,
      onDeleteSession: noop,
      onArchiveSession: noop,
      manualOrderIds: ['tip', 'root', 'background', 'unknown']
    }

    const view = render(wrap(<SidebarSessionsSection {...props} />))

    const grouped = () =>
      $forkSidebarListRows.get().map(row => (row.kind === 'divider' ? row.key : row.entry.session.id))

    expect(grouped()).toEqual(['status:working', 'tip', 'root', 'status:done', 'background', 'unknown'])
    act(() => raw.set({ root: 'stalled', tip: 'working', background: 'background' }))
    expect(grouped()).toEqual(['status:working', 'tip', 'root', 'status:done', 'background', 'unknown'])
    view.rerender(wrap(<SidebarSessionsSection {...props} sessions={sessions.filter(row => row.profile === 'work')} />))
    expect(grouped()).toEqual(['status:working', 'tip', 'status:done', 'background'])
    act(() => raw.set({}))
    expect(grouped()).toEqual(['status:done', 'tip', 'background'])
  })

  it('root combines bucket filters, profile filters, missing-id idle and independent status ordering', () => {
    $profiles.set([
      { name: 'default', is_default: true },
      { name: 'work', is_default: false }
    ] as typeof $profiles.value)
    setShowAllProfiles(true)
    $sessions.set(sessions)
    raw.set({ root: 'stalled', tip: 'needs-input', background: 'background' })
    $sidebarStatusFilter.set(['working'])
    $sidebarProfileFilter.set(['work'])
    setSidebarOrdering('status')
    render(
      wrap(
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
          onRetrySessions={noop}
          onTriggerCronJob={async () => {}}
        />
      )
    )
    expect(screen.getByText('background-work')).toBeTruthy()
    expect(screen.queryByText('live-default')).toBeNull()
    expect(screen.queryByText('live-work')).toBeNull()
    act(() => {
      $sidebarProfileFilter.set([])
      $sidebarStatusFilter.set(['idle'])
    })
    expect(screen.getByText('idle-default')).toBeTruthy()
    expect(screen.queryByText('background-work')).toBeNull()
  })
})
