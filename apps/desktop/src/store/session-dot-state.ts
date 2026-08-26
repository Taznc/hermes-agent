/**
 * SESSION DOT STATE — one map from session id to the single status a surface
 * should paint, so the sidebar, the pane tabs, and the switcher can never
 * disagree about what a session is doing.
 *
 * It exists for two reasons the individual membership atoms cannot cover:
 *
 * 1. PRIORITY IN ONE PLACE. The signals overlap — a session can be working AND
 *    unread AND running a background job — and resolving that per call site is
 *    how surfaces drift apart.
 * 2. LINEAGE. Compression rotates a conversation's stored id, so a sidebar row,
 *    a persisted tile, and the route can each hold a different tip of one
 *    lineage. Every state is claimed under every alias (see `lineageAliases`),
 *    so a surface gets the right answer whichever tip it happens to hold.
 *
 * The inputs are all reference-stable across stream deltas, so this recomputes
 * on status edges rather than per token.
 *
 * Unread has TWO sources, both claiming the same state: the runtime marker
 * (a turn finished in the background while this window wasn't looking at it,
 * $unreadFinishedSessionIds — transient) and the backend's derived read-state
 * watermark (row.unread — persists across restarts and is visible to every
 * surface). The write side of the persisted flag lives in session-unread.ts.
 */

import { atom, computed } from 'nanostores'

import { stableArray, stableRecord } from '@/lib/stable-array'

import { $backgroundRunningSessionIds } from './composer-status'
import { $messagingSessions, $sessions, $unreadFinishedSessionIds, lineageAliases } from './session'
import {
  $attentionSessionIds,
  $draftSessionIds,
  $sessionStates,
  $stalledSessionIds,
  $workingSessionIds
} from './session-states'
import { $unreadWriteGuard, UNREAD_WRITE_GUARD_MS } from './session-unread-remote'
import { $subagentsBySession, activeSubagentCount } from './subagents'

// Sessions parked in async delegation: the parent turn has ended (busy=false —
// delegate_task(background=true) returns its handle the moment the children
// are spawned) while those subagents keep working for minutes. Without this
// input the sidebar row dropped to a plain idle dot mid-delegation, reading as
// "done" while work was still running in child sessions. Same runtime→stored
// bridge and fresh-chat fallback as $backgroundRunningSessionIds:
// $subagentsBySession is keyed by runtime id, surfaces key on stored ids, and
// lineageAliases covers whichever tip of the conversation a surface holds.
let delegatingIds: readonly string[] = []
export const $delegatingSessionIds = computed(
  [$subagentsBySession, $sessionStates, $sessions],
  (bySession, states, sessions) => {
    const ids = new Set<string>()

    for (const [runtimeId, items] of Object.entries(bySession)) {
      if (activeSubagentCount(items) === 0) {
        continue
      }

      for (const alias of lineageAliases(states[runtimeId]?.storedSessionId ?? runtimeId, sessions)) {
        ids.add(alias)
      }
    }

    return (delegatingIds = stableArray(delegatingIds, [...ids]))
  }
)

export type SessionDotState = 'background' | 'draft' | 'idle' | 'needs-input' | 'rate-limited' | 'stalled' | 'unread' | 'working'

// Sessions with a pending, unresolved rate-limit terminal failure (Phase
// 2.12). Keyed by STORED session id (not aliased — a rate limit belongs to
// the exact turn that failed, and lineage rotation on that same session is
// covered by re-claiming under the new tip in the computed below).
// `resetAt` mirrors the failing turn's ErrorSurface.resetAt when known.
// Cleared explicitly on retry/resume/dismiss (never inferred from busy —
// the whole point is a session that is NOT busy but still needs attention).
export const $rateLimitedSessionIds = atom<Readonly<Record<string, { resetAt?: number }>>>({})

export function markSessionRateLimited(storedSessionId: null | string | undefined, resetAt?: number): void {
  if (!storedSessionId) {
    return
  }

  $rateLimitedSessionIds.set({ ...$rateLimitedSessionIds.get(), [storedSessionId]: { resetAt } })
}

export function clearSessionRateLimited(storedSessionId: null | string | undefined): void {
  if (!storedSessionId) {
    return
  }

  const current = $rateLimitedSessionIds.get()

  if (!(storedSessionId in current)) {
    return
  }

  const { [storedSessionId]: _dropped, ...rest } = current
  $rateLimitedSessionIds.set(rest)
}

/** The sidebar row's arc. A quiet turn is still authoritatively running, so
 *  `stalled` keeps it; a blocking prompt drops it, because the amber dot is the
 *  louder cue and two treatments at once fight each other. */
export const showsRunningArc = (state: SessionDotState): boolean => state === 'stalled' || state === 'working'

/** Whether this turn is the session's own, live: brighter title, and the row's
 *  age yields to the actions menu. Wider than the arc — a turn waiting on an
 *  answer has not ended. */
export const hasLiveTurn = (state: SessionDotState): boolean => showsRunningArc(state) || state === 'needs-input'

/** The buckets the sidebar's status filter and ordering work in. `stalled` and
 *  `background` fold into the state a user would name them. `rate-limited` is
 *  its own bucket — distinct from `needs-input` (no answer is required, just
 *  a wait or a manual recovery choice) and from `working` (the turn is not
 *  running). */
export type SessionStatusBucket = 'draft' | 'idle' | 'needs-input' | 'rate-limited' | 'unread' | 'working'

export const sessionStatusBucket = (state: SessionDotState = 'idle'): SessionStatusBucket =>
  state === 'stalled' || state === 'background' ? 'working' : state

const STATUS_RANK: Record<SessionStatusBucket, number> = {
  'needs-input': 0,
  'rate-limited': 1,
  working: 2,
  unread: 3,
  draft: 4,
  idle: 5
}

/** Loudest first — what ordering by status sorts on. */
export const sessionStatusRank = (state?: SessionDotState): number => STATUS_RANK[sessionStatusBucket(state)]

let dotStates: Readonly<Record<string, SessionDotState>> = {}

export const $sessionDotStateById = computed(
  [
    $attentionSessionIds,
    $workingSessionIds,
    $stalledSessionIds,
    $backgroundRunningSessionIds,
    $delegatingSessionIds,
    $unreadFinishedSessionIds,
    $draftSessionIds,
    $sessions,
    $unreadWriteGuard,
    $rateLimitedSessionIds
  ],
  (attention, working, stalled, background, delegating, unread, draft, sessions, unreadWriteGuard, rateLimited) => {
    const next: Record<string, SessionDotState> = {}

    const claim = (ids: readonly string[], state: SessionDotState) => {
      for (const id of ids) {
        for (const alias of lineageAliases(id, sessions)) {
          next[alias] = state
        }
      }
    }

    // Weakest claim first — each pass overwrites the one above it, so the order
    // below IS the priority order. A blocking prompt outranks everything: it is
    // the only state that needs the user.
    //
    // Draft is weakest of all: it says only "no turn has happened here yet", so
    // the first thing that does happen speaks over it.
    claim(draft, 'draft')
    claim(unread, 'unread')

    // Persisted read state (backend watermark): a row marked unread keeps the
    // same emerald dot a background finish would paint, and survives
    // restarts. Same tier as the runtime marker — both mean "there is
    // something here you haven't opened". A list page that predates one of
    // our own writes is fenced out by the write guard: keep OUR value until a
    // page confirms it or the guard expires.
    const persistedUnread: string[] = []

    for (const s of sessions) {
      const entry = unreadWriteGuard.get(s.id)

      if (entry && Date.now() - entry.at < UNREAD_WRITE_GUARD_MS) {
        if (entry.value) {
          persistedUnread.push(s.id)
        }

        continue
      }

      if (s.unread === true) {
        persistedUnread.push(s.id)
      }
    }

    claim(persistedUnread, 'unread')

    claim(background, 'background')
    // Async delegation: the parent turn has ended but its subagents are still
    // running, so the session's work continues in child sessions. Same visual
    // claim as background processes — and it yields to `working` below the
    // moment the parent turn itself is live (synchronous orchestrator children).
    claim(delegating, 'background')
    claim(working, 'working')

    // Stalled REFINES working rather than rivalling it — the turn is still
    // authoritatively running, it has just gone quiet — so it only downgrades a
    // session already claimed as working. The hint outlives its turn by a tick
    // on some paths; without this it could invent a running session.
    for (const id of stalled) {
      for (const alias of lineageAliases(id, sessions)) {
        if (next[alias] === 'working') {
          next[alias] = 'stalled'
        }
      }
    }

    claim(attention, 'needs-input')

    // Rate-limited (Phase 2.12): a terminal failure the user must act on or
    // wait out. Claimed AFTER working/stalled so it forcibly clears any
    // leftover busy/spinner claim for the exact session whose turn just
    // failed — busy=false is already true by the time this fires (the turn
    // ended), but this also guards a straggling stale claim. Below
    // needs-input: a blocking clarify prompt is a rarer, more urgent state
    // and the two are mutually exclusive per session in practice.
    for (const id of Object.keys(rateLimited)) {
      for (const alias of lineageAliases(id, sessions)) {
        if (next[alias] !== 'needs-input') {
          next[alias] = 'rate-limited'
        }
      }
    }

    return (dotStates = stableRecord(dotStates, next))
  }
)

/** Listed, non-archived rows whose resolved status is unread. Alias keys in
 *  `$sessionDotStateById` are ignored unless they are themselves a listed row. */
export function unreadSessionCount(
  byId: Readonly<Record<string, SessionDotState>>,
  ...lists: Array<readonly { archived?: boolean; id: string }[]>
): number {
  let n = 0

  for (const rows of lists) {
    for (const row of rows) {
      if (!row.archived && byId[row.id] === 'unread') {
        n++
      }
    }
  }

  return n
}

/** The titlebar badge. Cron sessions are deliberately EXCLUDED: cron runs
 *  finish unwatched by design, so counting them turns the badge into a cron
 *  run counter that is permanently lit (#93552). Their unread state stays
 *  visible where it belongs — the sidebar's cron section rows — and
 *  "mark all as read" still acks them (ackAllSessionsRead iterates cron rows). */
export const $unreadSessionCount = computed(
  [$sessionDotStateById, $sessions, $messagingSessions],
  (byId, sessions, messaging) => unreadSessionCount(byId, sessions, messaging)
)
