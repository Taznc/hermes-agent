import type { ServerRequest } from '@hermes/shared'
import { atom } from 'nanostores'

import { onGatewayEvent } from '@/contrib/events'
import { registry } from '@/contrib/registry'
import { UI_REQUEST_AREA, type UiRequestContribution } from '@/fork/ui-bridge/types'

/** Server requests the renderer rejects with this code are "unanswered" to the backend:
 *  `server_requests.send` returns None and the plugin's `ui.request` returns None. */
const CANCELLED_CODE = -32000

/** One `plugin.request` waiting for the user, parked under its session (like clarify). */
export interface ParkedUiRequest {
  requestId: string
  sessionId: string
  kind: string
  params: unknown
  /** The tool call the card renders under. `undefined` = not resolved (a replay that
   *  landed before the transcript hydrated): the session's open tool row claims it. */
  toolCallId?: string
  receivedAt: number
}

/** Parked requests keyed by request id. Changes only on park/settle, never per delta. */
export const $uiRequests = atom<Readonly<Record<string, ParkedUiRequest>>>({})

/** Live channel handles, kept out of the atom (not serialisable, not render state). */
const handles = new Map<string, ServerRequest>()

export function parkUiRequest(request: ServerRequest, parked: ParkedUiRequest): void {
  // A reconnect replay (`open_requests`) reuses the id: replace, never duplicate, and
  // answer through the newest socket generation.
  handles.set(parked.requestId, request)
  $uiRequests.set({ ...$uiRequests.get(), [parked.requestId]: parked })
}

/**
 * Requests waiting for the boot disk-plugin scan before they can decide between
 * parking and `{unsupported: true}`. They are not parked yet, so teardown must
 * withdraw them here too, or a cancelled request would park after the scan.
 */
const deferred = new Map<string, { off: () => void; sessionId: string }>()

export function deferUiRequest(requestId: string, sessionId: string, off: () => void): void {
  // A replay of a still-deferred id replaces the earlier wait.
  deferred.get(requestId)?.off()
  deferred.set(requestId, { off, sessionId })
}

/** Stop waiting for `requestId`. True when it was deferred. */
export function withdrawDeferredUiRequest(requestId: string): boolean {
  const entry = deferred.get(requestId)

  if (!entry) {
    return false
  }

  deferred.delete(requestId)
  entry.off()

  return true
}

export const isUiRequestDeferred = (requestId: string): boolean => deferred.has(requestId)

export function unparkUiRequest(requestId: string): void {
  withdrawDeferredUiRequest(requestId)
  handles.delete(requestId)

  const current = $uiRequests.get()

  if (requestId in current) {
    const { [requestId]: _dropped, ...rest } = current
    $uiRequests.set(rest)
  }
}

/** Drop every parked card of one session (turn ended, errored, or the session went away). */
export function clearSessionUiRequests(sessionId: string): void {
  for (const [requestId, entry] of [...deferred]) {
    if (entry.sessionId === sessionId) {
      withdrawDeferredUiRequest(requestId)
    }
  }

  for (const parked of Object.values($uiRequests.get())) {
    if (parked.sessionId === sessionId) {
      unparkUiRequest(parked.requestId)
    }
  }
}

/** Answer with the plugin's payload. False when the request is no longer open. */
export function respondUiRequest(requestId: string, payload: unknown): boolean {
  const request = handles.get(requestId)
  unparkUiRequest(requestId)

  if (!request) {
    return false
  }

  request.respond({ payload: payload ?? null })

  return true
}

/** Dismiss without an answer: the backend sees an unanswered request (`ui.request` → None). */
export function cancelUiRequest(requestId: string): boolean {
  const request = handles.get(requestId)
  unparkUiRequest(requestId)

  if (!request) {
    return false
  }

  request.fail(CANCELLED_CODE, 'plugin UI request dismissed by the user')

  return true
}

const contributionData = (data: unknown): null | UiRequestContribution => {
  const value = data as Partial<UiRequestContribution> | null | undefined

  return value && typeof value.kind === 'string' && typeof value.render === 'function'
    ? (value as UiRequestContribution)
    : null
}

/** The renderer registered for `kind` (first registration wins), from a resolved area snapshot. */
export function contributorForKind(
  contributions: readonly { data?: unknown; id: string }[],
  kind: string
): null | { id: string; data: UiRequestContribution } {
  for (const contribution of contributions) {
    const data = contributionData(contribution.data)

    if (data?.kind === kind) {
      return { data, id: contribution.id }
    }
  }

  return null
}

/** The settled-row renderer for a tool name (`tool` + `renderResult`), if any. */
export function contributorForTool(
  contributions: readonly { data?: unknown; id: string }[],
  toolName: string
): null | { id: string; data: UiRequestContribution } {
  for (const contribution of contributions) {
    const data = contributionData(contribution.data)

    if (data?.tool === toolName && typeof data.renderResult === 'function') {
      return { data, id: contribution.id }
    }
  }

  return null
}

export const hasUiRequestContributor = (kind: string): boolean =>
  contributorForKind(registry.getArea(UI_REQUEST_AREA), kind) !== null

// ── Teardown ─────────────────────────────────────────────────────────────────
// Rides the same plugin-facing tap as `host.onEvent`, so it sees every event
// before the app's own dispatch and cannot affect it.

const eventSessionId = (event: { payload?: unknown; session_id?: string }): string => {
  if (event.session_id) {
    return event.session_id
  }

  const payload = event.payload as { session_id?: unknown } | undefined

  return typeof payload?.session_id === 'string' ? payload.session_id : ''
}

let teardownInstalled = false

/** Idempotent: interrupt / timeout (`request.cancel`) and turn end / session end clear parked cards. */
export function installUiBridgeTeardown(): void {
  if (teardownInstalled) {
    return
  }

  teardownInstalled = true

  onGatewayEvent('request.cancel', event => {
    const id = (event.payload as { id?: unknown } | undefined)?.id

    if (typeof id === 'string') {
      unparkUiRequest(id)
    }
  })

  for (const type of ['message.complete', 'error', 'session.reclaimed']) {
    onGatewayEvent(type, event => {
      const sessionId = eventSessionId(event)

      if (sessionId) {
        clearSessionUiRequests(sessionId)
      }
    })
  }
}

export function resetUiBridgeForTests(): void {
  for (const requestId of [...deferred.keys()]) {
    withdrawDeferredUiRequest(requestId)
  }

  handles.clear()
  $uiRequests.set({})
}
