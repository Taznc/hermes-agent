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

  it('archives only the sessions beneath the clicked date divider', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-06T16:00:00Z'))
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
        sessions={[
          makeSession('today', Date.parse('2026-09-06T15:00:00Z') / 1000),
          makeSession('yesterday-a', Date.parse('2026-09-05T15:00:00Z') / 1000),
          makeSession('yesterday-b', Date.parse('2026-09-05T14:00:00Z') / 1000)
        ]}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Archive session: Yesterday' }))

    expect(onArchiveSession).toHaveBeenCalledTimes(2)
    expect(onArchiveSession).toHaveBeenNthCalledWith(1, 'yesterday-a')
    expect(onArchiveSession).toHaveBeenNthCalledWith(2, 'yesterday-b')
    vi.useRealTimers()
  })
})
