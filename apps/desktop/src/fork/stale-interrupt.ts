import type { ClientSessionState } from '@/app/types'

type UpdateSessionState = (
  sessionId: string,
  updater: (state: ClientSessionState) => ClientSessionState,
  storedSessionId?: string
) => unknown

/**
 * Release a window's Stop latch once the backend reports the stopped turn finished
 * (`session.info` running=false; anchor `stale-interrupt-release` in session-info.ts).
 *
 * Stop sets `interrupted` on THIS window's copy of the session so the stopped turn's late events and
 * blocking requests are dropped. Upstream clears it only on this window's own submit, so when the next
 * turn is sent from another attached window (a second tab, the session tile) the latch never ends and
 * this window keeps answering the live session's `clarify` / `plugin.request` at once (cancelled / no
 * card), beating the window that shows the chat. Events of the stopped turn precede its running=false
 * on the same socket, so nothing of that turn can arrive after the release.
 */
export function releaseStoppedTurnLatch(sessionId: string, updateSessionState: UpdateSessionState): void {
  updateSessionState(sessionId, state => (state.interrupted && !state.busy ? { ...state, interrupted: false } : state))
}
