import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { PlatformAvatar } from '@/app/messaging/platform-icon'
import { cn } from '@/lib/utils'
import { $sidebarListLimit, $sidebarMessagingOpenIds, toggleSidebarMessagingOpen } from '@/store/layout'
import { $sidebarMessagingGroups } from '@/store/sidebar-model'

import { SIDEBAR_GROUP_BODY } from './chrome'
import { SidebarLoadMoreRow } from './load-more-row'
import { SidebarSessionsSection } from './sessions-section'

// Mirrors the flat recents/cron sections: compact under a numeric list-length,
// full under 'all'.
const NON_SESSION_LOAD_STEP = 10

interface SidebarMessagingSectionsProps {
  activeSessionId: null | string
  onArchiveSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  onToggleUnread: (sessionId: string) => void
  onLoadMoreMessaging?: (platform: string) => Promise<void> | void
  /** False in the grouped-by-workspace (Projects) view, which has no room for
   *  messaging groups. Kept as a prop (not an internal store read) so the
   *  parent's own `$sidebarWorktreeGroupingActive` subscription is the single
   *  place that gate lives — this component still owns everything ELSE it
   *  reads. */
  visible?: boolean
}

/**
 * Every messaging-platform group, self-managed. Owns its own
 * `$sidebarMessagingGroups` subscription (computed off the messaging session
 * slice + pin membership — see store/sidebar-model.ts) so a messaging-only
 * tick (a new inbound message, a platform total refreshing) never re-renders
 * the rest of the sidebar (Pinned, Recents/Projects, Cron).
 */
export function SidebarMessagingSections({
  activeSessionId,
  onArchiveSession,
  onDeleteSession,
  onResumeSession,
  onTogglePin,
  onToggleUnread,
  onLoadMoreMessaging,
  visible: sectionVisible = true
}: SidebarMessagingSectionsProps) {
  const messagingGroups = useStore($sidebarMessagingGroups)
  const listLimit = useStore($sidebarListLimit)
  const messagingOpenIds = useStore($sidebarMessagingOpenIds)
  const [messagingLoadMorePending, setMessagingLoadMorePending] = useState<Record<string, boolean>>({})
  const [messagingVisible, setMessagingVisible] = useState<Record<string, number>>({})

  const loadMoreForMessaging = (platform: string) => {
    if (!onLoadMoreMessaging) {
      return
    }

    setMessagingLoadMorePending(prev => ({ ...prev, [platform]: true }))

    void Promise.resolve(onLoadMoreMessaging(platform))
      .catch(() => undefined)
      .finally(() => setMessagingLoadMorePending(({ [platform]: _done, ...rest }) => rest))
  }

  const revealMoreMessaging = (platform: string, loaded: number, hasMore: boolean) => {
    const step = typeof listLimit === 'number' ? listLimit : NON_SESSION_LOAD_STEP
    const next = (messagingVisible[platform] ?? step) + NON_SESSION_LOAD_STEP

    setMessagingVisible(prev => ({ ...prev, [platform]: next }))

    if (next > loaded && hasMore) {
      loadMoreForMessaging(platform)
    }
  }

  if (!sectionVisible || !messagingGroups.length) {
    return null
  }

  return (
    <>
      {messagingGroups.map(group => {
        const revealedCount =
          listLimit === 'all' ? group.sessions.length : (messagingVisible[group.sourceId] ?? listLimit)

        const shownSessions = group.sessions.slice(0, revealedCount)
        const canRevealMore = listLimit !== 'all' && (revealedCount < group.sessions.length || group.hasMore)

        return (
          <SidebarSessionsSection
            activeSessionId={activeSessionId}
            contentClassName={cn('flex max-h-56 flex-col gap-px pb-1.75', SIDEBAR_GROUP_BODY)}
            emptyState={null}
            footer={
              canRevealMore ? (
                <SidebarLoadMoreRow
                  loading={Boolean(messagingLoadMorePending[group.sourceId])}
                  onClick={() => revealMoreMessaging(group.sourceId, group.sessions.length, group.hasMore)}
                  step={Math.min(NON_SESSION_LOAD_STEP, Math.max(0, group.total - shownSessions.length))}
                />
              ) : null
            }
            key={group.sourceId}
            label={group.label}
            labelIcon={
              <PlatformAvatar
                className="size-4 rounded-[4px] text-[0.5625rem] [&_svg]:size-3"
                platformId={group.sourceId}
                platformName={group.label}
              />
            }
            onArchiveSession={onArchiveSession}
            onDeleteSession={onDeleteSession}
            onResumeSession={onResumeSession}
            onToggle={() => toggleSidebarMessagingOpen(group.sourceId)}
            onTogglePin={onTogglePin}
            onToggleUnread={onToggleUnread}
            open={messagingOpenIds.includes(group.sourceId)}
            pinned={false}
            rootClassName="shrink-0 p-0"
            sessions={shownSessions}
          />
        )
      })}
    </>
  )
}
