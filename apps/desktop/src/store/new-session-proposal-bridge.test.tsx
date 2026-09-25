import { useStore } from '@nanostores/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { type MutableRefObject, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearSingleFlightSessionResumeState } from '@/app/session/hooks/use-prompt-actions/single-flight-resume'
import { useSubmitPrompt } from '@/app/session/hooks/use-prompt-actions/submit'
import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import { useSessionStateCache } from '@/app/session/hooks/use-session-state-cache'
import { useI18n } from '@/i18n'
import {
  $activeSessionId,
  $messages,
  $selectedStoredSessionId,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId
} from '@/store/session'

import { makeStartNewSessionFromTopic } from './new-session-proposal'

/**
 * Integration-level proof for the `/new-topic` / `propose_new_session`
 * Approve bridge (t_2023fb69 recovery, reviewer comment 1341 item 2):
 * `new-session-proposal-tool.test.tsx` only asserts a MOCKED
 * `$startNewSessionFromTopic` was called with the right topic — it never
 * proves the real pipeline. This test wires the REAL `useSessionActions`
 * (startFreshSessionDraft) and `useSubmitPrompt` (submitText) hooks — the
 * exact pair `ContribWiring` feeds `makeStartNewSessionFromTopic` — and
 * drives `makeStartNewSessionFromTopic`'s own output, proving: a clean
 * session is created (session.create, no inherited history), selected
 * ($activeSessionId / $selectedStoredSessionId flip to the new ids), and the
 * topic becomes the session's first REAL submitted turn (prompt.submit).
 */

const RUNTIME_ID = 'rt-new-topic-1'
const STORED_ID = 'stored-new-topic-1'

interface HarnessHandle {
  startNewSessionFromTopic: (topic: string) => Promise<boolean>
}

function Harness({
  onReady,
  requestGateway
}: {
  onReady: (h: HarnessHandle) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const busyRef: MutableRefObject<boolean> = useRef(false)
  const creatingSessionRef: MutableRefObject<boolean> = useRef(false)

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse,
    setBusy,
    setMessages
  })

  const sessionActions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: cache.activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: cache.ensureSessionState,
    getRouteToken: () => '/::',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: cache.resetViewSync,
    runtimeIdByStoredSessionIdRef: cache.runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: cache.sessionStateByRuntimeIdRef,
    syncSessionStateToView: cache.syncSessionStateToView,
    updateSessionState: cache.updateSessionState
  })

  const submitText = useSubmitPrompt({
    activeSessionIdRef: cache.activeSessionIdRef,
    busyRef,
    copy: useI18n().t.desktop,
    createBackendSessionForSend: sessionActions.createBackendSessionForSend,
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: cache.getRuntimeIdForStoredSession,
    getRouteToken: () => '/::',
    requestGateway,
    resumeStoredSession: sessionActions.resumeSession,
    runtimeIdByStoredSessionIdRef: cache.runtimeIdByStoredSessionIdRef,
    selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
    syncAttachmentsForSubmit: async (sessionId, attachments) => ({ sessionId, attachments }),
    updateSessionState: cache.updateSessionState
  })

  const startNewSessionFromTopic = makeStartNewSessionFromTopic({
    startFreshSessionDraft: sessionActions.startFreshSessionDraft,
    submitText: (...args) => act(async () => submitText(...args)) as Promise<boolean>
  })

  onReady({ startNewSessionFromTopic })

  return null
}

describe('makeStartNewSessionFromTopic — real create/publish/navigate/first-submit pipeline', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    clearSingleFlightSessionResumeState()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setBusy(false)
    setAwaitingResponse(false)
  })

  it('creates a clean session, publishes/selects it, and submits the topic as its first real turn', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.create') {
        return { session_id: RUNTIME_ID, stored_session_id: STORED_ID } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await act(async () => {
      render(<Harness onReady={h => (handle = h)} requestGateway={requestGateway} />)
    })
    await waitFor(() => expect(handle).not.toBeNull())

    const ok = await handle!.startNewSessionFromTopic('refactor the auth module')

    expect(ok).toBe(true)

    // A clean session: session.create carries no inherited messages array —
    // never the raw session.create-with-messages shortcut round 1 rejected.
    const createCall = calls.find(c => c.method === 'session.create')
    expect(createCall).toBeTruthy()
    expect(createCall!.params).not.toHaveProperty('messages')

    // Selected/navigated: the runtime and stored ids the create minted are
    // now the live selection, not just handed back to the caller.
    await waitFor(() => expect($activeSessionId.get()).toBe(RUNTIME_ID))
    expect($selectedStoredSessionId.get()).toBe(STORED_ID)

    // The topic became the session's first REAL submitted turn — a genuine
    // prompt.submit RPC, not a synthesized transcript row.
    expect(calls).toContainEqual({
      method: 'prompt.submit',
      params: expect.objectContaining({ session_id: RUNTIME_ID, text: 'refactor the auth module' })
    })

    // The optimistic bubble for the seeded topic actually landed in the
    // visible transcript — this is the "type it and hit Enter" user-visible
    // proof, not just the wire call.
    expect(
      $messages
        .get()
        .some(m => m.role === 'user' && m.parts.some(p => p.type === 'text' && p.text === 'refactor the auth module'))
    ).toBe(true)
  })

  it('resolves false without creating a session when the pipeline rejects the send (e.g. empty gateway failure)', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        throw new Error('backend unavailable')
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await act(async () => {
      render(<Harness onReady={h => (handle = h)} requestGateway={requestGateway} />)
    })
    await waitFor(() => expect(handle).not.toBeNull())

    const ok = await handle!.startNewSessionFromTopic('will fail to create')

    expect(ok).toBe(false)
    expect($activeSessionId.get()).toBeNull()
  })
})
