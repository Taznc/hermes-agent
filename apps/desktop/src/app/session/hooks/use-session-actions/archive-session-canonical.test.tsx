// Regression coverage for the ONE canonical archiveSession/unarchiveSession
// pair: mutation fencing, unread cleanup, tile/runtime cleanup, notification
// behavior, and rollback on failure. The Archive-Undo layer that used to sit
// on top of this action has been removed (t_77c22e64) — archive is a plain
// optimistic mutation, reversible only through the Archived view's Unarchive.
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patchArchived = vi.fn<(id: string, archived: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  getSession: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: (id: string, archived: boolean, profile?: null | string) => patchArchived(id, archived, profile)
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const forgetSessionUnreadSpy = vi.fn()

vi.mock('@/store/session-unread', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  forgetSessionUnread: (...args: unknown[]) => forgetSessionUnreadSpy(...args)
}))

const closeSessionTileSpy = vi.fn()
const dropSessionStateSpy = vi.fn()

vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  closeSessionTile: (...args: unknown[]) => closeSessionTileSpy(...args),
  dropSessionState: (...args: unknown[]) => dropSessionStateSpy(...args)
}))

import { $pinnedSessionIds } from '@/store/layout'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $selectedStoredSessionId, $sessions, setSessions } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { $archivedSessions } from '@/store/sidebar-archive'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from './index'

function archivableSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'live-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'live session',
    tool_call_count: 0,
    ...overrides
  } as SessionInfo
}

type Handle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession' | 'unarchiveSession'>

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
    onReady({ archiveSession: actions.archiveSession, unarchiveSession: actions.unarchiveSession })
  }, [actions, onReady])

  return null
}

async function mountHarness(): Promise<Handle> {
  let handle: Handle | undefined
  render(<Harness onReady={h => (handle = h)} />)
  await waitFor(() => expect(handle).toBeDefined())

  return handle as Handle
}

describe('the canonical archiveSession/unarchiveSession pair', () => {
  beforeEach(() => {
    setSessions([])
    $archivedSessions.set([])
    $pinnedSessionIds.set([])
    $removedSessionIds.set(new Set())
    $selectedStoredSessionId.set(null)
    clearNotifications()
    patchArchived.mockReset()
    patchArchived.mockResolvedValue({ ok: true })
    forgetSessionUnreadSpy.mockReset()
    closeSessionTileSpy.mockReset()
    dropSessionStateSpy.mockReset()
  })

  afterEach(() => {
    cleanup()
    setSessions([])
    $archivedSessions.set([])
    $pinnedSessionIds.set([])
    $selectedStoredSessionId.set(null)
    clearNotifications()
  })

  it('fences the mutation and cleans up unread + tile state on archive', async () => {
    setSessions([archivableSession()])

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1'))

    expect(patchArchived).toHaveBeenCalledWith('live-1', true, undefined)
    expect(forgetSessionUnreadSpy).toHaveBeenCalledTimes(1)
    expect(closeSessionTileSpy).toHaveBeenCalledWith('live-1')
    // Row is optimistically gone from the active list.
    expect($sessions.get().map(s => s.id)).toEqual([])
  })

  it('shows the ambient "Archived" toast on a successful archive', async () => {
    setSessions([archivableSession()])

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1'))

    expect($notifications.get().length).toBe(1)
  })

  it('rolls an archive failure back to the sidebar and surfaces the error', async () => {
    setSessions([archivableSession()])
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1'))

    // Rolled back to the sidebar — the canonical action never rethrows on the
    // plain (non-caller-owned) path, it surfaces its own failure toast.
    expect($sessions.get().map(s => s.id)).toEqual(['live-1'])
    expect($notifications.get().length).toBe(1)
  })

  it('unarchives an archived row and surfaces it in the live sidebar', async () => {
    $archivedSessions.set([archivableSession({ archived: true, profile: 'reviewer' })])

    const handle = await mountHarness()

    await act(() => handle.unarchiveSession('live-1'))

    expect(patchArchived).toHaveBeenCalledWith('live-1', false, 'reviewer')
    expect($archivedSessions.get()).toEqual([])
    expect($sessions.get()).toEqual([expect.objectContaining({ archived: false, id: 'live-1' })])
  })

  it('rolls an unarchive failure back to the archived list', async () => {
    $archivedSessions.set([archivableSession({ archived: true, profile: 'reviewer' })])
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    const handle = await mountHarness()

    await act(() => handle.unarchiveSession('live-1'))

    expect($sessions.get()).toEqual([])
    expect($archivedSessions.get()).toEqual([expect.objectContaining({ archived: true, id: 'live-1' })])
  })
})
