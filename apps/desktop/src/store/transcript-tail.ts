/**
 * REST TAIL-HYDRATION BOOKKEEPING — keyed by STORED session id.
 *
 * `getLatestSessionMessages` loads a small newest-first page instead of a
 * fixed 500-row transcript. When that page comes back full (returned ===
 * limit), older rows likely exist on the backend; this store records that
 * fact plus the offset the next older page starts at, so the transcript
 * window's "Show earlier" action knows to backfill over REST once the
 * in-memory store is fully materialized (see app/chat/transcript-backfill).
 *
 * Offsets use the backend's `order: 'latest'` semantics: measured back from
 * the NEWEST row, with each page returned in chronological order — so the
 * page immediately older than N already-loaded tail rows starts at offset N.
 *
 * Perf: entries are keyed by a profile-scoped composite (JSON-encoded) OR the
 * bare stored id, and callers frequently need "every entry for this stored
 * id regardless of scope" (matchingTailEntries). `transcriptTailState` reads
 * that lookup from ChatRuntimeBoundary's render body — the hottest React path
 * in the app, ticking ~30x/s while a turn streams. A naive lookup that
 * JSON.parses every key to recover its stored id (bounded at
 * TRANSCRIPT_TAIL_LIMIT) turns one render into up to 256 parses; at 30
 * renders/sec that is ~7,700 JSON.parse calls/sec. `storedSessionIdIndex`
 * maintains the reverse mapping (stored id -> its live keys) incrementally on
 * every write/evict, so the lookup is O(matching keys) with no parsing.
 */

import { atom } from 'nanostores'

import type { SessionMessagesResponse } from '@/types/hermes'

export interface TranscriptTailState {
  /** Offset (back from the newest row) where the next older page starts. */
  nextOffset: number
  /** The last hydration page was exactly the page limit, so older rows
   *  likely exist beyond what the in-memory store holds. */
  possiblyTruncated: boolean
  /** Owning profile captured at hydration time, so a later backfill routes
   *  its REST read to the same backend that served the tail. */
  profile?: TranscriptProfileScope
}

export type TranscriptProfileScope =
  | null
  | string
  | {
      connectionId?: null | string
      profile?: null | string
    }

export const $transcriptTailBySessionId = atom<Record<string, TranscriptTailState>>({})
const TRANSCRIPT_TAIL_LIMIT = 256
let transcriptTailOrder: string[] = []

// Reverse index: stored session id -> the set of live entry keys scoped to
// it (a bare key equal to the stored id, and/or profile-scoped composite
// keys). Kept in sync with `$transcriptTailBySessionId` by every writer/
// evictor below so lookups never need to parse a key to recover its id.
const storedSessionIdIndex = new Map<string, Set<string>>()
// Reverse of the reverse: entry key -> the stored id it was recorded under,
// so eviction (which only knows the oldest KEY) can update the index above
// in O(1) instead of re-deriving the id.
const storedSessionIdByKey = new Map<string, string>()

type TailPage = Pick<SessionMessagesResponse, 'messages' | 'pagination'>

function normalizedScope(profile?: TranscriptProfileScope): { connectionId: string; profile: string } | null {
  if (typeof profile === 'string') {
    return { connectionId: '', profile: profile.trim() || 'default' }
  }

  if (!profile) {
    return null
  }

  return {
    connectionId: String(profile.connectionId || '').trim(),
    profile: String(profile.profile || '').trim() || 'default'
  }
}

function transcriptTailKey(storedSessionId: string, profile?: TranscriptProfileScope): string {
  const scope = normalizedScope(profile)

  return scope ? JSON.stringify([scope.connectionId, scope.profile, storedSessionId]) : storedSessionId
}

function indexKey(storedSessionId: string, key: string): void {
  let keys = storedSessionIdIndex.get(storedSessionId)

  if (!keys) {
    keys = new Set()
    storedSessionIdIndex.set(storedSessionId, keys)
  }

  keys.add(key)
  storedSessionIdByKey.set(key, storedSessionId)
}

function unindexKey(key: string): void {
  const storedSessionId = storedSessionIdByKey.get(key)

  if (storedSessionId === undefined) {
    return
  }

  storedSessionIdByKey.delete(key)
  const keys = storedSessionIdIndex.get(storedSessionId)

  if (!keys) {
    return
  }

  keys.delete(key)

  if (keys.size === 0) {
    storedSessionIdIndex.delete(storedSessionId)
  }
}

/** O(1) — no JSON.parse: reads the incrementally-maintained reverse index. */
function matchingTailEntries(storedSessionId: string): Array<[string, TranscriptTailState]> {
  const keys = storedSessionIdIndex.get(storedSessionId)

  if (!keys || keys.size === 0) {
    return []
  }

  const current = $transcriptTailBySessionId.get()
  const entries: Array<[string, TranscriptTailState]> = []

  for (const key of keys) {
    const state = current[key]

    if (state) {
      entries.push([key, state])
    }
  }

  return entries
}

function tailStateFromPage(page: TailPage, profile?: TranscriptProfileScope): TranscriptTailState {
  const pagination = page.pagination

  // No pagination metadata is a legacy backend that ignored the paging query
  // and returned the full transcript: nothing is truncated.
  if (!pagination || pagination.limit <= 0) {
    return { nextOffset: page.messages.length, possiblyTruncated: false, profile }
  }

  return {
    nextOffset: pagination.offset + page.messages.length,
    possiblyTruncated: page.messages.length >= pagination.limit,
    profile
  }
}

function setTranscriptTailEntry(key: string, storedSessionId: string, state: TranscriptTailState): void {
  const current = $transcriptTailBySessionId.get()
  const existing = new Set(Object.keys(current))
  transcriptTailOrder = transcriptTailOrder.filter(candidate => candidate !== key && existing.has(candidate))
  transcriptTailOrder.push(key)

  const next = { ...current, [key]: state }

  indexKey(storedSessionId, key)

  while (transcriptTailOrder.length > TRANSCRIPT_TAIL_LIMIT) {
    const oldest = transcriptTailOrder.shift()

    if (oldest !== undefined) {
      delete next[oldest]
      unindexKey(oldest)
    }
  }

  $transcriptTailBySessionId.set(next)
}

/** Record the outcome of a tail hydration (`getLatestSessionMessages`). */
export function recordTranscriptTail(storedSessionId: string, page: TailPage, profile?: TranscriptProfileScope): void {
  if (!storedSessionId) {
    return
  }

  const key = transcriptTailKey(storedSessionId, profile)
  setTranscriptTailEntry(key, storedSessionId, tailStateFromPage(page, profile))
}

/** Advance the bookkeeping after one older backfill page landed. */
export function recordTranscriptBackfillPage(
  storedSessionId: string,
  page: TailPage,
  profile?: TranscriptProfileScope
): void {
  const current = $transcriptTailBySessionId.get()

  const selected: Array<[string, TranscriptTailState | undefined]> =
    profile === undefined
      ? matchingTailEntries(storedSessionId)
      : [[transcriptTailKey(storedSessionId, profile), current[transcriptTailKey(storedSessionId, profile)]]]

  if (selected.length !== 1) {
    return
  }

  const [key, previous] = selected[0]

  if (!previous) {
    return
  }

  setTranscriptTailEntry(key, storedSessionId, tailStateFromPage(page, previous.profile))
}

export function transcriptTailState(
  storedSessionId: null | string | undefined,
  profile?: TranscriptProfileScope
): TranscriptTailState | undefined {
  if (!storedSessionId) {
    return undefined
  }

  if (profile !== undefined) {
    return $transcriptTailBySessionId.get()[transcriptTailKey(storedSessionId, profile)]
  }

  const matches = matchingTailEntries(storedSessionId)

  return matches.length === 1 ? matches[0][1] : undefined
}

export function clearTranscriptTail(storedSessionId: string, profile?: TranscriptProfileScope): void {
  const current = $transcriptTailBySessionId.get()

  const keys =
    profile === undefined
      ? matchingTailEntries(storedSessionId).map(([key]) => key)
      : [transcriptTailKey(storedSessionId, profile)]

  if (keys.length === 0) {
    return
  }

  const next = { ...current }

  for (const key of keys) {
    delete next[key]
    unindexKey(key)
  }

  const removed = new Set(keys)
  transcriptTailOrder = transcriptTailOrder.filter(key => !removed.has(key))

  $transcriptTailBySessionId.set(next)
}
