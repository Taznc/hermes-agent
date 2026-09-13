/**
 * Pure event/run text derivation for the task drawer — no React, no styling.
 * Split out of `drawer.tsx` so the feed's "what does this row say" logic can
 * be read and tested without pulling in the whole detail view.
 */

import { columnLabel, type KanbanText } from './i18n'
// The plain-language framing for raw dispatcher diagnostics
// (`stale_lock=...`, `pid N not alive`, ...) lives in `status-guidance.ts` so
// the CTA banner's automatic-failure copy and this file's run-history lines
// can never drift apart — one function, two renderers.
import { runErrorText } from './status-guidance'
import type { KanbanEvent } from './types'

export { runErrorText } from './status-guidance'

/**
 * Turn a task_events row into an operator-readable line. The backend logs
 * machine payloads ("status" + {"status":"ready"}); rendering the raw kind
 * made the feed useless ("status · 2 sec. ago" after a drag). Known kinds get
 * prose with the payload folded in; unknown kinds fall back to kind + compact
 * key=value detail so new backend events still say something.
 */
export function eventText(event: KanbanEvent, k: KanbanText): { detail?: string; label: string } {
  let p: Record<string, unknown> = {}

  if (typeof event.payload === 'string' && event.payload) {
    try {
      p = JSON.parse(event.payload) as Record<string, unknown>
    } catch {
      return { label: event.kind.replace(/_/g, ' '), detail: event.payload }
    }
  } else if (event.payload && typeof event.payload === 'object') {
    p = event.payload as Record<string, unknown>
  }

  const str = (key: string): null | string => {
    const value = p[key]

    return typeof value === 'string' && value ? value : null
  }

  const col = (key: string) => {
    const value = str(key)

    return value ? columnLabel(k, value) : null
  }

  switch (event.kind) {
    case 'created':
      return { label: k.evtCreated(col('status') ?? '', str('assignee') ?? '') }
    case 'status': {
      const reason = str('reason')

      return {
        label: k.evtMovedTo(col('status') ?? '?'),
        detail: reason === 'parent_reopened' ? k.evtParentReopened(str('parent') ?? '') : (reason ?? undefined)
      }
    }

    case 'assigned': {
      const assignee = str('assignee')

      return { label: assignee ? k.evtAssignedTo(assignee) : k.evtUnassigned }
    }

    case 'commented':
      return { label: k.evtCommentBy(str('author') ?? k.someone) }

    case 'claimed':
      return { label: str('source_status') === 'review' ? k.evtClaimedReview : k.evtClaimedWorker }

    case 'spawned':
      return { label: k.evtWorkerStarted, detail: p.pid != null ? `pid ${p.pid}` : undefined }

    case 'completed':
      return { label: k.evtCompleted }

    case 'blocked':
      return { label: k.evtBlocked, detail: str('reason') ?? undefined }

    case 'unblocked':
      return { label: k.evtUnblocked(col('status') ?? '') }

    case 'reclaimed':
      return { label: k.evtReclaimed, detail: str('reason') ?? undefined }

    case 'specified':
      return { label: k.evtSpecified }

    case 'promoted':
      return { label: k.evtPromoted }

    case 'scheduled':
      return { label: k.evtScheduled, detail: str('reason') ?? undefined }

    case 'archived':
      return { label: k.evtArchived }

    case 'reprioritized':
      return { label: k.evtReprioritized(String(p.priority ?? '?')) }
    case 'gave_up': {
      const rawError = str('error')

      return { label: k.evtGaveUp, detail: rawError ? runErrorText(rawError, k).primary : undefined }
    }

    case 'crashed': {
      const rawError = str('error')

      return { label: k.evtCrashed, detail: rawError ? runErrorText(rawError, k).primary : undefined }
    }

    case 'timed_out':
      return { label: k.evtTimedOut }

    case 'protocol_violation':
      return { label: k.evtProtocolViolation }

    case 'review_no_verdict':
      return { label: k.evtReviewNoVerdict }

    case 'review_round_cap':
      return { label: k.evtReviewRoundCap, detail: str('reason') ?? undefined }

    case 'stale':
      return { label: k.evtStale }

    case 'dependency_wait':
      return { label: k.evtDependencyWait, detail: str('reason') ?? undefined }

    case 'block_loop_detected':
      return { label: k.evtBlockLoop, detail: str('reason') ?? undefined }

    case 'held':
      return { label: k.evtHeld, detail: str('reason') ?? undefined }
    default: {
      const detail = Object.entries(p)
        .filter(([, value]) => value != null && typeof value !== 'object')
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(' ')

      return { label: event.kind.replace(/_/g, ' '), detail: detail || undefined }
    }
  }
}

/**
 * ACTIVITY · 71 rendering as six identical "heartbeat" rows is pure noise —
 * group consecutive events that render to the same label+detail into one
 * summarized row (expandable) instead of listing each one. Grouping on the
 * RENDERED text (not the raw `kind`) means two different kinds that happen
 * to read identically still collapse, and a kind whose detail changes
 * between events (e.g. two different comment authors) does NOT collapse —
 * exactly the granularity a human scanning the feed wants.
 */
export interface ActivityGroup {
  events: KanbanEvent[]
  label: string
  detail?: string
}

export function groupActivity(events: KanbanEvent[], k: KanbanText): ActivityGroup[] {
  const groups: ActivityGroup[] = []

  for (const event of events) {
    const { detail, label } = eventText(event, k)
    const last = groups[groups.length - 1]

    if (last && last.label === label && last.detail === detail) {
      last.events.push(event)
    } else {
      groups.push({ detail, events: [event], label })
    }
  }

  return groups
}

/** The reason text on the most recent `blocked` event, if any — this is the
 *  worker's own explanation for why the task is stuck, surfaced verbatim in
 *  the CTA banner rather than making the user dig through Activity for it. */
export function latestBlockReason(events: KanbanEvent[]): null | string {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]

    if (event.kind === 'blocked' || event.kind === 'block_loop_detected') {
      const payload = event.payload

      if (typeof payload === 'string' && payload) {
        try {
          const parsed = JSON.parse(payload) as Record<string, unknown>
          const reason = parsed.reason

          return typeof reason === 'string' && reason ? reason : null
        } catch {
          return null
        }
      } else if (payload && typeof payload === 'object') {
        const reason = (payload as Record<string, unknown>).reason

        return typeof reason === 'string' && reason ? reason : null
      }

      return null
    }
  }

  return null
}

/** Same lookup as `latestBlockReason`, but also carries the event's id — the
 *  stable handle that binds a clicked answer to the specific question it
 *  answers (`ChoiceResponse.question_event_id`), so a re-block with a new
 *  question can never be confused with an old, already-answered one. */
export function latestBlockEvent(events: KanbanEvent[]): null | {
  id: number
  intentionalInitialBlock: boolean
  reason: string
} {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]

    if (event.kind === 'blocked' || event.kind === 'block_loop_detected') {
      const reason = latestBlockReason([event])

      const payload = event.payload
      const parsed = typeof payload === 'string'
        ? (() => {
            try {
              return JSON.parse(payload) as Record<string, unknown>
            } catch {
              return null
            }
          })()
        : payload as Record<string, unknown> | null

      return reason
        ? { id: event.id, intentionalInitialBlock: parsed?.intentional_initial_block === true, reason }
        : null
    }
  }

  return null
}

/** Run outcomes that mean the run did NOT succeed — drives the failed-run
 *  rollup badge and the destructive row tone. */
export const FAILED_OUTCOMES = ['crashed', 'failed', 'timed_out', 'gave_up']

export const isFailedRun = (outcome: string) => FAILED_OUTCOMES.includes(outcome)
