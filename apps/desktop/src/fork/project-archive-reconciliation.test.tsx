import { useStore } from '@nanostores/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { type MutableRefObject, useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as Model from '@/app/chat/sidebar/projects/model'
import { ProjectOverviewRow } from '@/app/chat/sidebar/projects/overview-row'
import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import type { ClientSessionState } from '@/app/types'
import type { SessionInfo } from '@/hermes'
import { setSessionArchived } from '@/hermes'
import { $activeGatewayProfile, setShowAllProfiles } from '@/store/profile'
import { $projectTree, refreshProjectTree } from '@/store/projects'
import { $sessions } from '@/store/session'
import { $removedSessionIds, tombstoneSessions, untombstoneSessions } from '@/store/session-removal'

afterEach(cleanup)

const workspaceOpen = vi.hoisted(() => ({ value: false }))

// Keep the pure helpers real (they are the logic under test); stub only the
// persisted open/collapse hook and the in-memory fallback preview.
vi.mock('@/app/chat/sidebar/projects/model', async () => ({
  ...(await vi.importActual<typeof Model>('@/app/chat/sidebar/projects/model')),
  useWorkspaceNodeOpen: () => [workspaceOpen.value, vi.fn()]
}))

// ProjectMenu (the kebab) has its own dedicated test file — stub it here so
// this file only exercises overview-row's own Tip usage (the disclosure
// toggle) plus the WorkspaceAddButton wiring. ProjectContextMenu (the row's
// right-click wrapper) is stubbed as a pass-through so the row still renders.
vi.mock('@/app/chat/sidebar/projects/project-menu', () => ({
  ProjectContextMenu: ({ children }: { children: ReactNode }) => children,
  ProjectMenu: () => null
}))

const project = { id: 'p1', label: 'Test D' } as unknown as SidebarProjectTree

const session = (id: string, updated: number): SessionInfo => ({ id, updated_at: updated }) as unknown as SessionInfo

const gateway = vi.hoisted(() => ({ request: vi.fn(), connectionState: 'open' }))
vi.mock('@/store/gateway', async () => {
  const { atom } = await import('nanostores')

  return {
    $gateway: atom(null),
    activeGateway: () => gateway,
    activeGatewayConnectionId: () => null,
    isActivePrimary: () => true,
    ensureActiveGatewayOpen: async () => gateway,
    requestGatewayForProfile: vi.fn().mockResolvedValue({ archivable: true, blockers: [], session_key: 's1' })
  }
})
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  setSessionArchived: vi.fn().mockResolvedValue({ ok: true })
}))

type Handle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession'>

function Harness({ onReady }: { onReady: (handle: Handle) => void }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    routedSessionId: null,
    requestGateway: vi.fn().mockResolvedValue(undefined),
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady({ archiveSession: actions.archiveSession })
  }, [actions, onReady])

  return null
}

async function mountHarness(): Promise<Handle> {
  let handle: Handle | undefined
  render(<Harness onReady={h => (handle = h)} />)
  await waitFor(() => expect(handle).toBeDefined())

  return handle as Handle
}

const five = Array.from({ length: 5 }, (_, i) => ({ ...session(`s${i + 1}`, 100 - i), profile: 'default' }))

const tree = (rows = five): SidebarProjectTree =>
  ({
    ...project,
    path: '/p',
    repos: [
      {
        id: 'repo',
        path: '/p',
        label: 'repo',
        sessionCount: rows.length,
        groups: [{ id: 'lane', label: 'lane', sessions: rows }]
      }
    ],
    sessionCount: rows.length,
    sessionIds: rows.map(s => s.id),
    previewSessions: rows.slice(0, 3)
  }) as SidebarProjectTree

const renderRows = (items: SessionInfo[]) => <div data-testid="rows">{items.map(s => s.id).join(',')}</div>

function Overview({ preview }: { preview?: SessionInfo[] }) {
  const projects = useStore($projectTree)
  const sessions = useStore($sessions)
  const removed = useStore($removedSessionIds)

  return (
    <ProjectOverviewRow
      isSessionHidden={s => removed.has(s.id)}
      previewSessions={preview ?? sessions.slice(0, 3)}
      project={projects[0] ?? tree()}
      renderRows={renderRows}
    />
  )
}

describe('real project stores + React archive/refresh/show-all sequence', () => {
  beforeEach(() => {
    workspaceOpen.value = true
    $activeGatewayProfile.set('default')
    setShowAllProfiles(false)
    $removedSessionIds.set(new Set())
    $sessions.set(five)
    $projectTree.set([{ ...tree(), repos: [] }])
    gateway.request.mockReset().mockImplementation(async (method: string) =>
      method === 'projects.tree'
        ? {
            projects: [tree(five.filter(s => s.id !== 's1'))],
            active_id: null,
            scoped_session_ids: ['s2', 's3', 's4', 's5']
          }
        : { project: tree() }
    )
  })
  afterEach(() => {
    cleanup()
    untombstoneSessions(five.map(s => s.id))
    $removedSessionIds.set(new Set())
    $sessions.set([])
  })
  it('keeps an archived hydrated row excluded after authoritative refresh prunes its tombstone', async () => {
    render(<Overview />)
    fireEvent.click(screen.getByRole('button', { name: 'Show all 5 sessions' }))
    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s1,s2,s3,s4,s5'))
    act(() => {
      tombstoneSessions(['s1'])
      $sessions.set(five.slice(1))
    })
    expect(screen.getByTestId('rows').textContent).toBe('s2,s3,s4,s5')
    await act(() => refreshProjectTree())
    expect($removedSessionIds.get().has('s1')).toBe(false)
    expect(screen.getByTestId('rows').textContent).toBe('s2,s3,s4,s5')
  })
  it('restores an explicitly unarchived live row after exclusion outlives its tombstone', async () => {
    render(<Overview />)
    fireEvent.click(screen.getByRole('button', { name: 'Show all 5 sessions' }))
    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s1,s2,s3,s4,s5'))
    act(() => {
      tombstoneSessions(['s1'])
      $sessions.set(five.slice(1))
    })
    await act(() => refreshProjectTree())
    expect(screen.getByTestId('rows').textContent).not.toContain('s1')
    act(() => {
      untombstoneSessions(['s1'])
      $sessions.set(five.map(s => ({ ...s, archived: false })))
    })
    expect(screen.getByTestId('rows').textContent).toBe('s1,s2,s3,s4,s5')
  })
  it('rejects project hydration from a previous profile', async () => {
    render(<Overview />)
    let resolve!: (value: unknown) => void

    const response = new Promise(r => {
      resolve = r
    })

    gateway.request.mockImplementationOnce(() => response)
    fireEvent.click(screen.getByRole('button', { name: 'Show all 5 sessions' }))
    act(() => {
      $activeGatewayProfile.set('other')
      $sessions.set([])
      $projectTree.set([tree([])])
    })
    await act(async () => {
      resolve({ project: tree() })
      await response
    })
    expect(screen.queryByTestId('rows')?.textContent ?? '').toBe('')
  })
  it('counts the complete current membership, not loaded rows already evicted by archive', () => {
    act(() => {
      tombstoneSessions(['s1'])
      $sessions.set(five.slice(1))
    })
    render(<Overview />)
    expect(screen.getByRole('button', { name: 'Show all 4 sessions' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Show all 5 sessions' })).toBeNull()
  })
  it('canonical archive -> refresh -> show-all rejects the archived row in a stale response, but backfills older live rows', async () => {
    const handle = await mountHarness()
    await act(() => handle.archiveSession('s1'))
    expect(setSessionArchived).toHaveBeenCalledWith('s1', true, 'default')
    expect($sessions.get().map(s => s.id)).not.toContain('s1')
    render(<Overview />)
    await act(() => refreshProjectTree())
    fireEvent.click(screen.getByRole('button', { name: 'Show all 4 sessions' }))
    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s2,s3,s4,s5'))
    expect(screen.getByTestId('rows').textContent).not.toContain('s1')
  })
  it('a hydration in flight across archive and refresh cannot resurrect its stale member', async () => {
    const handle = await mountHarness()
    render(<Overview />)
    let resolve!: (value: unknown) => void

    const response = new Promise(r => {
      resolve = r
    })

    gateway.request.mockImplementationOnce(() => response)
    fireEvent.click(screen.getByRole('button', { name: 'Show all 5 sessions' }))
    await act(() => handle.archiveSession('s1'))
    await act(() => refreshProjectTree())
    await act(async () => {
      resolve({ project: tree() })
      await response
    })
    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s2,s3,s4,s5'))
  })
  it('an explicit empty overlaid preview cannot fall back to excluded raw lane rows', () => {
    render(<ProjectOverviewRow previewSessions={[]} project={tree()} renderRows={renderRows} />)
    expect(screen.queryByTestId('rows')?.textContent ?? '').toBe('')
  })
})
