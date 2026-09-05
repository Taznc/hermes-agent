// Regression for review t_548d0d33, blocking issue 2: the sidebar's
// undo-capable archive must route through the ONE canonical `archiveSession`
// action (mutation fencing, unread cleanup, tile/runtime cleanup) instead of
// a second, forked implementation. This exercises the real hook with
// `{ withUndo: true }` and asserts every canonical side effect still fires,
// plus that a 10s undo window opens for it — the two halves of the fix.
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patchArchived = vi.fn<(id: string, archived: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(
  () => Promise.resolve({ ok: true })
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
import { $removedSessionIds } from '@/store/projects'
import { $selectedStoredSessionId, $sessions, setSessions } from '@/store/session'
import { isArchiveUndoPending, resetArchiveUndos } from '@/store/session-archive-undo'

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

describe('archiveSession({ withUndo: true }) reuses the canonical archive path', () => {
  beforeEach(() => {
    setSessions([])
    $pinnedSessionIds.set([])
    $removedSessionIds.set(new Set())
    $selectedStoredSessionId.set(null)
    resetArchiveUndos()
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
    $pinnedSessionIds.set([])
    $selectedStoredSessionId.set(null)
    resetArchiveUndos()
    clearNotifications()
  })

  it('fences the mutation, cleans up unread + tile state, and opens an undo window', async () => {
    setSessions([archivableSession()])

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1', { withUndo: true }))

    expect(patchArchived).toHaveBeenCalledWith('live-1', true, undefined)
    // Same cleanup the plain (non-undo) archive path performs — this is the
    // whole point of routing through one action instead of a fork.
    expect(forgetSessionUnreadSpy).toHaveBeenCalledTimes(1)
    expect(closeSessionTileSpy).toHaveBeenCalledWith('live-1')
    // The undo-capable caller's own window is now live.
    expect(isArchiveUndoPending('live-1')).toBe(true)
    // Row is optimistically gone from the active list.
    expect($sessions.get().map(s => s.id)).toEqual([])
  })

  it('does not show the ambient "Archived" toast for the undo-capable path (the caller shows its own)', async () => {
    setSessions([archivableSession()])

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1', { withUndo: true }))

    expect($notifications.get()).toEqual([])
  })

  it('shows the ambient "Archived" toast and does NOT open an undo window for a plain archive', async () => {
    setSessions([archivableSession()])

    const handle = await mountHarness()

    await act(() => handle.archiveSession('live-1'))

    expect($notifications.get().length).toBe(1)
    expect(isArchiveUndoPending('live-1')).toBe(false)
  })

  it('rejects (does not swallow) an undo-capable archive failure and discards the pending entry', async () => {
    setSessions([archivableSession()])
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    const handle = await mountHarness()

    await expect(act(() => handle.archiveSession('live-1', { withUndo: true }))).rejects.toThrow('network down')

    expect(isArchiveUndoPending('live-1')).toBe(false)
    // Rolled back to the sidebar.
    expect($sessions.get().map(s => s.id)).toEqual(['live-1'])
  })
})
