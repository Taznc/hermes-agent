import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router'

import { Codicon } from '@/components/ui/codicon'
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '@/components/ui/context-menu'
import { KbdGroup } from '@/components/ui/kbd'
import { SearchField } from '@/components/ui/search-field'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem
} from '@/components/ui/sidebar'
import { TipKeybindLabel } from '@/components/ui/tooltip'
import { useContributions } from '@/contrib/react/use-contributions'
import { useI18n } from '@/i18n'
import { comboTokens } from '@/lib/keybinds/combo'
import { cn } from '@/lib/utils'
import { $bindings } from '@/store/keybinds'
import {
  $panesFlipped,
  $sidebarCronOpen,
  $sidebarPinsOpen,
  pinSession,
  SESSION_SEARCH_FOCUS_EVENT,
  setSidebarCronOpen,
  setSidebarPinsOpen
} from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { $newChatProfile } from '@/store/profile'
import { openProjectCreate } from '@/store/projects'
import { openRouteTile } from '@/store/route-tiles'
import { $sessions } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-states'
import { markSessionUnread } from '@/store/session-unread-remote'
import { $sidebarShowSessionSections, $sidebarWorktreeGroupingActive } from '@/store/sidebar-model'
import type { SessionInfo } from '@/types/hermes'

import {
  type AppView,
  ARTIFACTS_ROUTE,
  CRON_ROUTE,
  MESSAGING_ROUTE,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution,
  SKILLS_ROUTE
} from '../../routes'
import type { SidebarNavItem } from '../../types'
import { type NewSessionSplitHandler, startNewSessionDrag } from '../new-session-drag'

import { SIDEBAR_SCROLL_Y } from './chrome'
import { SidebarCronJobsSection } from './cron-jobs-section'
import { SidebarMessagingSections } from './messaging-sections'
import { SidebarPinsSection } from './pins-section'
import { ProfileRail } from './profile-switcher'
import { ProjectDialog } from './project-dialog'
import { WorktreeDialog } from './projects/worktree-dialog'
import { SidebarSearchSection } from './search-section'
import { SidebarBlankState } from './section-states'
import { CONTEXT_SPLIT_KIT, SplitSubmenu } from './split-submenu'
import { SidebarWorkspaceSection } from './workspace-section'

const SIDEBAR_NAV: SidebarNavItem[] = [
  {
    id: 'new-session',
    label: '',
    icon: props => <Codicon name="robot" {...props} />,
    action: 'new-session',
    keybindActionId: 'session.new'
  },
  {
    id: 'skills',
    label: '',
    icon: props => <Codicon name="symbol-misc" {...props} />,
    route: SKILLS_ROUTE,
    keybindActionId: 'nav.skills'
  },
  {
    id: 'messaging',
    label: '',
    icon: props => <Codicon name="comment" {...props} />,
    route: MESSAGING_ROUTE,
    keybindActionId: 'nav.messaging'
  },
  {
    id: 'artifacts',
    label: '',
    icon: props => <Codicon name="files" {...props} />,
    route: ARTIFACTS_ROUTE,
    keybindActionId: 'nav.artifacts'
  },
  {
    id: 'cron',
    label: '',
    icon: props => <Codicon name="watch" {...props} />,
    route: CRON_ROUTE,
    keybindActionId: 'nav.cron'
  }
]

interface ChatSidebarProps extends React.ComponentProps<typeof Sidebar> {
  currentView: AppView
  onNavigate: (item: SidebarNavItem) => void
  onLoadMoreSessions: () => Promise<void> | void
  onLoadMoreMessaging?: (platform: string) => Promise<void> | void
  onResumeSession: (sessionId: string, session?: SessionInfo) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onBranchSession: (sessionId: string) => void
  onNewSessionInWorkspace: (path: null | string) => void
  /** Create a brand-new session and open it as a tile. `dir` is the dock edge
   *  (or `center` to stack a tab); `anchor`/`before` optionally pin it to a
   *  specific zone / tab-strip slot, and `cwd` pins it to a project's path —
   *  used by the new-session drags (the "New session" row and the project "+"
   *  buttons), which land a fresh session exactly where it's dropped. The
   *  context-menu "Open in split" path passes just a `dir`. */
  onNewSessionSplit: NewSessionSplitHandler
  onManageCronJob: (jobId: string) => void
  onTriggerCronJob: (jobId: string) => Promise<void>
}

/**
 * The sidebar root: layout/scope atoms and cross-cutting effects only. Every
 * per-row store (`$sessions`, `$cronJobs`, `$messagingSessions`,
 * `$projectTree`, `$pullRequestsByBranch`, ...) has moved into the section
 * that actually paints from it (SidebarPinsSection, SidebarWorkspaceSection,
 * SidebarMessagingSections, SidebarCronJobsSection) or into a shared
 * `computed()` derivation in store/sidebar-model.ts that those sections read
 * directly. A tick on any of those stores now re-renders only the section(s)
 * that subscribe to it — this component only re-renders for layout/scope
 * changes (pane flip, nav route, search query) and the coarse "any sessions
 * at all" gate ($sidebarShowSessionSections).
 */
export function ChatSidebar({
  currentView,
  onNavigate,
  onLoadMoreSessions,
  onLoadMoreMessaging,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onBranchSession,
  onNewSessionInWorkspace,
  onNewSessionSplit,
  onManageCronJob,
  onTriggerCronJob
}: ChatSidebarProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const { pathname } = useLocation()
  // Contributed nav rows (plugins pairing a page with a sidebar entry) render
  // below the built-ins with the same chrome; active = at their route.
  const navContributions = useContributions(SIDEBAR_NAV_AREA)

  const contributedNav = useMemo<SidebarNavItem[]>(
    () =>
      navContributions.flatMap(c => {
        const data = c.data as Partial<SidebarNavContribution> | undefined

        if (!data?.path?.startsWith('/') || !data.label) {
          return []
        }

        const codicon = data.codicon || 'plug'

        return [
          {
            id: c.id,
            label: data.label,
            icon: (props: { className?: string }) => <Codicon name={codicon} {...props} />,
            route: data.path
          }
        ]
      }),
    [navContributions]
  )

  const panesFlipped = useStore($panesFlipped)
  const pinsOpen = useStore($sidebarPinsOpen)
  const cronOpen = useStore($sidebarCronOpen)
  const worktreeGroupingActive = useStore($sidebarWorktreeGroupingActive)
  // The sidebar highlight tracks the FOCUSED session — the interacted tile's
  // tab, else the main selection — so it stays 1:1 with whatever tab is active.
  const selectedSessionId = useStore($focusedStoredSessionId)
  const showSessionSections = useStore($sidebarShowSessionSections)

  const newSessionCombo = useStore($bindings)['session.new']?.[0]
  const newSessionKbd = newSessionCombo ? comboTokens(newSessionCombo) : []
  const [searchQuery, setSearchQuery] = useState('')
  const [newSessionKbdFlash, setNewSessionKbdFlash] = useState(false)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const trimmedQuery = searchQuery.trim()

  // Hotkey (session.focusSearch) → focus the field once it's mounted.
  useEffect(() => {
    const onFocus = () => searchInputRef.current?.focus({ preventScroll: true })

    window.addEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)

    return () => window.removeEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)
  }, [])

  // Flash the ⌘N hint full-opacity (no transition) for the press, so hitting
  // the shortcut visibly pings its affordance in the sidebar.
  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout> | undefined

    const onShortcut = () => {
      setNewSessionKbdFlash(true)
      clearTimeout(timeout)
      timeout = setTimeout(() => setNewSessionKbdFlash(false), 140)
    }

    window.addEventListener('hermes:new-session-shortcut', onShortcut)

    return () => {
      window.removeEventListener('hermes:new-session-shortcut', onShortcut)
      clearTimeout(timeout)
    }
  }, [])

  const activeSidebarSessionId = currentView === 'chat' ? selectedSessionId : null

  // Toggle the persisted read-state watermark from a row menu. The row's own
  // `unread` prop mirrors what the dot paints; flip it and let the backend
  // become the truth (optimistic update + rollback in markSessionUnread).
  const toggleUnread = useCallback(
    (storedId: string) => {
      const row = $sessions.get().find(r => r.id === storedId)

      if (!row) {
        return
      }

      markSessionUnread(storedId, row.unread !== true).catch(err => notifyError(err, s.row.unreadFailed))
    },
    [s.row.unreadFailed]
  )

  return (
    <Sidebar
      className={cn(
        // Visibility is the layout tree's job (a hidden zone is display:none;
        // the narrow overlay renders the live instance) — the sidebar always
        // paints itself fully.
        'relative h-full min-w-0 overflow-hidden border-t-0 border-b-0 text-foreground transition-none',
        panesFlipped ? 'border-l border-r-0' : 'border-r border-l-0',
        'border-(--sidebar-edge-border) bg-(--ui-sidebar-surface-background) opacity-100'
      )}
      collapsible="none"
      data-tip-region=""
      data-tour="sessions-sidebar"
    >
      <SidebarContent className="gap-0 overflow-hidden bg-transparent px-2.5">
        <SidebarGroup className="shrink-0 p-0 pb-2 pt-[calc(var(--titlebar-height)+0.375rem)]">
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              {[...SIDEBAR_NAV, ...contributedNav].map(item => {
                const isInteractive = Boolean(item.action) || Boolean(item.route)

                const active =
                  (item.id === 'skills' && currentView === 'skills') ||
                  (item.id === 'messaging' && currentView === 'messaging') ||
                  (item.id === 'artifacts' && currentView === 'artifacts') ||
                  (item.id === 'cron' && currentView === 'cron') ||
                  // Contributed rows light up at their own route.
                  (Boolean(item.route) && pathname === item.route)

                const isNewSession = item.id === 'new-session'

                const button = (
                  <SidebarMenuButton
                    aria-disabled={!isInteractive}
                    className={cn(
                      // no-drag: these rows sit directly under the titlebar's
                      // [-webkit-app-region:drag] strips (app-shell.tsx), with only
                      // 6px of clearance. Drag regions win hit-testing over DOM
                      // (pointer-events can't override), and on Linux/WSLg the
                      // resolved region has been observed to swallow clicks on the
                      // top rows. Same carve-out as USER_BUBBLE_BASE_CLASS in
                      // thread.tsx.
                      'flex h-7 w-full justify-start gap-2 rounded-md border border-transparent px-2 text-left text-[0.8125rem] font-medium text-(--ui-text-secondary) transition-colors duration-100 ease-out [-webkit-app-region:no-drag] hover:bg-(--ui-control-hover-background) hover:text-foreground hover:transition-none',
                      active &&
                        'border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) text-foreground shadow-none hover:border-(--ui-stroke-tertiary)!',
                      !isInteractive &&
                        'cursor-default hover:border-transparent hover:bg-transparent hover:text-inherit'
                    )}
                    // A tip anchored to the label points at the end of the
                    // word; the row is what it's actually about.
                    data-tip-region=""
                    onClick={() => {
                      // A plain new session lands in whatever profile the live
                      // gateway is on (= the active switcher context). null →
                      // no swap. The switcher header is the single place to
                      // change which profile that is.
                      if (isNewSession) {
                        $newChatProfile.set(null)
                      }

                      onNavigate(item)
                    }}
                    onPointerDown={event => {
                      // The "New session" row is a drag source too: drag it onto
                      // a chat zone's tab strip / edge / center to create the
                      // session exactly there (stack / split). The pointer drag
                      // session owns the gesture — a sub-threshold release falls
                      // through to the onClick above (ordinary new session), and
                      // an engaged drag suppresses that click so it never
                      // double-creates. The create callback sets $newChatProfile
                      // itself (the suppressed click can't), so a dragged new
                      // session lands in the same profile a click would.
                      if (!isNewSession) {
                        return
                      }

                      startNewSessionDrag(placement => {
                        $newChatProfile.set(null)
                        onNewSessionSplit(placement.dir, { anchor: placement.anchor, before: placement.before })
                      }, event)
                    }}
                    tooltip={
                      item.keybindActionId
                        ? {
                            children: (
                              <TipKeybindLabel actionId={item.keybindActionId} text={s.nav[item.id] ?? item.label} />
                            )
                          }
                        : (s.nav[item.id] ?? item.label)
                    }
                    type="button"
                  >
                    <item.icon className="size-4 shrink-0 text-[color-mix(in_srgb,currentColor_72%,transparent)]" />
                    {/* Shrink-to-fit, not flex-1: the label carries the row's
                        `data-tour` handle, and anything anchored to it should
                        land at the end of the WORD, not out at the sidebar's
                        edge. Still truncates — `min-w-0` lets it shrink past
                        its content when the rail is narrow — and the trailing
                        chip's `ml-auto` was already doing the pushing that
                        `flex-1` looked like it was for.
                        Its own `sidebar-nav-` namespace: the overlay nav owns
                        `nav-<id>`, and both are on screen with Settings open. */}
                    <span className="min-w-0 truncate" data-tip-arrow-only="" data-tour={`sidebar-nav-${item.id}`}>
                      {s.nav[item.id] ?? item.label}
                    </span>
                    {isNewSession && (
                      <KbdGroup
                        className={cn('ml-auto opacity-55', newSessionKbdFlash && 'opacity-100!')}
                        keys={newSessionKbd}
                        size="sm"
                      />
                    )}
                  </SidebarMenuButton>
                )

                // New session + route-backed pages can open in a split —
                // right-click for the directional "Open in split" submenu.
                return (
                  <SidebarMenuItem key={item.id}>
                    {isNewSession || item.route ? (
                      <ContextMenu>
                        <ContextMenuTrigger asChild>{button}</ContextMenuTrigger>
                        <ContextMenuContent aria-label={s.nav[item.id] ?? item.label}>
                          <SplitSubmenu
                            kit={CONTEXT_SPLIT_KIT}
                            label={s.row.openInSplit}
                            onSplit={dir => {
                              if (isNewSession) {
                                onNewSessionSplit(dir)
                              } else if (item.route) {
                                openRouteTile(item.route, dir)
                              }
                            }}
                          />
                        </ContextMenuContent>
                      </ContextMenu>
                    ) : (
                      button
                    )}
                  </SidebarMenuItem>
                )
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {showSessionSections && (
          <div className="shrink-0 px-2 pb-1 pt-1">
            <SearchField
              aria-label={s.searchAria}
              inputRef={searchInputRef}
              onChange={setSearchQuery}
              placeholder={s.searchPlaceholder}
              value={searchQuery}
            />
          </div>
        )}

        {showSessionSections && (
          <div className={cn('flex min-h-0 flex-1 flex-col pb-1.75', SIDEBAR_SCROLL_Y, '[scrollbar-gutter:stable]')}>
            {trimmedQuery ? (
              <SidebarSearchSection
                activeSessionId={activeSidebarSessionId}
                contentClassName={cn('flex min-h-0 flex-1 flex-col gap-px pb-1.75', SIDEBAR_SCROLL_Y)}
                onArchiveSession={onArchiveSession}
                onBranchSession={onBranchSession}
                onDeleteSession={onDeleteSession}
                onResumeSession={onResumeSession}
                onTogglePin={pinSession}
                onToggleUnread={toggleUnread}
                query={trimmedQuery}
                rootClassName="min-h-32 flex-1 overflow-hidden p-0"
              />
            ) : (
              <>
                <SidebarPinsSection
                  activeSessionId={activeSidebarSessionId}
                  label={s.pinned}
                  onArchiveSession={onArchiveSession}
                  onBranchSession={onBranchSession}
                  onDeleteSession={onDeleteSession}
                  onResumeSession={onResumeSession}
                  onToggle={() => setSidebarPinsOpen(!pinsOpen)}
                  onToggleUnread={toggleUnread}
                  open={pinsOpen}
                />

                <SidebarWorkspaceSection
                  activeSessionId={activeSidebarSessionId}
                  onArchiveSession={onArchiveSession}
                  onBranchSession={onBranchSession}
                  onDeleteSession={onDeleteSession}
                  onLoadMoreSessions={onLoadMoreSessions}
                  onNewSessionInWorkspace={onNewSessionInWorkspace}
                  onNewSessionSplit={onNewSessionSplit}
                  onResumeSession={onResumeSession}
                  onToggleUnread={toggleUnread}
                />

                <SidebarMessagingSections
                  activeSessionId={activeSidebarSessionId}
                  onArchiveSession={onArchiveSession}
                  onDeleteSession={onDeleteSession}
                  onLoadMoreMessaging={onLoadMoreMessaging}
                  onResumeSession={onResumeSession}
                  onTogglePin={pinSession}
                  onToggleUnread={toggleUnread}
                  visible={!worktreeGroupingActive}
                />

                <SidebarCronJobsSection
                  label={s.cronJobs}
                  onManageJob={onManageCronJob}
                  onOpenRun={onResumeSession}
                  onToggle={() => setSidebarCronOpen(!cronOpen)}
                  onTriggerJob={onTriggerCronJob}
                  open={cronOpen}
                  visible={!worktreeGroupingActive}
                />
              </>
            )}
          </div>
        )}

        {!showSessionSections && <SidebarBlankState onNewProject={openProjectCreate} />}

        <div className="shrink-0 px-0.5 pb-1 pt-0.5">
          <ProfileRail />
        </div>
      </SidebarContent>
      <ProjectDialog />
      {/* One mount for the whole app. The header of WorktreeDialog tells why. */}
      <WorktreeDialog />
    </Sidebar>
  )
}
