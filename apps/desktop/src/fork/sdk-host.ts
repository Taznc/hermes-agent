import { computed, type ReadableAtom } from 'nanostores'

import { forkKanban } from '@/fork/kanban/host'
import { refreshProjectTree } from '@/store/projects'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { sessionTileDelegate } from '@/store/session-states'
import { $archivedSessions, loadArchivedSessions } from '@/store/sidebar-archive'

import {
  $archiveBlockers,
  $archiveSessionRows,
  type ArchiveBlocker,
  canArchiveSession,
  observeArchiveSession,
  takeForkArchiveRefusal
} from './archive-guard'
import { installCompactionWatermarkChip } from './compaction-watermark'
import { SIDEBAR_GROUP_ACTION_AREA } from './sidebar-group-actions'
import { forkUi } from './ui-bridge/sdk'

// "Keep full context" statusbar chip (spec t_07c75c42): always mounted, so it
// is registered once at module load (this module is on the SDK boot chain).
installCompactionWatermarkChip()

/**
 * `host.fork` — the fork's namespaced, versioned Desktop plugin capabilities
 * (ledger Part C; rules: next-branch-policy.md § Fork extension points).
 * Nothing here is flat on `host`, so an upstream method of the same name can
 * never collide. Plugins feature-detect: `host.fork?.sessions?.version >= 1`.
 */

/** Resolve a durable (lineage-root) or live id to the loaded row's live id —
 *  the id the app's own row menu archives with. Unloaded ids pass through;
 *  the archive verb resolves their owner profile itself. */
function liveId(id: string): string {
  const session = $archiveSessionRows.get().find(s => sessionMatchesStoredId(s, id))

  return session ? session.id : id
}

export interface ForkArchiveResult {
  archived: string[]
  failed: { error: string; id: string }[]
  /** Additive v1 capability: present only when active members were skipped. */
  skipped?: { id: string; reason: ArchiveBlocker }[]
}

// Bounded fan-out: each archive is one PATCH plus local cleanup; a 200-row
// group shouldn't open 200 concurrent requests against the backend.
const ARCHIVE_CONCURRENCY = 4

async function archiveMany(ids: readonly string[]): Promise<ForkArchiveResult> {
  const delegate = sessionTileDelegate()

  if (!delegate) {
    throw new Error('Session actions are not ready yet. Try again in a moment.')
  }

  const queue = [
    ...new Set(
      ids
        .map(id => id?.trim())
        .filter(Boolean)
        .map(liveId)
    )
  ]

  const result: ForkArchiveResult = { archived: [], failed: [] }

  const worker = async () => {
    for (let id = queue.shift(); id; id = queue.shift()) {
      const blocker = $archiveBlockers.get()[id]

      if (blocker) {
        ;(result.skipped ??= []).push({ id, reason: blocker })

        continue
      }

      try {
        // THE app archive verb (use-session-actions archiveSession): drops the
        // row from every sidebar slice, tombstones the lineage ids, unpins,
        // closes the tile + runtime state, clears persisted unread, and rolls
        // all of it back on failure. The backend flips `sessions.archived` for
        // the whole compression lineage in one transaction.
        takeForkArchiveRefusal(id)
        await delegate.archiveSession(id)

        // archiveSession reports failure by rolling back + toasting rather
        // than throwing. Its tombstone is the tell: kept on success, removed
        // on rollback, never set when ownership can't be resolved. Works for
        // rows this window never loaded (project members past the preview).
        if (!$removedSessionIds.get().has(id)) {
          const after = $archiveBlockers.get()[id] ?? takeForkArchiveRefusal(id)

          if (after) {
            ;(result.skipped ??= []).push({ id, reason: after })
          } else {
            result.failed.push({ error: 'archive rolled back', id })
          }
        } else {
          result.archived.push(id)
        }
      } catch (err) {
        result.failed.push({ error: err instanceof Error ? err.message : String(err), id })
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(ARCHIVE_CONCURRENCY, queue.length) }, worker))

  // Group archives can empty whole project lanes and change owner counts the
  // per-row cleanup doesn't touch; refresh the derived views once at the end.
  if (ids.length > 1) {
    void refreshProjectTree().catch(() => undefined)
    void loadArchivedSessions()
  }

  return result
}

/** Every id (live + lineage) this window knows to be archived: the Archived
 *  view's set, rows flagged archived in the live cache, and tombstoned rows.
 *  Lets a plugin hide an Archive affordance on rows that are already archived. */
const $archivedIds: ReadableAtom<ReadonlySet<string>> = computed(
  [$archivedSessions, $sessions, $removedSessionIds],
  (archived, sessions, removed) => {
    const ids = new Set<string>(removed)

    for (const session of [...archived, ...sessions.filter(s => s.archived)]) {
      ids.add(session.id)

      if (session._lineage_root_id) {
        ids.add(session._lineage_root_id)
      }

      for (const id of session._lineage_ids ?? []) {
        ids.add(id)
      }
    }

    return ids
  }
)

export const forkHost = {
  sessions: {
    version: 1 as const,
    /** Archive sessions through the app's own archive verb (identical to the
     *  row menu's Archive). Accepts live or durable ids. Never rejects for a
     *  per-row failure — inspect `failed`. */
    archive: (ids: readonly string[]): Promise<ForkArchiveResult> => archiveMany(ids),
    /** Read-only set of ids known archived (subscribe with `useValue`). */
    archivedIds: $archivedIds,
    /** Reactive work blockers; absent entries are idle, not absent runtimes. */
    archiveBlockers: $archiveBlockers,
    /** Synchronous renderer hint; the canonical action and backend recheck. */
    canArchive: canArchiveSession,
    /** Observe mounted archive affordances; unsubscribe on row disposal. */
    observeArchive: observeArchiveSession
  },
  sidebar: {
    version: 1 as const,
    /** Contribution area for group-header actions (see sidebar-group-actions). */
    GROUP_ACTION_AREA: SIDEBAR_GROUP_ACTION_AREA
  },
  /** X02 plugin UI bridge (see ui-bridge/sdk.ts). */
  ui: forkUi,
  /** Kanban focus mode + All Boards (see kanban/host.ts). */
  kanban: forkKanban
}
