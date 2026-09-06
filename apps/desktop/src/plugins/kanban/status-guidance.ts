/**
 * Single source of truth for "what happened, and what should I do about it"
 * for every visible Kanban status. Pure, no React — `drawer.tsx`'s CtaBanner
 * (and any future consumer: card, tooltip) renders this, so the two surfaces
 * cannot drift the way the old `blocked`-only `latestBlockReason` scan did.
 *
 * Background: `blocked` is not one state — it's at least four, rendered
 * identically before this module existed:
 *   A. manual `kanban_block(reason=...)`      -> event `blocked`, block_kind set
 *   B. automatic circuit-breaker trip          -> event `gave_up`, block_kind NULL
 *   C. reviewer exited with no verdict         -> event `review_no_verdict`
 *   D. born blocked (`initial_status=blocked`) -> possibly zero events at all
 * (A) had a typed reason and rendered correctly; (B)/(C)/(D) fell back to
 * "Blocked — needs your input" / "did not record a reason", which is false
 * for an automatic failure and confusing for a reviewer hand-off. See
 * KANBAN-STATUS-AUDIT.md (parent card) for the full trace.
 */
import type { KanbanEvent, KanbanRun, KanbanTaskFull } from './types'

/** The subset of `KanbanText` `runErrorText` needs — kept narrow so callers
 *  without the full bound i18n object (e.g. `completion-notify.ts`, which
 *  has its own small `t()` wrapper, not a React hook) can still humanize a
 *  dispatcher diagnostic without pulling in the whole plugin i18n surface. */
export interface RunErrorTextDeps {
  runErrPidExited: (code: string) => string
  runErrPidNotAlive: string
  runErrPidSignaled: (signal: string) => string
  runErrStaleLock: string
}

/**
 * The dispatcher writes machine-shaped diagnostics into `run.error` /
 * `gave_up.error` (`stale_lock=hermes-dev:27237`, `pid 111279 not alive`) that
 * a human cannot act on unread. Recognized shapes get a plain-language
 * primary line; the raw string stays available as `raw` for an expand
 * toggle. Unrecognized shapes fall back to showing the raw string as
 * primary — there's nothing to translate.
 */
export function runErrorText(error: string, k: RunErrorTextDeps): { primary: string; raw?: string } {
  const staleLock = /^stale_lock=(.+)$/.exec(error)

  if (staleLock) {
    return { primary: k.runErrStaleLock, raw: error }
  }

  const notAlive = /^pid \d+ not alive$/.exec(error)

  if (notAlive) {
    return { primary: k.runErrPidNotAlive, raw: error }
  }

  const exited = /^pid \d+ exited with code (.+)$/.exec(error)

  if (exited) {
    return { primary: k.runErrPidExited(exited[1]), raw: error }
  }

  const signaled = /^pid \d+ killed by signal (.+)$/.exec(error)

  if (signaled) {
    return { primary: k.runErrPidSignaled(signaled[1]), raw: error }
  }

  return { primary: error }
}

/** Typed reasons `kanban_block` may record (see backend `VALID_BLOCK_KINDS`).
 *  `dependency` is intentionally excluded: a dependency block never sits in
 *  the `blocked` column (it routes to `todo` — see `_route_block`), so it can
 *  never be the `block_kind` of a task this resolver is asked about. */
export type BlockKind = 'capability' | 'needs_input' | 'transient'

/** The event kinds the backend's own recency scans treat as authoritative
 *  lifecycle markers (`_has_sticky_block`, `_gave_up_was_force_tripped`,
 *  `_resume_status_from_events` in kanban_db.py) — reusing the same set here
 *  is the anti-drift argument: one definition of "what counts", shared by
 *  the backend's routing and the desktop's display. `unblocked` terminates
 *  the scan: anything before it is a resolved, stale incident. */
const CAUSE_EVENT_KINDS = new Set([
  'blocked',
  'block_loop_detected',
  'gave_up',
  'review_no_verdict',
  'crashed',
  'timed_out',
  'protocol_violation',
  'rate_limited',
  'stale',
  'dependency_wait',
  'scheduled',
  'held',
  'unblocked'
])

function eventPayload(event: KanbanEvent): Record<string, unknown> {
  const payload = event.payload

  if (typeof payload === 'string' && payload) {
    try {
      return JSON.parse(payload) as Record<string, unknown>
    } catch {
      return {}
    }
  }

  if (payload && typeof payload === 'object') {
    return payload as Record<string, unknown>
  }

  return {}
}

const str = (p: Record<string, unknown>, key: string): null | string => {
  const value = p[key]

  return typeof value === 'string' && value ? value : null
}

/** The resolved reason a task is `blocked`, in the shape the CtaBanner needs
 *  to pick copy + actions. `origin` distinguishes what actually happened —
 *  never fabricated: `unknown` means no cause was found anywhere and the UI
 *  must say so honestly rather than inventing one. */
export type BlockCause =
  | { origin: 'manual'; reason: string; kind: BlockKind | null }
  | { origin: 'automatic'; raw: string }
  | { origin: 'review_no_verdict' }
  | { origin: 'unknown' }

/**
 * Recency-ordered backward scan over `events`, mirroring the backend's own
 * scans (`kanban_db.py`: `_has_sticky_block`, `_gave_up_was_force_tripped`,
 * `_resume_status_from_events`). NOT "manual always wins" — a card can be
 * manually blocked, unblocked, then auto-blocked (or the reverse), so only
 * the MOST RECENT relevant event decides. Falls through, in order, to
 * `task.last_failure_error`, then the newest failed run's `error`, before
 * giving up honestly.
 */
export function resolveBlockCause(task: KanbanTaskFull, events: KanbanEvent[], runs: KanbanRun[]): BlockCause {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]

    if (!CAUSE_EVENT_KINDS.has(event.kind)) {
      continue
    }

    if (event.kind === 'unblocked') {
      // Whatever came before this is a resolved, stale incident.
      break
    }

    const p = eventPayload(event)

    if (event.kind === 'blocked' || event.kind === 'block_loop_detected') {
      const reason = str(p, 'reason')
      // `task.block_kind` (not the event payload) is the durable, typed
      // field the backend keeps across unblock/re-block cycles — it's the
      // same field `_route_block` writes and the same one the OLD CTA logic
      // read, so this preserves exact icon/title behavior for manual blocks.
      const kind = (task.block_kind ?? null) as BlockKind | null

      return { kind, origin: 'manual', reason: reason ?? '' }
    }

    if (event.kind === 'review_no_verdict') {
      return { origin: 'review_no_verdict' }
    }

    // gave_up / crashed / timed_out / protocol_violation / rate_limited /
    // stale / dependency_wait / scheduled / held all carry the failure (or
    // wait reason) in `payload.error` or `payload.reason`.
    const automatic = str(p, 'error') ?? str(p, 'reason')

    if (automatic) {
      return { origin: 'automatic', raw: automatic }
    }

    break
  }

  if (task.last_failure_error) {
    return { origin: 'automatic', raw: task.last_failure_error }
  }

  const newestFailedRun = [...runs].reverse().find(run => run.error)

  if (newestFailedRun?.error) {
    return { origin: 'automatic', raw: newestFailedRun.error }
  }

  return { origin: 'unknown' }
}

/** The subset of `KanbanText` `statusGuidance` needs. */
export interface StatusGuidanceDeps extends RunErrorTextDeps {
  col: Record<string, { help: string }>
  guideAssignReady: string
  guideBlockLoop: (reason: string) => string
  guideBlockedAutomatic: (cause: string) => string
  guideBlockedGeneric: string
  guideBlockedManualCapability: string
  guideBlockedManualTransient: string
  guideBlockedReviewNoVerdict: string
  guideBlockedUnknown: string
  guideDone: string
  guideOnHold: string
  guideReadyQueued: string
  guideReview: string
  guideRunning: string
  guideRunningStale: string
  guideScheduled: string
  guideTodo: string
  guideTriage: string
}

/** One-line, plain-English "what happened / what to do" for a task's CURRENT
 *  status — the drawer's always-present answer, independent of whether the
 *  louder `CtaBanner` (blocked/review only) also renders. Every
 *  `COLUMN_META` id must resolve to a non-empty string here: that coverage
 *  is a behavior contract enforced by a test that walks `COLUMN_META`, not a
 *  hardcoded list, so a new backend status can't silently render blank.
 *
 *  A dict of pure per-status resolvers (never an if/elif ladder) — each may
 *  read `events`/`task` for a sharper answer (e.g. the actual
 *  `block_loop_detected` reason in `triage`) but must never fabricate a
 *  cause that isn't in the data. */
const GUIDANCE_RESOLVERS: Record<string, (task: KanbanTaskFull, events: KanbanEvent[], runs: KanbanRun[], k: StatusGuidanceDeps) => string> = {
  archived: (_task, _events, _runs, k) => k.col.archived?.help ?? '',

  blocked: (task, events, runs, k) => {
    const cause = resolveBlockCause(task, events, runs)

    // A. Manual block: the banner above already shows the worker's own
    // words (verbatim, or parsed into clickable options with the fence
    // stripped) — this line's job is the NEXT ACTION, not a second copy of
    // the reason, so it never re-prints `cause.reason` (and never risks
    // leaking raw ```choices fence syntax the banner deliberately strips).
    // The next action differs by `cause.kind`: `needs_input` (or no typed
    // kind) really is a question for a human — reply/unblock is right.
    // `capability` and `transient` are NOT "needs your input": the data
    // already names a different next step (retry once available / wait
    // out the flake), so asserting "needs your input" there would be the
    // same false-framing class as defect 1, one branch over.
    if (cause.origin === 'manual') {
      if (cause.kind === 'capability') {
        return k.guideBlockedManualCapability
      }

      if (cause.kind === 'transient') {
        return k.guideBlockedManualTransient
      }

      return k.guideBlockedGeneric
    }

    if (cause.origin === 'automatic') {
      return k.guideBlockedAutomatic(runErrorText(cause.raw, k).primary)
    }

    if (cause.origin === 'review_no_verdict') {
      return k.guideBlockedReviewNoVerdict
    }

    // D. No cause found anywhere — the banner already states the diagnosis
    // ("no cause is recorded") and offers Retry/Copy-log; this line must be
    // the distinct next-action (inspect the log, then retry/reassign), not
    // a second copy of the banner body — that duplication is exactly the
    // anti-pattern defect 2 removed from the manual arm. Must NOT be
    // `guideBlockedGeneric` either: that copy asserts "needs your input",
    // a cause this state doesn't have.
    return k.guideBlockedUnknown
  },

  done: (_task, _events, _runs, k) => k.guideDone,

  on_hold: (_task, _events, _runs, k) => k.guideOnHold,

  ready: (task, _events, _runs, k) => (task.assignee ? k.guideReadyQueued : k.guideAssignReady),

  review: (_task, _events, _runs, k) => k.guideReview,

  running: (task, _events, _runs, k) => {
    const stale = task.last_heartbeat_at ? Date.now() / 1000 - task.last_heartbeat_at > 120 : false

    return stale ? k.guideRunningStale : k.guideRunning
  },

  scheduled: (_task, _events, _runs, k) => k.guideScheduled,

  todo: (_task, _events, _runs, k) => k.guideTodo,

  triage: (_task, events, _runs, k) => {
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i]

      if (event.kind === 'block_loop_detected') {
        const reason = str(eventPayload(event), 'reason')

        return reason ? k.guideBlockLoop(reason) : k.guideTriage
      }

      if (event.kind === 'unblocked') {
        break
      }
    }

    return k.guideTriage
  }
}

/** Look up a status's guidance; unknown backend statuses fall back to their
 *  raw column help (or empty, matching `columnHelp`'s own fallback). */
export function statusGuidance(
  status: string,
  task: KanbanTaskFull,
  events: KanbanEvent[],
  runs: KanbanRun[],
  k: StatusGuidanceDeps
): string {
  const resolver = GUIDANCE_RESOLVERS[status]

  return resolver ? resolver(task, events, runs, k) : (k.col[status]?.help ?? '')
}
