import type { useSensors } from '@dnd-kit/core'
import { useStore } from '@nanostores/react'
import { useCallback } from 'react'

import { useStoreSelector } from '@/lib/use-session-slice'
import { setPinnedSessionOrder, unpinSession } from '@/store/layout'
import { sessionPinId } from '@/store/session'
import { $sidebarAllProfilesActive, $sidebarPinnedSessions, $sidebarSessionByAnyId } from '@/store/sidebar-model'

import { SidebarPinnedEmptyState } from './section-states'
import { SidebarSessionsSection } from './sessions-section'

interface SidebarPinsSectionProps {
  label: string
  open: boolean
  onToggle: () => void
  activeSessionId: null | string
  onArchiveSession: (sessionId: string) => void
  onBranchSession?: (sessionId: string, profile?: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onToggleUnread: (sessionId: string) => void
  dndSensors?: ReturnType<typeof useSensors>
}

/**
 * Owns its own `$sidebarPinnedSessions` subscription (a computed store built
 * off `$pinnedSessionIds` + the session/cron/messaging slices — see
 * store/sidebar-model.ts). The parent used to compute this same chain itself
 * off ~6 raw `useStore()` calls, so a session-list or messaging tick
 * re-rendered the ENTIRE sidebar to re-derive a section most ticks don't
 * change at all. Subscribing here confines that churn to Pinned.
 */
export function SidebarPinsSection({
  label,
  open,
  onToggle,
  activeSessionId,
  onArchiveSession,
  onBranchSession,
  onDeleteSession,
  onResumeSession,
  onToggleUnread,
  dndSensors
}: SidebarPinsSectionProps) {
  const pinnedSessions = useStore($sidebarPinnedSessions)
  const showAllProfiles = useStore($sidebarAllProfilesActive)

  // Sortable rows carry live session ids; the pinned store is keyed by durable
  // (lineage-root) ids, so translate before persisting the new order.
  // `sessionByAnyId` is read imperatively (not subscribed) — the callback only
  // needs the CURRENT map at drop time, not a re-render whenever it changes.
  const reorderPinned = useCallback((ids: string[]) => {
    const sessionByAnyId = $sidebarSessionByAnyId.get()

    setPinnedSessionOrder(
      ids.map(id => {
        const session = sessionByAnyId.get(id)

        return session ? sessionPinId(session) : id
      })
    )
  }, [])

  return (
    <SidebarSessionsSection
      activeSessionId={activeSessionId}
      contentClassName="flex flex-col gap-px rounded-lg pb-2 pt-1"
      dndSensors={dndSensors}
      emptyState={<SidebarPinnedEmptyState />}
      label={label}
      onArchiveSession={onArchiveSession}
      onBranchSession={onBranchSession}
      onDeleteSession={onDeleteSession}
      onReorderSessions={reorderPinned}
      onResumeSession={onResumeSession}
      onToggle={onToggle}
      onTogglePin={unpinSession}
      onToggleUnread={onToggleUnread}
      open={open}
      pinned
      rootClassName="shrink-0 p-0 pb-1"
      sessions={pinnedSessions}
      showProfileTags={showAllProfiles}
      sortable={pinnedSessions.length > 1}
    />
  )
}

/** Just the count — for callers (the empty-state copy in the Sessions section)
 *  that need "is anything pinned" without paying for the whole resolved list.
 *  A `useStoreSelector` scalar read: re-renders only when the COUNT changes,
 *  not on every reference churn of the underlying pinned list. */
export function useSidebarPinnedCount(): number {
  return useStoreSelector($sidebarPinnedSessions, sessions => sessions.length)
}
