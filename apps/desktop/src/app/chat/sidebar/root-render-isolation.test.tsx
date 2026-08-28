// Root-level render-isolation contract for the sidebar decomposition
// (t_4d2f3e30, round 2 review): ChatSidebar itself must not re-render on an
// ordinary $sessions publication while search is inactive. Round 1 left two
// session-derived subscriptions ($sidebarSortedSessions, $sidebarSessionByAnyId)
// on the root for the search feature; round 2 moved those into
// SidebarSearchSection, mounted only while a query is active, so the root now
// carries zero session-derived stores.
//
// Detection strategy: every section child is mocked as a `memo()`'d spy. Two
// of the root's own callback props to those children (onToggle for Pins/Cron)
// are inline closures recreated on every ChatSidebar render, so if ChatSidebar
// re-executes for ANY reason its children's props change reference and the
// memoized spies re-render. If ChatSidebar bails out (no subscribed store
// changed), React never re-invokes its render function and the child elements
// are never freshly created, so the memoized spies stay silent — the same
// asymmetry render-isolation.test.tsx exploits one level down for the dot-state
// tick.
import { act, cleanup, render } from '@testing-library/react'
import { memo } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const {
  $bindings,
  $focusedStoredSessionId,
  $newChatProfile,
  $panesFlipped,
  $sessions,
  $sidebarCronOpen,
  $sidebarPinsOpen,
  $sidebarShowSessionSections,
  $sidebarWorktreeGroupingActive
} = vi.hoisted(() => {
  function fakeAtom<T>(initial: T) {
    let value = initial
    const listeners = new Set<(next: T) => void>()

    return {
      get: () => value,
      listen: (fn: (next: T) => void) => {
        listeners.add(fn)

        return () => listeners.delete(fn)
      },
      set: (next: T) => {
        value = next
        listeners.forEach(fn => fn(next))
      },
      subscribe: (fn: (next: T) => void) => {
        fn(value)
        listeners.add(fn)

        return () => listeners.delete(fn)
      }
    }
  }

  return {
    $bindings: fakeAtom<Record<string, unknown[]>>({}),
    $focusedStoredSessionId: fakeAtom<null | string>(null),
    $newChatProfile: fakeAtom<null | string>(null),
    $panesFlipped: fakeAtom(false),
    $sessions: fakeAtom<Array<{ id: string; unread?: boolean }>>([]),
    $sidebarCronOpen: fakeAtom(false),
    $sidebarPinsOpen: fakeAtom(false),
    $sidebarShowSessionSections: fakeAtom(true),
    $sidebarWorktreeGroupingActive: fakeAtom(false)
  }
})

vi.mock('@/contrib/react/use-contributions', () => ({ useContributions: () => [] }))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        cronJobs: 'Cron jobs',
        nav: {},
        pinned: 'Pinned',
        row: { openInSplit: 'Open in split', unreadFailed: 'Failed' },
        searchAria: 'Search sessions',
        searchPlaceholder: 'Search…'
      }
    }
  })
}))

vi.mock('react-router', () => ({ useLocation: () => ({ pathname: '/' }) }))

vi.mock('@/store/keybinds', () => ({ $bindings }))
vi.mock('@/store/layout', () => ({
  $panesFlipped,
  $sidebarCronOpen,
  $sidebarPinsOpen,
  pinSession: vi.fn(),
  SESSION_SEARCH_FOCUS_EVENT: 'hermes:focus-session-search',
  setSidebarCronOpen: (next: boolean) => $sidebarCronOpen.set(next),
  setSidebarPinsOpen: (next: boolean) => $sidebarPinsOpen.set(next)
}))
vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))
vi.mock('@/store/profile', () => ({ $newChatProfile }))
vi.mock('@/store/projects', () => ({ openProjectCreate: vi.fn() }))
vi.mock('@/store/route-tiles', () => ({ openRouteTile: vi.fn() }))
vi.mock('@/store/session', () => ({ $sessions }))
vi.mock('@/store/session-states', () => ({ $focusedStoredSessionId }))
vi.mock('@/store/session-unread-remote', () => ({ markSessionUnread: vi.fn().mockResolvedValue(undefined) }))
// The two per-row stores that lived on the root through round 1 are
// deliberately ABSENT from this mock — if index.tsx still imported
// $sidebarSortedSessions/$sidebarSessionByAnyId, this test file would fail to
// resolve them and every test below would error out, not silently pass.
vi.mock('@/store/sidebar-model', () => ({ $sidebarShowSessionSections, $sidebarWorktreeGroupingActive }))

vi.mock('../../routes', () => ({
  ARTIFACTS_ROUTE: '/artifacts',
  CRON_ROUTE: '/cron',
  MESSAGING_ROUTE: '/messaging',
  SIDEBAR_NAV_AREA: 'sidebar-nav',
  SKILLS_ROUTE: '/skills'
}))

const { renderCounts, spySection } = vi.hoisted(() => {
  const counts = new Map<string, number>()

  return {
    renderCounts: counts,
    spySection: (name: string) =>
      memo(() => {
        counts.set(name, (counts.get(name) ?? 0) + 1)

        return null
      })
  }
})

vi.mock('./chrome', () => ({ SIDEBAR_SCROLL_Y: 'overflow-y-auto' }))
vi.mock('./cron-jobs-section', () => ({ SidebarCronJobsSection: spySection('cron') }))
vi.mock('./messaging-sections', () => ({ SidebarMessagingSections: spySection('messaging') }))
vi.mock('./pins-section', () => ({ SidebarPinsSection: spySection('pins') }))
vi.mock('./profile-switcher', () => ({ ProfileRail: spySection('profileRail') }))
vi.mock('./project-dialog', () => ({ ProjectDialog: spySection('projectDialog') }))
vi.mock('./projects/worktree-dialog', () => ({ WorktreeDialog: spySection('worktreeDialog') }))
vi.mock('./search-section', () => ({ SidebarSearchSection: spySection('search') }))
vi.mock('./section-states', () => ({ SidebarBlankState: spySection('blankState') }))
vi.mock('./split-submenu', () => ({
  CONTEXT_SPLIT_KIT: {},
  SplitSubmenu: () => null
}))
vi.mock('./workspace-section', () => ({ SidebarWorkspaceSection: spySection('workspace') }))

// NOTE: these two imports intentionally sit BELOW the vi.mock() calls above —
// the module mocks must be registered before these modules resolve.
import { SidebarProvider } from '@/components/ui/sidebar'

import { ChatSidebar } from './index'

const noop = () => {}

afterEach(() => {
  cleanup()
  renderCounts.clear()
  $panesFlipped.set(false)
  $sidebarCronOpen.set(false)
  $sidebarPinsOpen.set(false)
  $focusedStoredSessionId.set(null)
  $bindings.set({})
  $sidebarShowSessionSections.set(true)
  $sidebarWorktreeGroupingActive.set(false)
  $newChatProfile.set(null)
  $sessions.set([])
})

function Harness() {
  return (
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
        onTriggerCronJob={() => Promise.resolve()}
      />
    </SidebarProvider>
  )
}

describe('ChatSidebar root render isolation ($sessions tick, inactive search)', () => {
  it('an ordinary $sessions publication does not re-render the root while search is inactive', () => {
    render(<Harness />)

    const afterMount = new Map(renderCounts)

    expect(afterMount.get('pins')).toBe(1)
    expect(afterMount.get('workspace')).toBe(1)
    expect(afterMount.get('messaging')).toBe(1)
    expect(afterMount.get('cron')).toBe(1)
    expect(afterMount.get('search')).toBeUndefined()

    act(() => {
      $sessions.set([{ id: 's1' }, { id: 's2' }])
    })

    // The root itself carries zero session-derived stores (post round-2), so
    // this commit must not touch it — the section spies must not re-render.
    expect(renderCounts.get('pins')).toBe(afterMount.get('pins'))
    expect(renderCounts.get('workspace')).toBe(afterMount.get('workspace'))
    expect(renderCounts.get('messaging')).toBe(afterMount.get('messaging'))
    expect(renderCounts.get('cron')).toBe(afterMount.get('cron'))
  })

  it('sanity: a genuine layout/scope change (pins-open toggle) DOES re-render the root', () => {
    render(<Harness />)

    const afterMount = new Map(renderCounts)

    // Proves the harness is sensitive enough to catch a real root re-render —
    // otherwise the first test's "no change" assertion would be vacuous.
    act(() => {
      $sidebarPinsOpen.set(true)
    })

    expect(renderCounts.get('pins')).toBe((afterMount.get('pins') ?? 0) + 1)
  })
})
