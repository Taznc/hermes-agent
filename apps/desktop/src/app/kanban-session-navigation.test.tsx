import { useStore } from '@nanostores/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { createMemoryRouter, RouterProvider, useLocation, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { findGroupOfPane, group } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { SidebarProvider } from '@/components/ui/sidebar'
import { registry } from '@/contrib/registry'
import {
  $activeSessionId,
  $selectedStoredSessionId,
  $sessionResumeRequest,
  $sessions,
  requestSessionResume,
  sessionMatchesStoredId,
  setSessions
} from '@/store/session'
import { $focusedStoredSessionId, $sessionTiles } from '@/store/session-states'

import { ChatSidebar } from './chat/sidebar'
import { openSession } from './open-session'
import {
  $workspaceIsPage,
  appViewForPath,
  navigateToWorkspacePage,
  ROUTES_AREA,
  routeSessionId,
  SIDEBAR_NAV_AREA,
  syncWorkspaceRoute
} from './routes'
import { useRouteResume } from './session/hooks/use-route-resume'

vi.mock('@/contrib/react/use-contributions', () => ({
  useContributions: (area: string) =>
    area === SIDEBAR_NAV_AREA
      ? [
          {
            area,
            data: { codicon: 'project', label: 'Kanban', path: '/kanban' },
            id: 'kanban'
          }
        ]
      : []
}))

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

vi.mock('./chat/sidebar/chrome', () => ({ SIDEBAR_SCROLL_Y: 'overflow-y-auto' }))
vi.mock('./chat/sidebar/cron-jobs-section', () => ({ SidebarCronJobsSection: () => null }))
vi.mock('./chat/sidebar/messaging-sections', () => ({ SidebarMessagingSections: () => null }))
vi.mock('./chat/sidebar/pins-section', () => ({ SidebarPinsSection: () => null }))
vi.mock('./chat/sidebar/profile-switcher', () => ({ ProfileRail: () => null }))
vi.mock('./chat/sidebar/project-dialog', () => ({ ProjectDialog: () => null }))
vi.mock('./chat/sidebar/projects/worktree-dialog', () => ({ WorktreeDialog: () => null }))
vi.mock('./chat/sidebar/search-section', () => ({ SidebarSearchSection: () => null }))
vi.mock('./chat/sidebar/section-states', () => ({ SidebarBlankState: () => null }))
vi.mock('./chat/sidebar/split-submenu', () => ({ CONTEXT_SPLIT_KIT: {}, SplitSubmenu: () => null }))
vi.mock('./chat/sidebar/workspace-section', () => ({
  SidebarWorkspaceSection: ({
    activeSessionId,
    onResumeSession
  }: {
    activeSessionId: null | string
    onResumeSession: (sessionId: string) => void
  }) => (
    <>
      <output data-testid="sidebar-active-session">{activeSessionId ?? ''}</output>
      <button onClick={() => onResumeSession('s1')} type="button">
        Open session s1
      </button>
      <button onClick={() => onResumeSession('s2')} type="button">
        Open session s2
      </button>
    </>
  )
}))

const resumeSession = vi.fn(async () => undefined)
const noop = () => undefined

function foreground(pathname: string): string {
  const tree = $layoutTree.get()
  const activePane = tree ? findGroupOfPane(tree, 'workspace')?.active : undefined

  if (activePane?.startsWith('session-tile:')) {
    return `chat:${activePane.slice('session-tile:'.length)}`
  }

  return appViewForPath(pathname) === 'extension' ? 'kanban' : 'main-chat'
}

function NavigationHarness() {
  const location = useLocation()
  const navigate = useNavigate()
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const sessionResumeRequest = useStore($sessionResumeRequest)
  const sessionTiles = useStore($sessionTiles)
  const sessions = useStore($sessions)
  const layoutTree = useStore($layoutTree)
  const routedSessionId = routeSessionId(location.pathname)
  const activeSessionIdRef = useRef(activeSessionId)
  const selectedStoredSessionIdRef = useRef(selectedStoredSessionId)
  const runtimeIdByStoredSessionIdRef = useRef(new Map<string, string>())
  const creatingSessionRef = useRef(false)
  const [gatewayState, setGatewayState] = useState<'closed' | 'open'>('open')

  activeSessionIdRef.current = activeSessionId
  selectedStoredSessionIdRef.current = selectedStoredSessionId

  const preserveSessionTile = Boolean(
    (location.state as { preserveSessionTile?: unknown } | null)?.preserveSessionTile &&
      routedSessionId &&
      sessionTiles.some(tile =>
        sessions.some(
          session =>
            sessionMatchesStoredId(session, tile.storedSessionId) && sessionMatchesStoredId(session, routedSessionId)
        )
      )
  )

  useEffect(() => {
    syncWorkspaceRoute(location.pathname)
  }, [location.pathname])

  useRouteResume({
    activeSessionId,
    activeSessionIdRef,
    creatingSessionRef,
    currentView: appViewForPath(location.pathname),
    freshDraftReady: false,
    gatewayState,
    locationPathname: location.pathname,
    preserveSessionTile,
    resumeExhaustedSessionId: null,
    resumeFailedSessionId: null,
    resumeSession,
    routedSessionId,
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef,
    sessionResumeRequest,
    startFreshSessionDraft: noop
  })

  return (
    <SidebarProvider>
      <ChatSidebar
        currentView={appViewForPath(location.pathname)}
        onArchiveSession={noop}
        onBranchSession={noop}
        onDeleteSession={noop}
        onLoadMoreSessions={noop}
        onManageCronJob={noop}
        onNavigate={item => {
          if (item.route) {
            navigateToWorkspacePage(navigate, item.route)
          }
        }}
        onNewSessionInWorkspace={noop}
        onNewSessionSplit={noop}
        onResumeSession={sessionId => {
          requestSessionResume(sessionId)
          openSession(sessionId, navigate)
        }}
        onTriggerCronJob={() => Promise.resolve()}
        onUnarchiveSession={noop}
      />
      <button onClick={() => setGatewayState('closed')} type="button">
        Close gateway
      </button>
      <button onClick={() => setGatewayState('open')} type="button">
        Open gateway
      </button>
      <output data-testid="location">{`${location.pathname}${location.search}`}</output>
      <output data-testid="view">{appViewForPath(location.pathname)}</output>
      <output data-testid="gateway-state">{gatewayState}</output>
      <output data-testid="foreground">{foreground(location.pathname)}</output>
      <output data-testid="focused-session">{$focusedStoredSessionId.get() ?? ''}</output>
      <output data-testid="tile-layout">
        {JSON.stringify({
          active: layoutTree ? findGroupOfPane(layoutTree, 'workspace')?.active : null,
          tiles: sessionTiles.map(tile => ({
            anchor: tile.anchor,
            before: tile.before,
            dir: tile.dir,
            storedSessionId: tile.storedSessionId
          }))
        })}
      </output>
    </SidebarProvider>
  )
}

describe('Kanban -> sidebar session navigation', () => {
  let disposeRoute: () => void

  beforeEach(() => {
    disposeRoute = registry.register({
      area: ROUTES_AREA,
      data: { path: '/kanban' },
      id: 'test-kanban-route',
      render: () => null
    })
    resumeSession.mockClear()
    setSessions([
      { id: 's1', title: 'Session one' } as never,
      { id: 's2', title: 'Session two' } as never
    ])
    $sessionResumeRequest.set(null)
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessionTiles.set([
      {
        anchor: 'workspace',
        before: null,
        dir: 'center',
        storedSessionId: 's1',
        workspaceMode: 'sessions'
      },
      {
        anchor: 'workspace',
        before: null,
        dir: 'center',
        storedSessionId: 's2',
        workspaceMode: 'sessions'
      }
    ])
    $layoutTree.set(group(['workspace', 'session-tile:s1', 'session-tile:s2'], { active: 'session-tile:s1', id: 'main' }))
    noteActiveTreeGroup('main')
    $workspaceIsPage.set(false)
  })

  afterEach(() => {
    cleanup()
    disposeRoute()
    $sessionResumeRequest.set(null)
    setSessions([])
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessionTiles.set([])
    $layoutTree.set(null)
    noteActiveTreeGroup(null)
    $workspaceIsPage.set(false)
  })

  it('keeps one coherent foreground through page, tile click, and browser history', async () => {
    const router = createMemoryRouter([{ path: '*', element: <NavigationHarness /> }], {
      initialEntries: ['/kanban?board=shipping']
    })

    render(<RouterProvider router={router} />)

    const kanbanButton = await screen.findByRole('button', { name: 'Kanban' })
    const tileLayoutBefore = JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/kanban?board=shipping')
      expect(screen.getByTestId('view').textContent).toBe('extension')
      expect(screen.getByTestId('foreground').textContent).toBe('kanban')
      expect(kanbanButton.className).toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('')
    })

    fireEvent.click(screen.getByRole('button', { name: 'Open session s1' }))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/s1')
      expect(screen.getByTestId('view').textContent).toBe('chat')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s1')
      expect(kanbanButton.className).not.toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s1')
      expect(JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')).toEqual({
        ...tileLayoutBefore,
        active: 'session-tile:s1'
      })
    })
    expect(resumeSession).not.toHaveBeenCalled()

    // The original query-bearing Kanban entry and the preserved session entry
    // both remain truthful when replayed by browser history.
    await act(async () => {
      await router.navigate(-1)
    })

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/kanban?board=shipping')
      expect(screen.getByTestId('foreground').textContent).toBe('kanban')
      expect(kanbanButton.className).toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('')
    })

    await act(async () => {
      await router.navigate(1)
    })

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/s1')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s1')
      expect(kanbanButton.className).not.toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s1')
      expect(JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')).toEqual({
        ...tileLayoutBefore,
        active: 'session-tile:s1'
      })
    })
    expect(resumeSession).not.toHaveBeenCalled()

    fireEvent.click(kanbanButton)

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/kanban')
      expect(screen.getByTestId('view').textContent).toBe('extension')
      expect(screen.getByTestId('foreground').textContent).toBe('kanban')
      expect(kanbanButton.className).toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('')
    })

    await act(async () => {
      await router.navigate(-1)
    })

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/s1')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s1')
      expect(kanbanButton.className).not.toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s1')
      expect(JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')).toEqual({
        ...tileLayoutBefore,
        active: 'session-tile:s1'
      })
    })
    expect(resumeSession).not.toHaveBeenCalled()

    await act(async () => {
      await router.navigate(1)
    })

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/kanban')
      expect(screen.getByTestId('foreground').textContent).toBe('kanban')
      expect(kanbanButton.className).toContain('bg-(--ui-control-active-background)')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('')
    })
    expect($sessionTiles.get()).toEqual([
      expect.objectContaining({
        anchor: 'workspace',
        before: null,
        dir: 'center',
        storedSessionId: 's1'
      }),
      expect.objectContaining({
        anchor: 'workspace',
        before: null,
        dir: 'center',
        storedSessionId: 's2'
      })
    ])
  })

  it('keeps a later sidebar selection authoritative across background updates', async () => {
    const router = createMemoryRouter([{ path: '*', element: <NavigationHarness /> }], {
      initialEntries: ['/kanban?board=shipping']
    })

    render(<RouterProvider router={router} />)

    await waitFor(() => expect(screen.getByTestId('foreground').textContent).toBe('kanban'))

    fireEvent.click(screen.getByRole('button', { name: 'Open session s1' }))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/s1')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s1')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s1')
    })

    const tileLayoutBefore = JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')

    // Match the production sidebar callback: publish the resume request first,
    // then focus/open the clicked session. Because s2 is already tiled,
    // openSession keeps the existing /s1 history entry and only changes focus.
    fireEvent.click(screen.getByRole('button', { name: 'Open session s2' }))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/s1')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s2')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s2')
      expect(JSON.parse(screen.getByTestId('tile-layout').textContent ?? '{}')).toEqual({
        ...tileLayoutBefore,
        active: 'session-tile:s2'
      })
    })

    // A background gateway transition re-runs route resume but is not a route
    // visit, so it must not reassert the stale s1 history target.
    fireEvent.click(screen.getByRole('button', { name: 'Close gateway' }))

    await waitFor(() => {
      expect(screen.getByTestId('gateway-state').textContent).toBe('closed')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s2')
    })

    fireEvent.click(screen.getByRole('button', { name: 'Open gateway' }))

    await waitFor(() => {
      expect(screen.getByTestId('gateway-state').textContent).toBe('open')
      expect(screen.getByTestId('foreground').textContent).toBe('chat:s2')
      expect(screen.getByTestId('sidebar-active-session').textContent).toBe('s2')
    })
    expect(resumeSession).not.toHaveBeenCalled()
    expect($sessionTiles.get()).toEqual([
      expect.objectContaining({ storedSessionId: 's1' }),
      expect.objectContaining({ storedSessionId: 's2' })
    ])
  })
})
