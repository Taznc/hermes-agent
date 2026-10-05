import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import type { SessionInfo } from '@/hermes'
import type { SidebarListRow } from '@/lib/session-date-groups'
import { $sessions } from '@/store/session'
import { $removedSessionIds, tombstoneSessions, untombstoneSessions } from '@/store/session-removal'
import { type SessionTileDelegate, setSessionTileDelegate } from '@/store/session-states'
import { $archivedSessions } from '@/store/sidebar-archive'

import { forkHost } from './sdk-host'
import {
  $forkSidebarListRows,
  forkListDividerAction,
  publishForkSidebarListRows,
  sessionIdsUnderDivider,
  SIDEBAR_GROUP_ACTION_AREA,
  type SidebarGroupActionProps,
  SidebarProjectGroupAction
} from './sidebar-group-actions'

vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { sidebar: { dateDivider: { today: 'Today', yesterday: 'Yesterday' } } } })
}))
vi.mock('@/store/projects', async () => {
  const { atom } = await import('nanostores')

  return {
    $projectTree: atom([]),
    refreshProjectTree: vi.fn(() => Promise.resolve()),
    fetchProjectSessions: vi.fn(async () => null),
    projectProfile: () => 'default'
  }
})
vi.mock('@/store/sidebar-archive', async () => {
  const { atom } = await import('nanostores')

  return { $archivedSessions: atom([]), loadArchivedSessions: vi.fn(() => Promise.resolve()) }
})

const session = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo => ({ id, ...extra }) as SessionInfo
const sRow = (id: string): SidebarListRow => ({ entry: { session: session(id) }, kind: 'session' }) as SidebarListRow
const divider = (key: string): SidebarListRow => ({ key, kind: 'divider', label: key }) as SidebarListRow

afterEach(() => {
  cleanup()
  $forkSidebarListRows.set([])
  $sessions.set([])
  $removedSessionIds.set(new Set())
  $archivedSessions.set([])
})

describe('sessionIdsUnderDivider', () => {
  const yesterday = divider('yesterday')
  const lastWeek = divider('last-week')
  const rows = [sRow('head'), yesterday, sRow('a'), sRow('b'), lastWeek, sRow('c')]

  it('returns the sessions between a divider and the next one', () => {
    expect(sessionIdsUnderDivider(rows, yesterday)).toEqual(['a', 'b'])
    expect(sessionIdsUnderDivider(rows, lastWeek)).toEqual(['c'])
  })

  it('matches by identity: a same-key divider from another list (project lane) has no members', () => {
    expect(sessionIdsUnderDivider(rows, divider('yesterday'))).toEqual([])
  })
})

describe('no plugin registered', () => {
  it('with NO plugin registered, every anchor renders nothing (stock behaviour unchanged)', () => {
    const yesterday = divider('yesterday')
    act(() => void publishForkSidebarListRows([yesterday, sRow('a')]))

    const { container } = render(
      <>
        {forkListDividerAction(yesterday)}
        <SidebarProjectGroupAction project={{ id: 'p', label: 'P' }} />
      </>
    )

    expect(container.innerHTML).toBe('')
  })
})

describe('group action slot', () => {
  const seen: SidebarGroupActionProps[] = []
  let unregister: () => void

  beforeEach(() => {
    seen.length = 0
    unregister = registry.register({
      area: SIDEBAR_GROUP_ACTION_AREA,
      data: {
        render: (props: SidebarGroupActionProps) => {
          seen.push(props)

          return <span data-testid={`act-${props.kind}-${props.groupKey}`}>{props.sessionIds.join(',')}</span>
        }
      },
      id: 'probe',
      source: 'test'
    })
  })

  afterEach(() => unregister())

  it('renders nothing extra for a session row: core action passes through untouched', () => {
    const core = <span data-testid="core" />

    expect(forkListDividerAction(sRow('x'), core)).toBe(core)
  })

  it('divider action resolves members from published rows, INCLUDING a collapsed group', () => {
    const yesterday = divider('yesterday')
    // The full flat rows are published; the rendered list may have dropped
    // the sessions under a collapsed divider — the action still sees them.
    act(() => void publishForkSidebarListRows([sRow('head'), yesterday, sRow('a'), sRow('b')]))
    render(<>{forkListDividerAction(yesterday, <span data-testid="core" />)}</>)

    expect(screen.getByTestId('act-date-yesterday').textContent).toBe('a,b')
    expect(screen.getByTestId('core')).toBeTruthy()
  })

  it('status dividers report kind=status', () => {
    const working = divider('status:working')
    act(() => void publishForkSidebarListRows([working, sRow('w1')]))
    render(<>{forkListDividerAction(working)}</>)

    expect(screen.getByTestId('act-status-status:working').textContent).toBe('w1')
  })

  it('waits for COMPLETE hydrated members when the backend omits owner ids', async () => {
    const { fetchProjectSessions } = await import('@/store/projects')
    vi.mocked(fetchProjectSessions).mockResolvedValueOnce({
      id: 'p4',
      repos: [{ groups: [{ sessions: [session('a'), session('b'), session('c')] }] }]
    } as never)
    render(
      <SidebarProjectGroupAction project={{ id: 'p4', label: 'Full', previewSessions: [session('a')], repos: [] }} />
    )
    const { waitFor } = await import('@testing-library/react')
    await waitFor(() => expect(screen.getByTestId('act-project-p4').textContent).toBe('a,b,c'))
  })

  it('project action prefers the backend complete owner set over the preview', () => {
    render(
      <SidebarProjectGroupAction
        project={{
          id: 'p1',
          label: 'Proj',
          previewSessions: [session('a')],
          repos: [],
          sessionIds: ['a', 'b', 'c']
        }}
      />
    )

    expect(screen.getByTestId('act-project-p1').textContent).toBe('a,b,c')
  })

  it('project action refuses a partial preview when full hydration is unavailable', () => {
    render(
      <SidebarProjectGroupAction
        project={{
          id: 'p2',
          label: 'Old',
          previewSessions: [session('a')],
          repos: [{ groups: [{ sessions: [session('a'), session('b')] }] }]
        }}
      />
    )

    expect(screen.queryByTestId('act-project-p2')).toBeNull()
  })

  it('project action tolerates a partial payload (no repos) without crashing the row', () => {
    render(<SidebarProjectGroupAction project={{ id: 'p3', label: 'Bare' }} />)

    expect(screen.queryByTestId('act-project-p3')).toBeNull()
  })
})

describe('host.fork.sessions.archive', () => {
  const archiveSession = vi.fn<(id: string) => Promise<void>>()

  beforeEach(() => {
    archiveSession.mockReset()
    setSessionTileDelegate({ archiveSession } as unknown as SessionTileDelegate)
  })

  it('is versioned and namespaced under host.fork', () => {
    expect(forkHost.sessions.version).toBe(1)
    expect(forkHost.sidebar.GROUP_ACTION_AREA).toBe(SIDEBAR_GROUP_ACTION_AREA)
  })

  it('routes every id through the app archive verb, resolving durable ids to the live row id', async () => {
    $sessions.set([session('live-tip', { _lineage_root_id: 'root' })])
    // The real verb tombstones on success.
    archiveSession.mockImplementation(async id => tombstoneSessions([id]))

    const result = await forkHost.sessions.archive(['root', 'other', 'other', ' '])

    expect(archiveSession.mock.calls.map(([id]) => id).sort()).toEqual(['live-tip', 'other'])
    expect(result.archived.sort()).toEqual(['live-tip', 'other'])
    expect(result.failed).toEqual([])
  })

  it('reports a rolled-back archive (verb untombstones + toasts, never throws) as failed', async () => {
    archiveSession.mockImplementation(async id => {
      tombstoneSessions([id])
      untombstoneSessions([id])
    })

    const result = await forkHost.sessions.archive(['x'])

    expect(result).toEqual({ archived: [], failed: [{ error: 'archive rolled back', id: 'x' }] })
  })

  it('reports a thrown archive as failed and keeps going', async () => {
    archiveSession.mockImplementation(async id => {
      if (id === 'bad') {
        throw new Error('boom')
      }

      tombstoneSessions([id])
    })

    const result = await forkHost.sessions.archive(['ok1', 'bad', 'ok2'])

    expect(result.archived.sort()).toEqual(['ok1', 'ok2'])
    expect(result.failed).toEqual([{ error: 'boom', id: 'bad' }])
  })

  it('skips active members, retaining an accurate mixed-group result', async () => {
    const { $sessionStates } = await import('@/store/session-states')
    $sessionStates.set({ rt: { storedSessionId: 'busy', busy: true, messages: [] } } as never)
    archiveSession.mockImplementation(async id => tombstoneSessions([id]))

    try {
      const result = await forkHost.sessions.archive(['busy', 'idle'])
      expect(archiveSession.mock.calls.map(([id]) => id)).toEqual(['idle'])
      expect(result).toEqual({ archived: ['idle'], failed: [], skipped: [{ id: 'busy', reason: 'running' }] })
      expect(forkHost.sessions.canArchive('busy')).toBe(false)
      expect(forkHost.sessions.archiveBlockers.get().busy).toBe('running')
    } finally {
      $sessionStates.set({})
    }
  })

  it('reports work that starts during canonical owner resolution as skipped', async () => {
    const { $sessionStates } = await import('@/store/session-states')
    archiveSession.mockImplementation(async id => {
      $sessionStates.set({ rt: { storedSessionId: id, busy: true, messages: [] } } as never)
    })

    try {
      expect(await forkHost.sessions.archive(['race'])).toEqual({
        archived: [],
        failed: [],
        skipped: [{ id: 'race', reason: 'running' }]
      })
    } finally {
      $sessionStates.set({})
    }
  })

  it('projects-only compressed members use lineage blockers and dedupe aliases', async () => {
    const { $projectTree } = await import('@/store/projects')
    const { $sessionStates } = await import('@/store/session-states')
    $projectTree.set([
      { id: 'project', repos: [], previewSessions: [{ ...session('tip'), _lineage_root_id: 'root', profile: 'other' }] }
    ] as never)
    $sessionStates.set({ rt: { storedSessionId: 'root', busy: true, messages: [] } } as never)

    try {
      expect(forkHost.sessions.canArchive('tip')).toBe(false)
      $sessionStates.set({})
      archiveSession.mockImplementation(async id => tombstoneSessions([id]))
      const result = await forkHost.sessions.archive(['root', 'tip'])
      expect(archiveSession.mock.calls.map(([id]) => id)).toEqual(['tip'])
      expect(result.archived).toEqual(['tip'])
    } finally {
      $sessionStates.set({})
      $projectTree.set([])
    }
  })

  it('reports a backend-discovered active member as skipped, not rolled back', async () => {
    const gateway = await import('@/store/gateway')

    const probe = vi
      .spyOn(gateway, 'requestGatewayForProfile')
      .mockResolvedValue({ archivable: false, blockers: ['process'], session_key: 'remote' })

    const { guardForkSessionArchive } = await import('./archive-guard')
    archiveSession.mockImplementation(async id => {
      await guardForkSessionArchive(id, 'default')
    })

    try {
      expect(await forkHost.sessions.archive(['remote'])).toEqual({
        archived: [],
        failed: [],
        skipped: [{ id: 'remote', reason: 'backend-work' }]
      })
    } finally {
      probe.mockRestore()
    }
  })
  it('caps concurrency at 4', async () => {
    let inFlight = 0
    let peak = 0

    archiveSession.mockImplementation(async id => {
      inFlight++
      peak = Math.max(peak, inFlight)
      await new Promise(resolve => setTimeout(resolve, 1))
      inFlight--
      tombstoneSessions([id])
    })

    await forkHost.sessions.archive(Array.from({ length: 20 }, (_, i) => `s${i}`))

    expect(peak).toBe(4)
    expect(archiveSession).toHaveBeenCalledTimes(20)
  })

  it('archivedIds covers the Archived view, archived-flagged cache rows (with lineage), and tombstones', () => {
    $archivedSessions.set([session('arch')])
    $sessions.set([
      session('tip', { _lineage_ids: ['mid'], _lineage_root_id: 'root', archived: true }),
      session('live')
    ])
    tombstoneSessions(['gone'])

    const ids = forkHost.sessions.archivedIds.get()

    expect([...ids].sort()).toEqual(['arch', 'gone', 'mid', 'root', 'tip'])
    expect(ids.has('live')).toBe(false)
  })
})
