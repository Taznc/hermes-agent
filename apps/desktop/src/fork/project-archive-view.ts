import { useStore } from '@nanostores/react'

import { excludeProjectSessions, type SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { SessionInfo } from '@/hermes'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import { $removedSessionIds, sessionRemovalIntersected, tombstoneRowIds } from '@/store/session-removal'

const NO_GENERATIONS = new Map<string, number>()

/** A local hydrated expansion is a cache, too. Its removal snapshot must survive
 * projects.tree pruning membership tombstones, just like in-flight fetch guards.
 * A rolled-back removal releases the lifecycle edge; an explicitly restored row
 * in the live cache re-admits the conversation even after a prune. */
export function useArchiveView(
  project: SidebarProjectTree,
  preview: SessionInfo[] | undefined,
  hidden: ((session: SessionInfo) => boolean) | undefined,
  hiddenCount: number
): [SidebarProjectTree, (session: SessionInfo) => boolean, number] {
  const removed = useStore($removedSessionIds)
  const sessions = useStore($sessions)
  // Empty baseline also covers expansions mounted AFTER an archive/prune.
  const snapshot = NO_GENERATIONS

  const isHidden = (row: SessionInfo): boolean => {
    if (row.archived || hidden?.(row)) {
      return true
    }

    const ids = tombstoneRowIds(row)

    if (ids.some(id => removed.has(id))) {
      return true
    }

    const restored = sessions.some(s => !s.archived && ids.some(id => sessionMatchesStoredId(s, id)))

    return !restored && ids.some(id => sessionRemovalIntersected(snapshot, id))
  }

  const original = project
  project = excludeProjectSessions(project.repos ? project : { ...project, repos: [] }, isHidden)

  // Overview repos are intentionally empty: deriving the count from lane rows
  // collapses it to zero as soon as one preview is filtered. Complete owner ids
  // let us count removals even after canonical archive evicted the loaded row.
  if (original.sessionIds !== undefined) {
    const known = [
      ...sessions,
      ...(original.previewSessions ?? []),
      ...(preview ?? []),
      ...(original.repos ?? []).flatMap(r => r.groups.flatMap(g => g.sessions))
    ]

    const ids = [...new Set(original.sessionIds)]
    hiddenCount = ids.filter(id =>
      isHidden(known.find(s => sessionMatchesStoredId(s, id)) ?? ({ id } as SessionInfo))
    ).length
    project = { ...project, sessionCount: ids.length }
  } else {
    project = { ...project, sessionCount: original.sessionCount }
  }

  // [] is an authoritative overlay, not "no data". Keep repo metadata for
  // project actions/cwd, but don't let core's fallback reinsert raw sessions.
  if (preview?.length === 0) {
    project = {
      ...project,
      repos: (project.repos ?? []).map(repo => ({
        ...repo,
        groups: repo.groups.map(group => ({ ...group, sessions: [] }))
      }))
    }
  }

  return [project, isHidden, hiddenCount]
}
