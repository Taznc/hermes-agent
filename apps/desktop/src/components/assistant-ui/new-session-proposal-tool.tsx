'use client'

import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { useCallback, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { sessionRoute } from '@/app/routes'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { parseMaybeObject } from '@/components/assistant-ui/tool/fallback-model/format'
import { WIDGET_SHELL_CLASS } from '@/components/chat/widget-shell'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { AlertCircle, CheckCircle2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $gateway } from '@/store/gateway'
import {
  clearNewSessionProposalRequest,
  type NewSessionProposalOutcome,
  sessionNewSessionProposalRequest
} from '@/store/new-session-proposal'
import { notifyError } from '@/store/notifications'

import { selectMessageRunning } from './tool/fallback-model'

interface ProposalArgs {
  topic: string
  reason: string
}

function readProposalArgs(args: unknown): ProposalArgs {
  const row = parseMaybeObject(args)

  return {
    reason: typeof row.reason === 'string' ? row.reason : '',
    topic: typeof row.topic === 'string' ? row.topic : ''
  }
}

/** The tool's settled JSON — the card's outcome plus the tool-only
 *  `unanswered` status (timeout, no user action). */
type SettledResult = Omit<Partial<NewSessionProposalOutcome>, 'status'> & {
  status?: 'unanswered' | NewSessionProposalOutcome['status']
}

function readProposalResult(result: unknown): SettledResult {
  return parseMaybeObject(result) as SettledResult
}

const SHELL_CLASS = `${WIDGET_SHELL_CLASS} text-[length:var(--conversation-text-font-size)] text-(--ui-text-primary)`
const ICON_CLASS = 'mt-px size-4 shrink-0 text-(--ui-text-tertiary)'

export const NewSessionProposalTool = (props: ToolCallMessagePartProps) => {
  // Settled → static outcome line (the flow already ran or was declined).
  if (props.result !== undefined) {
    return <NewSessionProposalSettled {...props} />
  }

  return <NewSessionProposalLive {...props} />
}

const NewSessionProposalLive = (props: ToolCallMessagePartProps) => {
  const messageRunning = useAuiState(selectMessageRunning)

  // Stopped mid-prompt with no result — don't leave a dead interactive panel.
  if (!messageRunning) {
    return <ToolFallback {...props} />
  }

  return <NewSessionProposalPending {...props} />
}

function NewSessionProposalSettled({ args, result }: ToolCallMessagePartProps) {
  const { t } = useI18n()
  const copy = t.assistant.newSessionProposal
  const fromArgs = useMemo(() => readProposalArgs(args), [args])
  const fromResult = useMemo(() => readProposalResult(result), [result])

  const topic = fromResult.topic || fromArgs.topic
  const status = fromResult.status ?? 'error'

  const line =
    status === 'approved'
      ? copy.approved
      : status === 'declined'
        ? copy.declined
        : status === 'unanswered'
          ? copy.unanswered
          : copy.failed

  const ok = status === 'approved'
  const neutral = status === 'declined' || status === 'unanswered'

  return (
    <div className={cn(SHELL_CLASS, 'my-1.5 grid gap-1.5')} data-slot="new-session-proposal-inline">
      <div className="flex items-start gap-2">
        {ok ? (
          <CheckCircle2 aria-hidden className={cn(ICON_CLASS, 'text-emerald-400')} />
        ) : neutral ? (
          <Codicon className={ICON_CLASS} name="comment-discussion" size="1rem" />
        ) : (
          <AlertCircle aria-hidden className={cn(ICON_CLASS, 'text-destructive')} />
        )}
        <div className="min-w-0 flex-1">
          <span className={cn('font-medium', neutral && 'italic text-(--ui-text-tertiary)')}>{line}</span>
          {topic && <p className="mt-0.5 text-(--ui-text-secondary)">{topic}</p>}
        </div>
      </div>
    </div>
  )
}

function NewSessionProposalPending({ args }: ToolCallMessagePartProps) {
  const { t } = useI18n()
  const copy = t.assistant.newSessionProposal
  // The tool row is in whichever session's transcript rendered it — read THAT
  // session's request (primary or tile), not the globally-active one.
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionNewSessionProposalRequest(sessionId), [sessionId])
  const request = useStore($request)
  const gateway = useStore($gateway)
  const fromArgs = useMemo(() => readProposalArgs(args), [args])

  const topic = fromArgs.topic || request?.topic || ''
  const reason = fromArgs.reason || request?.reason || ''

  const [working, setWorking] = useState(false)

  // Race: tool.start fires a tick before session.propose.request — hold the
  // buttons until the gateway request is wired (same spinner rule as clarify
  // / setup_mcp).
  const ready = Boolean(request?.requestId)

  const respond = useCallback(
    async (outcome: NewSessionProposalOutcome) => {
      // Another path may have already resolved this request; the store is
      // the single source of truth, so bail if this session's entry is gone
      // — same guard as the approval bar / setup_mcp.
      if (!request || sessionNewSessionProposalRequest(request.sessionId).get()?.requestId !== request.requestId) {
        return
      }

      if (!gateway) {
        notifyError(new Error(copy.gatewayDisconnected), copy.sendFailed)

        return
      }

      // Clear first: the answer is decided, and an in-flight RPC must not
      // leave a live card that can be answered a second time.
      clearNewSessionProposalRequest(request.requestId, request.sessionId)

      try {
        await gateway.request('session.propose.respond', {
          request_id: request.requestId,
          result: JSON.stringify(outcome)
        })
        // tool.complete lands next → NewSessionProposalSettled.
      } catch (error) {
        notifyError(error, copy.sendFailed)
      }
    },
    [copy.gatewayDisconnected, copy.sendFailed, gateway, request]
  )

  const decline = useCallback(() => {
    triggerHaptic('cancel')
    void respond({ status: 'declined', topic })
  }, [respond, topic])

  const approve = useCallback(async () => {
    if (!gateway) {
      notifyError(new Error(copy.gatewayDisconnected), copy.failed)

      return
    }

    setWorking(true)

    try {
      // New session does NOT inherit profile/model/history from the parent —
      // clean start, context arrives entirely via the seeded first message
      // (decided with Josh, 2026-09-19).
      const created = await gateway.request<{ session_id: string; stored_session_id?: string }>('session.create', {
        cols: 96,
        source: 'desktop',
        messages: [{ content: topic, role: 'user' }]
      })

      triggerHaptic('submit')
      await respond({ status: 'approved', topic })

      const routedId = created.stored_session_id ?? created.session_id
      window.location.hash = `#${sessionRoute(routedId)}`
    } catch (error) {
      notifyError(error, copy.failed)
      await respond({ detail: String(error), status: 'error', topic })
    } finally {
      setWorking(false)
    }
  }, [copy.failed, copy.gatewayDisconnected, gateway, respond, topic])

  return (
    <div className={cn(SHELL_CLASS, 'my-1.5 grid gap-2')} data-slot="new-session-proposal-pending">
      <div className="flex items-start gap-2">
        <Codicon className={ICON_CLASS} name="comment-discussion" size="1rem" />
        <div className="min-w-0 flex-1">
          <span className="font-medium">{copy.title}</span>
          {topic && <p className="mt-0.5 text-(--ui-text-secondary)">{copy.description(topic)}</p>}
          {reason && (
            <p className="mt-1 text-xs text-(--ui-text-tertiary)">
              {copy.reasonLabel}: {reason}
            </p>
          )}
        </div>
      </div>

      <div className="flex justify-end gap-2">
        <Button disabled={!ready || working} onClick={decline} type="button" variant="ghost">
          {copy.decline}
        </Button>
        <Button disabled={!ready || working} onClick={() => void approve()} type="button">
          {copy.approve}
        </Button>
      </div>
    </div>
  )
}
