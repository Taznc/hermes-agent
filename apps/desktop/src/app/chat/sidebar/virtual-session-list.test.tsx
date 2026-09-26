import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SidebarSessionEntry } from '@/lib/session-branch-tree'
import type { SidebarListRow } from '@/lib/session-date-groups'
import type { SessionInfo } from '@/types/hermes'

import { VirtualSessionList } from './virtual-session-list'

const virtualizer = {
  getTotalSize: () => 68,
  getVirtualItems: () => [
    { end: 26, index: 0, start: 0 },
    { end: 68, index: 1, start: 26 }
  ],
  measure: vi.fn(),
  measureElement: vi.fn()
}

vi.mock('@dnd-kit/sortable', () => ({ useSortable: vi.fn() }))
vi.mock('@dnd-kit/utilities', () => ({ CSS: { Transform: { toString: vi.fn() } } }))
vi.mock('@tanstack/react-virtual', () => ({ useVirtualizer: () => virtualizer }))

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
        }
      }
    }
  })
}))

vi.mock('./chrome', () => ({
  SidebarDateDivider: ({ label, ...props }: { label: string } & React.ComponentProps<'div'>) => (
    <div data-testid={`divider-${label}`} {...props} />
  )
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ onArchive, session }: { onArchive: () => void; session: SessionInfo }) => (
    <button data-testid={`archive-${session.id}`} onClick={onArchive} type="button" />
  )
}))

afterEach(cleanup)

const rows: SidebarListRow[] = [
  { key: 'today', kind: 'divider', label: 'Today' },
  { key: 'older', kind: 'divider', label: 'Older' }
]

const noop = () => {}

describe('VirtualSessionList', () => {
  it('positions measured rows independently within a total-size spacer', () => {
    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={rows}
        sortable={false}
      />
    )

    const firstItem = getByTestId('divider-Today').parentElement
    const secondItem = getByTestId('divider-Older').parentElement
    const spacer = firstItem?.parentElement

    expect(firstItem?.dataset.index).toBe('0')
    expect(firstItem?.style.position).toBe('absolute')
    expect(firstItem?.style.transform).toBe('translateY(0px)')
    expect(secondItem?.dataset.index).toBe('1')
    expect(secondItem?.style.transform).toBe('translateY(26px)')
    expect(spacer?.className).toBe('relative')
    expect(spacer?.style.height).toBe('68px')
    expect(spacer?.style.paddingTop).toBe('')
    expect(spacer?.style.paddingBottom).toBe('')
  })

  it('lets wheel overscroll chain to the outer sidebar scroller (#84964)', () => {
    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={rows}
        sortable={false}
      />
    )

    const scroller = getByTestId('divider-Today').parentElement?.parentElement?.parentElement

    // The inner virtualized scroller must NOT contain overscroll: it is nested
    // inside the sidebar's own scroll container, and containing it swallowed
    // wheel events at the inner scroll boundary — the mid-list wheel dead-zone
    // at 25+ sessions. Chaining stays inside the sidebar because the OUTER
    // scroller keeps overscroll-contain.
    expect(scroller?.className).toContain('overflow-y-auto')
    expect(scroller?.className).not.toContain('overscroll-contain')
  })

  // The card's AC4 regression: an archived row's menu must call the inverse
  // canonical mutation, never re-archive an already-archived session — proven
  // through the virtualized path (>=25 sessions), not just the flat renderer.
  const archivedEntry: SidebarSessionEntry = {
    session: { archived: true, id: 'archived-session' } as SessionInfo
  }

  const archivedRows: SidebarListRow[] = [{ entry: archivedEntry, kind: 'session' }]

  it('routes an archived row to onUnarchiveSession, never onArchiveSession', () => {
    const onArchiveSession = vi.fn()
    const onUnarchiveSession = vi.fn()

    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        onUnarchiveSession={onUnarchiveSession}
        pinned={false}
        rows={archivedRows}
        sortable={false}
      />
    )

    fireEvent.click(getByTestId('archive-archived-session'))

    expect(onUnarchiveSession).toHaveBeenCalledExactlyOnceWith('archived-session')
    expect(onArchiveSession).not.toHaveBeenCalled()
  })

  it('falls back to a no-op on an archived row when no onUnarchiveSession is wired', () => {
    const onArchiveSession = vi.fn()

    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={archivedRows}
        sortable={false}
      />
    )

    fireEvent.click(getByTestId('archive-archived-session'))

    expect(onArchiveSession).not.toHaveBeenCalled()
  })
})

// AC4 (reviewer round 2, recovery t_77c22e64): the tests above prove
// VirtualSessionList's OWN routing logic against the bare-button row mock —
// they never exercise the row's real kebab/context menu. This block renders
// the ACTUAL SidebarSessionRow (and its real ./chrome + ./session-actions-menu
// dependents) through the virtualized path, at a >=25-row archived dataset, so
// a real click on a real Unarchive menu item is what proves the wiring —
// not a synthetic onClick on a stand-in button.
//
// The rest of this file mocks './chrome' and './session-row' with bare
// stand-ins, and @/i18n with a partial (dateDivider-only) catalog — none of
// which the real row can render with (it needs the full row chrome and the
// full t.sidebar.row translations). vi.doUnmock + vi.resetModules gets a
// fresh module graph for just this describe, without disturbing the
// statically-imported (mocked) VirtualSessionList the rest of the file uses.
describe('VirtualSessionList — real archived row through the virtualized path (AC4)', () => {
  it('opens the real Unarchive menu on an archived row and calls onUnarchiveSession once', async () => {
    vi.resetModules()
    vi.doUnmock('./chrome')
    vi.doUnmock('./session-row')
    vi.doUnmock('@/i18n')

    const { VirtualSessionList: RealRowVirtualSessionList } = await import('./virtual-session-list')

    const realArchivedRows: SidebarListRow[] = Array.from({ length: 25 }, (_, i) => ({
      entry: {
        session: {
          archived: true,
          id: `archived-${i}`,
          last_active: Date.now() / 1000 - i,
          profile: 'default',
          started_at: Date.now() / 1000 - i,
          title: `Archived session ${i}`
        } as SessionInfo
      },
      kind: 'session' as const
    }))

    const onArchiveSession = vi.fn()
    const onResumeSession = vi.fn()
    const onUnarchiveSession = vi.fn()

    render(
      <RealRowVirtualSessionList
        activeSessionId={null}
        onArchiveSession={onArchiveSession}
        onDeleteSession={noop}
        onResumeSession={onResumeSession}
        onTogglePin={noop}
        onToggleUnread={noop}
        onUnarchiveSession={onUnarchiveSession}
        pinned={false}
        rows={realArchivedRows}
        sortable={false}
      />
    )

    // The mocked @tanstack/react-virtual stub always yields exactly two virtual
    // items regardless of row count (see the shared `virtualizer` object above)
    // — that's enough to exercise one real archived row through the actual
    // virtualized render path without needing every one of the 25 to paint.
    const [trigger] = screen.getAllByRole('button', { name: 'Session actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    const unarchiveItem = await screen.findByRole('menuitem', { name: /^Unarchive$/i })
    fireEvent.click(unarchiveItem)

    expect(onUnarchiveSession).toHaveBeenCalledExactlyOnceWith('archived-0')
    expect(onArchiveSession).not.toHaveBeenCalled()
    expect(onResumeSession).not.toHaveBeenCalled()
  })
})
