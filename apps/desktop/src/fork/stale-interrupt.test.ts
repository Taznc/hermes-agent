import { describe, expect, it, vi } from 'vitest'

import { handleSessionInfoEvent } from '@/app/session/hooks/use-message-stream/gateway-event/session-info'
import type { GatewayEventContext } from '@/app/session/hooks/use-message-stream/gateway-event/types'
import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'

/**
 * Stop latches `interrupted` on THIS window's copy of the session, and only this window's own
 * submit cleared it. With a second window attached (another tab, the session tile), the next turn
 * was sent from there, so the stopping window stayed latched forever and answered every blocking
 * request of the live session on the spot: `clarify` with `{}` (→ outcome "cancelled" in 0.07s),
 * `plugin.request` with an error (→ the ask card never showed). The first response wins, so it beat
 * the window actually showing the chat. The latch must end when the backend reports the stopped
 * turn finished (session.info running=false).
 */
function infoEvent(sid: string, map: Map<string, ClientSessionState>, running: boolean): GatewayEventContext {
  const updateSessionState = vi.fn((id: string, updater: (s: ClientSessionState) => ClientSessionState) => {
    const next = updater(map.get(id) ?? createClientSessionState())
    map.set(id, next)

    return next
  })

  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: sid },
      hydrateFromStoredSession: vi.fn(),
      lastCwdInfoSessionRef: { current: null },
      queryClient: { invalidateQueries: vi.fn() },
      refreshHermesConfig: vi.fn(),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: (id: string) => map.get(id)?.interrupted ?? false,
      sessionStateByRuntimeIdRef: { current: map },
      updateSessionState,
      upsertToolCall: vi.fn()
    },
    event: { profile: 'default', session_id: sid, type: 'session.info' },
    explicitSid: sid,
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: Date.now() / 1000,
    payload: { running },
    scheduleConfigRefresh: vi.fn(),
    sessionId: sid
  } as unknown as GatewayEventContext
}

const stopped = (): ClientSessionState => ({
  ...createClientSessionState(),
  awaitingResponse: false,
  busy: false,
  interrupted: true
})

describe('stale interrupt latch', () => {
  it('a stopped window releases its latch once the backend reports the turn finished', () => {
    const map = new Map([['rt1', stopped()]])

    handleSessionInfoEvent(infoEvent('rt1', map, false))

    expect(map.get('rt1')?.interrupted).toBe(false)
  })

  it('keeps the latch while the stopped turn is still winding down (running=true)', () => {
    const map = new Map([['rt1', stopped()]])

    handleSessionInfoEvent(infoEvent('rt1', map, true))

    expect(map.get('rt1')?.interrupted).toBe(true)
    expect(map.get('rt1')?.busy).toBe(false)
  })
})
