import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import type { VirtualSessionListProps } from './virtual-session-list'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        },
        nav: { 'new-session': 'New session' },
        projects: { toggle: (label: string, open: boolean) => `${open ? 'Hide' : 'Show'} ${label}` },
        row: { archiveSession: 'Archive session' }
      }
    }
  })
}))

const mockVirtualListPropsHistory: VirtualSessionListProps[] = []

vi.mock('./virtual-session-list', () => ({
  VirtualSessionList: (props: VirtualSessionListProps) => {
    mockVirtualListPropsHistory.push(props)

    return <div data-testid="virtual-session-list">Virtual List ({props.rows.length} rows)</div>
  }
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <div data-testid={`session-row-${session.id}`}>{session.id}</div>
  )
}))

function makeSession(id: string, startedAt = 1000): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  } as unknown as SessionInfo
}

function generateSessions(count: number): SessionInfo[] {
  return Array.from({ length: count }, (_, i) => makeSession(`session-${i + 1}`, 10000 - i * 100))
}

const noop = () => {}

describe('SidebarSessionsSection memoization & virtualizer stability', () => {
  it('memoizes flatRows and passes the exact same rows array reference across parent re-renders', () => {
    mockVirtualListPropsHistory.length = 0

    const sessions = generateSessions(VIRTUALIZE_THRESHOLD + 5)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(1)
    const initialRowsRef = mockVirtualListPropsHistory[0].rows
    expect(initialRowsRef.length).toBeGreaterThan(VIRTUALIZE_THRESHOLD)

    // Re-render parent with the exact same sessions array and props
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(2)
    const nextRowsRef = mockVirtualListPropsHistory[1].rows

    // Confirm that the flatRows array reference remains strictly identical across renders (useMemo proof)
    expect(nextRowsRef).toBe(initialRowsRef)
  })

  it('re-computes flatRows reference when grouping or sessions change', () => {
    mockVirtualListPropsHistory.length = 0

    const initialSessions = generateSessions(VIRTUALIZE_THRESHOLD + 2)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="none"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const firstRowsRef = mockVirtualListPropsHistory[0].rows

    // Switch on date dividers
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const secondRowsRef = mockVirtualListPropsHistory[1].rows
    expect(secondRowsRef).not.toBe(firstRowsRef)

    // Change sessions array identity
    const updatedSessions = generateSessions(VIRTUALIZE_THRESHOLD + 4)
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={updatedSessions}
      />
    )

    const thirdRowsRef = mockVirtualListPropsHistory[2].rows
    expect(thirdRowsRef).not.toBe(secondRowsRef)
  })

  // Bulk date/status-group archive (`archiveDateGroup` +
  // `SidebarDateDividerArchiveButton`) is removed by this card: a date
  // divider must never carry an archive affordance, in the flat renderer or
  // an entered project's dated lane. `newSessionDividerAction` (the "+" to
  // start a session in that bucket) is the only surviving divider action.
  it('renders no bulk archive action on date dividers in the flat (non-virtualized) list', () => {
    const onArchiveSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={[makeSession('today'), makeSession('yesterday', 900)]}
      />
    )

    expect(screen.queryByRole('button', { name: /Archive session/i })).toBeNull()
    expect(onArchiveSession).not.toHaveBeenCalled()
  })

  it('renders no bulk archive action on date dividers inside an entered project', () => {
    const onArchiveSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        projectContent={{
          id: 'home',
          isNoProject: true,
          label: 'Home',
          path: null,
          repos: [
            {
              groups: [
                {
                  id: 'home-lane',
                  label: 'Home',
                  path: null,
                  sessions: [makeSession('today'), makeSession('yesterday', 900)]
                }
              ],
              id: 'home-repo',
              label: 'Home',
              path: null,
              sessionCount: 2
            }
          ],
          sessionCount: 2
        }}
        sessions={[]}
      />
    )

    expect(screen.queryByRole('button', { name: /Archive session/i })).toBeNull()
    expect(onArchiveSession).not.toHaveBeenCalled()
  })

  // AC4 (reviewer round 1): the virtualized path must receive the same
  // inverse-action callback the flat renderer wires — proves the chain from
  // SidebarSessionsSection down to VirtualSessionList's own archived-row
  // routing (asserted in virtual-session-list.test.tsx) is unbroken at
  // >=VIRTUALIZE_THRESHOLD sessions, where the section actually switches to
  // the virtualized renderer.
  it('forwards onUnarchiveSession to VirtualSessionList once virtualized', () => {
    mockVirtualListPropsHistory.length = 0
    const onUnarchiveSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        onUnarchiveSession={onUnarchiveSession}
        open={true}
        pinned={false}
        sessions={generateSessions(VIRTUALIZE_THRESHOLD + 5)}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(1)
    expect(mockVirtualListPropsHistory[0].onUnarchiveSession).toBe(onUnarchiveSession)
  })

  // AC6 (reviewer round 2, recovery t_77c22e64): the tests above (lines 192,
  // 217) prove the FLAT renderer's divider carries no bulk archive control,
  // and 'forwards onUnarchiveSession...' proves callback wiring against the
  // bare mocked VirtualSessionList — neither exercises a REAL virtual divider.
  // This test unmocks './virtual-session-list' (and stubs '@tanstack/
  // react-virtual' so the divider row is deterministically among the
  // virtualizer's rendered items) to render the actual SidebarDateDivider
  // component through the virtualized path at >=VIRTUALIZE_THRESHOLD
  // sessions, and asserts no bulk Archive control renders on it while the
  // ordinary new-session divider action still does.
  it('renders no bulk archive action on a REAL virtualized date divider, preserving the new-session action', async () => {
    vi.resetModules()
    vi.doUnmock('./virtual-session-list')
    vi.doMock('@tanstack/react-virtual', () => ({
      useVirtualizer: ({ count }: { count: number }) => ({
        getTotalSize: () => count * 40,
        getVirtualItems: () =>
          Array.from({ length: count }, (_, index) => ({ end: (index + 1) * 40, index, start: index * 40 })),
        measure: () => {},
        measureElement: () => {}
      })
    }))
    vi.doMock('@dnd-kit/sortable', () => ({ useSortable: () => ({ attributes: {}, listeners: {} }) }))
    vi.doMock('@dnd-kit/utilities', () => ({ CSS: { Transform: { toString: () => '' } } }))

    const { SidebarSessionsSection: RealVirtualSessionsSection } = await import('./sessions-section')

    const onArchiveSession = vi.fn()
    const onNewSessionInWorkspace = vi.fn()

    const nowSec = Date.now() / 1000

    // Two calendar days, split by a real >8h gap so headRunCutoffMs actually
    // ends the head run and groupEntriesByRecency emits a real "Yesterday"
    // divider between them (a tight cluster of close timestamps never breaks
    // into groups at all — this must span a genuine day boundary).
    const todaySessions = generateSessions(15).map((session, i) => ({
      ...session,
      id: `today-${i}`,
      last_active: nowSec - i * 60,
      started_at: nowSec - i * 60
    })) as SessionInfo[]

    const yesterdaySessions = generateSessions(15).map((session, i) => ({
      ...session,
      id: `yesterday-${i}`,
      last_active: nowSec - 26 * 60 * 60 - i * 60,
      started_at: nowSec - 26 * 60 * 60 - i * 60
    })) as SessionInfo[]

    render(
      <RealVirtualSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onNewSessionInWorkspace={onNewSessionInWorkspace}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={[...todaySessions, ...yesterdaySessions]}
      />
    )

    // A real divider rendered — proves this exercised the virtualized path,
    // not a degenerate empty list.
    expect(screen.getByText('Yesterday')).toBeTruthy()

    expect(screen.queryByRole('button', { name: /Archive session/i })).toBeNull()
    expect(onArchiveSession).not.toHaveBeenCalled()

    // The new-session "+" divider action is the one surviving divider
    // affordance — it must still be present and functional.
    const newSessionButton = screen.getAllByRole('button', { name: 'New session' })[0]
    fireEvent.click(newSessionButton)
    expect(onNewSessionInWorkspace).toHaveBeenCalledWith(null)
  })
})
