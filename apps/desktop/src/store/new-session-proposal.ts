import { atom, computed } from 'nanostores'

import { $gateway } from './gateway'

/**
 * Bridge into the canonical session-creation pipeline (`useSessionActions` /
 * `usePromptActions`, both mounted only inside `ContribWiring`). The inline
 * proposal card and the `/new-topic` manual trigger both live outside that
 * tree, so they can't call `startFreshSessionDraft`/`submitText` directly —
 * `ContribWiring` publishes this one function here (mirrors the
 * `$restartPreviewServer` pattern used for the same core/contrib boundary).
 *
 * Starts a brand-new, clean session (no inherited profile/model/history) and
 * submits `topic` as its real first turn through the normal create →
 * publish → prompt.submit pipeline — never a raw `session.create` call.
 * Resolves to whether the submit succeeded.
 */
export type StartNewSessionFromTopic = (topic: string) => Promise<boolean>
export const $startNewSessionFromTopic = atom<StartNewSessionFromTopic | null>(null)

/**
 * Pending `session.propose.request`s — the desktop half of the
 * `propose_new_session` tool's blocking bridge
 * (tools/propose_new_session_tool.py). Mirrors the clarify/mcp-setup stores:
 * keyed by the runtime session id that raised the request so a background
 * session can park its card while the user looks at another chat, and the
 * inline card reads its own session's entry.
 */
export interface NewSessionProposalRequest {
  requestId: string
  /** The seeded first message for the new session, written by the agent. */
  topic: string
  /** Agent-supplied one-liner: why splitting off now helps. */
  reason: string
  sessionId: string | null
}

/** The card's answer, serialized back through `session.propose.respond`. */
export interface NewSessionProposalOutcome {
  status: 'approved' | 'declined' | 'error'
  /** The seeded topic — echoed back so the settled card can show it. */
  topic?: string
  /** The freshly created session's runtime id (approved only). */
  session_id?: string
  detail?: string
}

const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $newSessionProposalRequests = atom<Record<string, NewSessionProposalRequest>>({})

/** The proposal for one specific session — the transcript card reads this
 *  fixed-key view, same shape as `sessionMcpSetupRequest`. */
export const sessionNewSessionProposalRequest = (sessionId: string | null) =>
  computed($newSessionProposalRequests, requests => requests[keyFor(sessionId)] ?? null)

export function setNewSessionProposalRequest(request: NewSessionProposalRequest): void {
  $newSessionProposalRequests.set({ ...$newSessionProposalRequests.get(), [keyFor(request.sessionId)]: request })
}

export function clearNewSessionProposalRequest(requestId?: string, sessionId?: string | null): void {
  const requests = $newSessionProposalRequests.get()

  if (sessionId !== undefined) {
    const key = keyFor(sessionId)
    const current = requests[key]

    if (!current || (requestId && current.requestId !== requestId)) {
      return
    }

    const next = { ...requests }
    delete next[key]
    $newSessionProposalRequests.set(next)

    return
  }

  const next: Record<string, NewSessionProposalRequest> = {}
  let changed = false

  for (const [key, value] of Object.entries(requests)) {
    if (requestId && value.requestId !== requestId) {
      next[key] = value
    } else {
      changed = true
    }
  }

  if (changed) {
    $newSessionProposalRequests.set(next)
  }
}

/** Whether `sessionId` has a new-session card pending right now (imperative
 *  read — the composer checks this on Enter, not on every render). */
export const hasNewSessionProposalRequest = (sessionId: string | null | undefined): boolean =>
  Boolean($newSessionProposalRequests.get()[keyFor(sessionId)])

/**
 * Answer `sessionId`'s pending proposal card as declined and drop it locally,
 * resolving to whether there was one to skip.
 *
 * Mirrors skipMcpSetupRequest/skipClarifyRequest: typing a real message is
 * itself the answer "not now" — decline so the tool returns, then route the
 * words normally. `session.propose.respond` is allow_expired, so racing the
 * timeout is harmless.
 */
export async function skipNewSessionProposalRequest(sessionId: string | null | undefined): Promise<boolean> {
  const request = $newSessionProposalRequests.get()[keyFor(sessionId)]

  if (!request) {
    return false
  }

  clearNewSessionProposalRequest(request.requestId, request.sessionId)

  try {
    await $gateway.get()?.request('session.propose.respond', {
      request_id: request.requestId,
      result: JSON.stringify({ status: 'declined' })
    })
  } catch {
    // The tool times out on its own; a failed skip must never swallow the
    // message the user is actually sending.
  }

  return true
}
