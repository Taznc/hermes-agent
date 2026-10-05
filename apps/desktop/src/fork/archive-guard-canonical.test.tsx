// Regression: deleting a session from the Archived filter view left a ghost
// row. Archived rows live in $archivedSessions (their own capped store —
// they're excluded from $sessions by design), and removeSession only pruned
// $sessions. The ghost row then resumed into a hard-deleted id: resume 404 →
// goneSessionVerdict saw the row still listed → 'retry' → an unrecoverable
// spinner (Aug 2026 desktop audit, F8).
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import type { ClientSessionState } from '@/app/types'
import { type SessionInfo, setSessionArchived } from '@/hermes'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { $backgroundStatusBySession } from '@/store/composer-status'
import { $sessions, setSessions } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { $sessionStates, $sessionTiles } from '@/store/session-states'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

function archivedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: true,
    ended_at: null,
    id: 'arch-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'archived ghost',
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

describe('canonical archive active-work guard', () => {
  beforeEach(() => {
    setSessions([archivedSession({ archived: false, profile: 'default', is_active: true })])
    $sessionStates.set({})
    $queuedPromptsBySession.set({})
    $backgroundStatusBySession.set({})
    $subagentsBySession.set({})
    $removedSessionIds.set(new Set())
    $sessionTiles.set([{ storedSessionId: 'arch-1', runtimeId: 'rt', dir: 'right' }])
    vi.mocked(setSessionArchived).mockReset().mockResolvedValue({ ok: true })
  })
  afterEach(() => {
    cleanup()
    $sessionStates.set({})
    $sessionTiles.set([])
  })
  it.each(['pending-input', 'queued', 'descendant', 'process'] as const)(
    'refuses %s after the parent becomes idle',
    async kind => {
      $sessionStates.set({
        rt: {
          storedSessionId: 'arch-1',
          busy: false,
          needsInput: kind === 'pending-input',
          messages: []
        } as unknown as ClientSessionState
      })

      if (kind === 'queued') {
        $queuedPromptsBySession.set({ 'arch-1': [{ id: 'q', text: 'continue', attachments: [], queuedAt: 1 }] })
      }

      if (kind === 'descendant') {
        $subagentsBySession.set({ rt: [{ id: 'child', status: 'running' } as SubagentProgress] })
      }

      if (kind === 'process') {
        $backgroundStatusBySession.set({ rt: [{ state: 'running', id: 'proc', type: 'background', title: 'job' }] })
      }

      const handle = await mountHarness()
      await act(() => handle.archiveSession('arch-1'))
      expect(setSessionArchived).not.toHaveBeenCalled()
      expect($sessions.get()).toHaveLength(1)
      expect($removedSessionIds.get().size).toBe(0)
      expect($sessionTiles.get()).toHaveLength(1)
    }
  )
  it('blocks a pending approval even when a running heartbeat has already settled', async () => {
    const { setApprovalRequest, clearAllPrompts } = await import('@/store/prompts')
    $sessionStates.set({
      rt: { storedSessionId: 'arch-1', busy: false, needsInput: false, messages: [] } as unknown as ClientSessionState
    })
    setApprovalRequest({ sessionId: 'rt', command: 'job', description: 'approve', requestId: 'approval' })

    try {
      const handle = await mountHarness()
      await act(() => handle.archiveSession('arch-1'))
      expect(setSessionArchived).not.toHaveBeenCalled()
      expect($sessionTiles.get()).toHaveLength(1)
    } finally {
      clearAllPrompts()
    }
  })
  it('allows an idle OPEN runtime; tab presence and is_active are not work', async () => {
    $sessionStates.set({
      rt: { storedSessionId: 'arch-1', busy: false, needsInput: false, messages: [] } as unknown as ClientSessionState
    })
    const handle = await mountHarness()
    await act(() => handle.archiveSession('arch-1'))
    expect(setSessionArchived).toHaveBeenCalledWith('arch-1', true, 'default')
    expect($sessions.get()).toHaveLength(0)
    expect($removedSessionIds.get().has('arch-1')).toBe(true)
  })
  it('refuses a running turn before evicting the row or closing its tile', async () => {
    $sessionStates.set({ rt: { busy: true, storedSessionId: 'arch-1', messages: [] } as unknown as ClientSessionState })
    const handle = await mountHarness()
    await act(() => handle.archiveSession('arch-1'))
    expect(setSessionArchived).not.toHaveBeenCalled()
    expect($sessions.get().map(s => s.id)).toEqual(['arch-1'])
    expect($removedSessionIds.get().size).toBe(0)
    expect($sessionTiles.get()).toHaveLength(1)
  })
})
