import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { createElement, type FC, useEffect, useMemo } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { toolEntryDisclosureId } from '@/components/assistant-ui/tool/fallback-model/targets'
import { ContribBoundary } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { openToolCall } from '@/fork/ui-bridge/handler'
import {
  $uiRequests,
  cancelUiRequest,
  contributorForKind,
  contributorForTool,
  type ParkedUiRequest,
  respondUiRequest
} from '@/fork/ui-bridge/store'
import { UI_REQUEST_AREA, type UiRequestRenderProps, type UiToolResultRenderProps } from '@/fork/ui-bridge/types'
import type { ChatMessage } from '@/lib/chat-messages'
import { $activeSessionId } from '@/store/session'
import { $toolDisclosureStates, setToolDisclosureOpen } from '@/store/tool-view'

type ToolRowProps = ToolCallMessagePartProps & { completedAt?: number; timestamp?: number }

/**
 * The parked request that renders under THIS row, if any. Matching is scoped to the
 * row's own session, so a background session's request can never paint into the
 * foreground transcript, even when the foreground has an open tool row too. A
 * request whose tool call could not be resolved when it parked (a cold replay that
 * beat hydration) is claimed by the session's current open tool row.
 */
export function parkedForRow(
  requests: Readonly<Record<string, ParkedUiRequest>>,
  sessionId: null | string,
  toolCallId: string,
  messages: () => readonly ChatMessage[]
): null | ParkedUiRequest {
  if (!sessionId) {
    return null
  }

  let unresolved: null | ParkedUiRequest = null

  for (const request of Object.values(requests)) {
    if (request.sessionId !== sessionId) {
      continue
    }

    if (request.toolCallId === toolCallId) {
      return request
    }

    if (request.toolCallId === undefined) {
      unresolved = request
    }
  }

  return unresolved && openToolCall(messages())?.toolCallId === toolCallId ? unresolved : null
}

/**
 * Keep the row's entry disclosure open while a card hangs off it, so a surrounding
 * run of activity is expanded rather than a one-line ticker clipping the card. The
 * user's own prior choice is restored afterwards.
 */
function HoldDisclosureOpen({ args, toolCallId, toolName }: Pick<ToolRowProps, 'args' | 'toolCallId' | 'toolName'>) {
  // Mounted only while a card is live, so ordinary rows never touch the aui store.
  const messageId = useAuiState(state => state.message.id)

  const disclosureId = useMemo(
    () => toolEntryDisclosureId(messageId, { args, toolCallId, toolName }),
    [args, messageId, toolCallId, toolName]
  )

  useEffect(() => {
    const previous = $toolDisclosureStates.get()[disclosureId]
    setToolDisclosureOpen(disclosureId, true)

    return () => setToolDisclosureOpen(disclosureId, previous ?? false)
  }, [disclosureId])

  return null
}

/**
 * The inline slot: wraps the transcript's tool-row component (`ChainToolFallback`).
 *
 * - While a `plugin.request` is parked for this row, the stock row is followed by the
 *   contributor's card, answerable in place.
 * - Once the row has a RESULT (answered live, or hydrated from history) the durable
 *   record is that result: a contributor with `tool` + `renderResult` draws it,
 *   otherwise the stock row does. Nothing else is persisted.
 *
 * Contributor renders are mounted as components (their hooks and errors belong to
 * them) inside the contribution error boundary.
 */
export function withUiRequestSlot(Row: FC<ToolRowProps>): FC<ToolRowProps> {
  const Slotted: FC<ToolRowProps> = props => {
    const contributions = useContributions(UI_REQUEST_AREA)
    const requests = useStore($uiRequests)
    const view = useSessionView()
    const sessionId = useStore(view.$runtimeId)
    const activeSessionId = useStore($activeSessionId)
    const { args, isError, result, toolCallId, toolName } = props
    const hasResult = result !== undefined

    const parked = hasResult ? null : parkedForRow(requests, sessionId, toolCallId, () => view.$messages.get())
    const live = parked ? contributorForKind(contributions, parked.kind) : null
    const settled = hasResult ? contributorForTool(contributions, toolName) : null

    if (settled?.data.renderResult) {
      const resultProps: UiToolResultRenderProps = { args, isError: Boolean(isError), result, toolCallId, toolName }

      return (
        <ContribBoundary id={settled.id} variant="chip">
          {createElement(settled.data.renderResult as FC<UiToolResultRenderProps>, resultProps)}
        </ContribBoundary>
      )
    }

    if (!parked || !live) {
      return <Row {...props} />
    }

    const { requestId } = parked

    const cardProps: UiRequestRenderProps = {
      cancel: () => void cancelUiRequest(requestId),
      isActive: parked.sessionId === activeSessionId,
      kind: parked.kind,
      params: parked.params,
      respond: payload => void respondUiRequest(requestId, payload),
      sessionId: parked.sessionId
    }

    return (
      <>
        <HoldDisclosureOpen args={args} toolCallId={toolCallId} toolName={toolName} />
        <Row {...props} />
        <div className="mt-1.5 min-w-0 max-w-full" data-fork-ui-request={parked.kind}>
          <ContribBoundary id={live.id} variant="chip">
            {createElement(live.data.render as FC<UiRequestRenderProps>, { key: requestId, ...cardProps })}
          </ContribBoundary>
        </div>
      </>
    )
  }

  Slotted.displayName = `withUiRequestSlot(${Row.displayName ?? Row.name ?? 'ToolRow'})`

  return Slotted
}
