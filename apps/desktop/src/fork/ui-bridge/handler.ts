import type { ServerRequestContext } from '@/app/session/hooks/use-message-stream/gateway-event/server-requests'
import { $diskPluginsScanPending } from '@/contrib/runtime-loader'
import {
  deferUiRequest,
  hasUiRequestContributor,
  installUiBridgeTeardown,
  parkUiRequest,
  withdrawDeferredUiRequest
} from '@/fork/ui-bridge/store'
import { type ChatMessage, restorePendingBlockingToolCall } from '@/lib/chat-messages'
import { requestScrollToBottom } from '@/store/thread-scroll'

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/** Interrupted before the card could show: unanswered, so `ui.request` returns None. */
const INTERRUPTED_CODE = -32000

/**
 * The tool call a `plugin.request` belongs to: the newest tool row of the session's
 * current turn (after its last user message) that has no result. Interactive plugin
 * tools never run in parallel (X02a puts them in `_NEVER_PARALLEL_TOOLS`), so while
 * one blocks on the user it is the only result-less row. `sealed` = the row carries a
 * settle-time `completedAt` (a stop / lost completion before a reconnect replay) and
 * must be re-armed. `null` when the transcript is not loaded yet (cold replay); the
 * inline slot then lets the session's open row claim the card.
 */
export function openToolCall(
  messages: readonly ChatMessage[] | undefined
): null | { sealed: boolean; toolCallId: string; toolName: string } {
  for (let m = (messages?.length ?? 0) - 1; m >= 0; m -= 1) {
    const message = messages![m]

    if (message.role === 'user') {
      return null
    }

    for (let p = message.parts.length - 1; p >= 0; p -= 1) {
      const part = message.parts[p]

      if (part.type === 'tool-call' && part.result === undefined && part.toolCallId) {
        return { sealed: part.completedAt !== undefined, toolCallId: part.toolCallId, toolName: part.toolName }
      }
    }
  }

  return null
}

/**
 * `plugin.request` → park the card under its session and originating tool call, or
 * answer `{unsupported: true}` at once when no Desktop plugin renders `kind` (the
 * Python plugin then falls back, e.g. to core clarify). Parked per session exactly
 * like clarify: a BACKGROUND session's request flags "needs input" and waits for
 * the user to open that chat — it never paints into, or scrolls, the foreground one.
 */
export function handlePluginRequest(ctx: ServerRequestContext): void {
  const { deps, request, sessionId } = ctx
  const kind = str(request.params.kind)

  installUiBridgeTeardown()

  if (sessionId && deps.sessionInterrupted(sessionId)) {
    request.fail(INTERRUPTED_CODE, 'session interrupted')

    return
  }

  // Unscoped requests have no transcript to render under.
  if (!kind || !sessionId) {
    request.respond({ unsupported: true })

    return
  }

  if (!hasUiRequestContributor(kind)) {
    // A replay at boot can beat the disk-plugin scan: decide once plugins are in.
    // Deferred (not parked) meanwhile; teardown withdraws it, so a request cancelled
    // or whose session ended before the scan finished is never parked afterwards.
    if ($diskPluginsScanPending.get()) {
      const off = $diskPluginsScanPending.listen(pending => {
        if (!pending && withdrawDeferredUiRequest(request.id)) {
          handlePluginRequest(ctx)
        }
      })

      deferUiRequest(request.id, sessionId, off)

      return
    }

    request.respond({ unsupported: true })

    return
  }

  const explicit = str(request.params.tool_call_id)
  let toolCallId: string | undefined = explicit || undefined

  // Same state write clarify does: flag "needs input" and, on a reconnect replay whose
  // row a stop sealed, re-arm that row so the card renders live instead of as history.
  deps.updateSessionState(sessionId, state => {
    const open = explicit ? null : openToolCall(state.messages)
    toolCallId ??= open?.toolCallId

    const projection = open?.sealed
      ? restorePendingBlockingToolCall(state.messages, { name: open.toolName, tool_id: open.toolCallId })
      : null

    return projection
      ? { ...state, messages: projection.messages, needsInput: true, streamId: projection.streamId }
      : state.needsInput
        ? state
        : { ...state, needsInput: true }
  })

  parkUiRequest(request, {
    kind,
    params: request.params.payload,
    receivedAt: Date.now() / 1000,
    requestId: request.id,
    sessionId,
    toolCallId
  })

  if (sessionId === deps.activeSessionIdRef.current) {
    requestScrollToBottom(sessionId)
  }
}
