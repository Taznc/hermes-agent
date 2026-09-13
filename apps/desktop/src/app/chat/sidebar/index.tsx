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
  $sidebarPrDataWanted,
  $sidebarPrFilter,
  $sidebarProfileFilter,
  $sidebarProjectFilter,
  $sidebarProjectOrderIds,
  $sidebarRecentsOpen,
  $sidebarSessionOrderIds,
  $sidebarSessionOrderManual,
  $sidebarShowAllSessions,
  $sidebarShowArchived,
  $sidebarStatusFilter,
  $sidebarWorkspaceOrderIds,
  $sidebarWorkspaceParentOrderIds,
  filterVisibleProjects,
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
import { $focusedSessionIsTile, $focusedStoredSessionId } from '@/store/session-states'
import { markSessionUnread } from '@/store/session-unread-remote'
import { $sidebarShowSessionSections, $sidebarWorktreeGroupingActive } from '@/store/sidebar-model'
import type { SessionInfo } from '@/types/hermes'

import {
  type AppView,
  ARTIFACTS_ROUTE,
  CRON_ROUTE,
  MESSAGING_ROUTE,
  SESSION_IMPORT_ROUTE,
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
  },
  {
    id: 'session-import',
    label: '',
    icon: props => <Codicon name="cloud-download" {...props} />,
    route: SESSION_IMPORT_ROUTE
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
  /** Restore an archived row — only ever exercised by the Archived filter's
   *  rows (see SidebarSessionsSection's `archivedMode`). */
  onUnarchiveSession: (sessionId: string) => void
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
  currentView: routeView,
  onNavigate,
  onLoadMoreSessions,
  onLoadMoreMessaging,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onUnarchiveSession,
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
  // Following a focused tile: its pane is a chat regardless of the ROUTE view,
  // so the nav highlight and `activeSidebarSessionId` track the tile.
  const focusedSessionIsTile = useStore($focusedSessionIsTile)
  const currentView = focusedSessionIsTile ? 'chat' : routeView
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

  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  // Profile scope = the "workspace switcher" context. Concrete scope shows only
  // that profile's sessions (clean rows, no per-row tags); ALL fans every
  // profile in. Grouped rendering stays gated on `showAllProfiles` (multi-profile
  // + ALL) so a single-profile user is never stranded in a grouped view with no
  // rail — but the *data* still has to fan in when the persisted scope is ALL
  // (Grouping → Profile). Filtering that pool against the `__all__` sentinel
  // matches nothing and empties recents + pins.
  // Archived rows are excluded from the sessions query, so Archived is a view of
  // its own set rather than a filter over this one — a flat list of archived
  // rows, no project tree, no date or status dividers.
  const scopedSessions = useMemo(() => {
    const pool = showArchived ? archivedSessions : sessions

    return filterSessionsByProfileScope(pool, profileScope)
  }, [sessions, archivedSessions, showArchived, profileScope])

  // One predicate for the status/project filters, so the flat list and the
  // project lanes narrow by the same rule. A project lane holds rows the loaded
  // page may not, so it has to be answerable per session rather than by
  // membership in the filtered set. Detached rows file under the Home bucket id
  // (same rule as the overview overlay), so filtering to Home keeps Home's rows.
  const sessionMatchesFilters = useCallback(
    (session: SessionInfo) => {
      if (statusFilter.length && !statusFilter.includes(sessionStatusBucket(dotStates[session.id]))) {
        return false
      }

      // Narrowing to a few of the profiles on screen. Scoped to one profile the
      // list is already that profile's, so a stale selection can't blank it.
      if (showAllProfiles && profileFilter.length && !profileFilter.includes(normalizeProfileKey(session.profile))) {
        return false
      }

      if (prFilter.length) {
        const key = sessionPrKey(session)

        if (!prFilter.includes(pullRequestBucket(key ? pullRequests[key] : undefined))) {
          return false
        }
      }

      // Same membership the sidebar groups and colors by, so a filtered row
      // lands in the lane the user picked it from.
      return sessionMatchesProjectFilter(session, projectFilter, projects)
    },
    [statusFilter, projectFilter, profileFilter, showAllProfiles, prFilter, pullRequests, projects, dotStates]
  )

  const filtersNarrow =
    statusFilter.length > 0 ||
    projectFilter.length > 0 ||
    prFilter.length > 0 ||
    (showAllProfiles && profileFilter.length > 0)

  const visibleSessions = useMemo(
    () => (filtersNarrow ? scopedSessions.filter(sessionMatchesFilters) : scopedSessions),
    [scopedSessions, filtersNarrow, sessionMatchesFilters]
  )

  // Recents by activity (last_active || started_at). User send stamps
  // last_active immediately. Ordering by status doesn't sort here — it re-slots
  // rows *inside* whatever dividers are on, via sortOrderIds below — so the
  // date buckets stay chronological either way.
  const sortedSessions = useMemo(
    () => [...visibleSessions].sort((a, b) => sessionTime(b) - sessionTime(a)),
    [visibleSessions]
  )

  const visibleCronSessions = useMemo(
    () => filterSessionsByProfileScope(cronSessions, profileScope),
    [cronSessions, profileScope]
  )

  const visibleMessagingSessions = useMemo(
    () => filterSessionsByProfileScope(messagingSessions, profileScope),
    [messagingSessions, profileScope]
  )

  // Index sessions by every id a pin might be stored under — recents, cron,
  // AND messaging, since all three can be pinned (see session-index.ts).
  const sessionByAnyId = useMemo(
    () => buildSessionByAnyId(visibleSessions, visibleCronSessions, visibleMessagingSessions),
    [visibleSessions, visibleCronSessions, visibleMessagingSessions]
  )

  // Local pin ids first (hand-picked order), then server-flagged pins the
  // local set doesn't know about — a backend `pinned=1` row must never be
  // invisible just because localStorage is cold or was clobbered (#85969) —
  // minus the rows whose flag our own in-flight pin write already contradicts.
  const pinnedSessions = useMemo(
    () =>
      resolvePinnedSessions(
        pinnedSessionIds,
        sessionByAnyId,
        [...visibleSessions, ...cronSessions, ...messagingSessions],
        unconfirmedPinWrites
      ),
    [pinnedSessionIds, sessionByAnyId, visibleSessions, cronSessions, messagingSessions, unconfirmedPinWrites]
  )

  // Every id a pin is reachable under: the raw stored ids, plus BOTH identities
  // of each session we resolved one to. A pin is stored on the durable lineage
  // root, but the lists that must filter it out are fed from three independent
  // fetches (recents, the messaging slice, the backend project tree) and each
  // can surface the same conversation under either its live tip or its root.
  // Comparing one identity against the other is how a pinned session ended up
  // rendered twice — once in Pinned, once in its project group.
  const pinnedIdentitySet = useMemo(() => {
    const ids = new Set(pinnedSessionIds)

    for (const session of pinnedSessions) {
      ids.add(session.id)

      if (session._lineage_root_id) {
        ids.add(session._lineage_root_id)
      }
    }

    return ids
  }, [pinnedSessionIds, pinnedSessions])

  // A pinned session belongs to the Pinned section and nowhere else, so every
  // other list filters it out. Match on either identity the row carries — a
  // backend snapshot can surface either side of a compression tip rotation.
  const isPinnedSession = useCallback(
    (session: SessionInfo) =>
      pinnedIdentitySet.has(session.id) ||
      (session._lineage_root_id != null && pinnedIdentitySet.has(session._lineage_root_id)),
    [pinnedIdentitySet]
  )

  // What the project tree drops: pins (they live in their own section) plus
  // anything the active filters exclude, so filtering works the same whether
  // you're looking at the flat list or the lanes.
  const isHiddenFromProjects = useCallback(
    (session: SessionInfo) => isPinnedSession(session) || (filtersNarrow && !sessionMatchesFilters(session)),
    [isPinnedSession, filtersNarrow, sessionMatchesFilters]
  )

  // Full-text search across *all* sessions (not just the loaded page) so 699
  // sessions stay findable. Debounced; loaded sessions are matched instantly
  // client-side and merged ahead of the server hits.
  useEffect(() => {
    if (!trimmedQuery) {
      setServerMatches([])
      setSearchPending(false)

      return
    }

    let cancelled = false

    setSearchPending(true)

    const id = window.setTimeout(() => {
      void searchSessions(trimmedQuery)
        .then(res => {
          if (!cancelled) {
            setServerMatches(res.results)
          }
        })
        .catch(() => undefined)
        .finally(() => {
          if (!cancelled) {
            setSearchPending(false)
          }
        })
    }, 200)

    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [trimmedQuery])

  const searchResults = useMemo(() => {
    if (!trimmedQuery) {
      return []
    }

    const out = new Map<string, SessionInfo>()

    for (const s of sortedSessions) {
      if (sessionMatchesSearch(s, trimmedQuery)) {
        out.set(s.id, s)
      }
    }

    for (const match of serverMatches) {
      if (out.has(match.session_id)) {
        continue
      }

      const loaded = sessionByAnyId.get(match.session_id)
      out.set(match.session_id, loaded ?? searchResultToSession(match))
    }

    return [...out.values()]
  }, [trimmedQuery, sortedSessions, serverMatches, sessionByAnyId])

  const unpinnedAgentSessions = useMemo(
    () => sortedSessions.filter(s => !isPinnedSession(s)),
    [sortedSessions, isPinnedSession]
  )

  useEffect(() => {
    const next = resolveManualSessionOrderIds(
      unpinnedAgentSessions.map(s => s.id),
      agentOrderIds,
      agentOrderManual
    )

    if (!next.length && agentOrderManual) {
      setSidebarSessionOrderManual(false)
    }

    if (!next.length && agentOrderIds.length) {
      setSidebarSessionOrderIds([])

      return
    }

    if (next.length && !sameIds(next, agentOrderIds)) {
      setSidebarSessionOrderIds(next)
    }
  }, [agentOrderIds, agentOrderManual, unpinnedAgentSessions])

  // Recents render in recency order. The hand-picked order is layered on per
  // date group inside the section (orderRowsWithinGroups) rather than baked
  // into the list here, so a drag ranks a row among its own day's chats
  // instead of flattening the whole sidebar into an undated manual mode.
  const agentSessions = unpinnedAgentSessions

  // Recents are local-only: messaging-platform sessions are fetched as their
  // own slice ($messagingSessions) and rendered in self-managed per-platform
  // sections below, so there is no source-grouping magic to untangle here.
  //
  // Workspace grouping is a `project -> repo -> lane -> sessions` tree computed
  // authoritatively on the backend (projects.tree). Parents reorder via
  // workspaceParentOrderIds; worktrees within a parent via workspaceOrderIds.
  const worktreeGroupingActive = agentsGrouped && !showArchived
  const gatewayReady = gatewayState === 'open'
  const showAllSessions = useStore($sidebarShowAllSessions)

  // The backend project tree is a structural snapshot, NOT a per-message feed.
  // Refresh it on structural edges only — entering the grouped view, a profile
  // switch, gateway (re)connect — plus the once-per-run disk scan. Live session
  // changes between refreshes are reflected by the in-memory overlay
  // (overlayLiveLanes / overlayLivePreviews) off `$sessions`, so a turn
  // completing does NOT re-run the heavy list_sessions_rich scan. Project
  // mutations refresh the tree from their own store actions.
  useEffect(() => {
    if (!gatewayReady) {
      return
    }

    if (worktreeGroupingActive) {
      void refreshProjects()

      // The all-profiles tree is served off every profile's databases at once
      // and deliberately leaves discovery out — a repo with no sessions is the
      // same repo in every profile, so scanning here would multiply empty lanes
      // by the profile count and write the result into profiles the user isn't
      // driving.
      if (showAllProfiles) {
        void refreshProjectTree()

        return
      }

      // Paint the list from the fast tree fetch (explicit projects + repos from
      // existing sessions / the backend cache) FIRST, then kick off the heavy
      // home-dir git crawl so newly-discovered repos fold in afterward — instead
      // of the crawl blocking the first render.
      void refreshProjectTree().finally(() => void scanAndRecordRepos())

      return
    }

    // Flat view: warm the tree in the background anyway. Fetching it only on
    // the switch meant the first switch of every run paid for the whole round
    // trip behind a skeleton, and the menu's Project filter had nothing to
    // list until you'd visited the grouped view at least once.
    const warm = window.setTimeout(() => void refreshProjectTree(), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [activeConnectionId, worktreeGroupingActive, showAllProfiles, profileScope, gatewayReady])

  // Widen the existing tree query when the user expands previews, without
  // repeating repo discovery. Initial load/scope changes use the effect above.
  useEffect(
    () =>
      $sidebarShowAllSessions.listen(() => {
        if (gatewayReady && worktreeGroupingActive) {
          void refreshProjectTree()
        }
      }),
    [gatewayReady, worktreeGroupingActive]
  )

  // Sessions the branch join can't answer for get one look at their own
  // transcript — a `gh pr create` in there names the PR outright. Backfills
  // whatever is loaded, whether or not the badge is on: gating it on the badge
  // meant switching PR on showed a half-empty list until a second pass caught
  // up. One request per batch of never-scanned rows, and the scanned set makes
  // that batch empty from the second pass on, so this settles to nothing.
  useEffect(() => {
    if (!gatewayReady) {
      return
    }

    const warm = window.setTimeout(() => void recoverSessionPullRequests(scopedSessions), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [gatewayReady, scopedSessions])

  // PR state is only fetched for someone who asked to see it — the badge or the
  // filter — and it asks about the branches on screen, so the answer can't be
  // crowded out by a busy repo's newer PRs.
  const prLookupsByRepo = useMemo(() => {
    if (!prDataWanted) {
      return {}
    }

    const byRepo: Record<string, string[]> = {}

    for (const session of scopedSessions) {
      // The row's own key, so a session bound to a branch (or a PR number) it
      // was stamped with asks about THAT, not the branch it started on.
      const [root, lookup] = sessionPrKey(session)?.split('\n') ?? []

      if (root && lookup && !byRepo[root]?.includes(lookup)) {
        byRepo[root] = [...(byRepo[root] ?? []), lookup]
      }
    }

    return byRepo
    // prBranchOverrides is what `sessionPrKey` reads through — a recovered PR
    // has to re-ask with the key it just learned.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prDataWanted, scopedSessions, prBranchOverrides])

  // A stable identity for "the same question as last time", so a re-render that
  // rebuilds the map doesn't re-ask GitHub.
  const prQueryKey = JSON.stringify(
    Object.entries(prLookupsByRepo)
      .map(([root, lookups]) => [root, [...lookups].sort()] as const)
      .sort(([a], [b]) => a.localeCompare(b))
  )

  useEffect(() => {
    if (prQueryKey === '[]') {
      return
    }

    const byRepo = Object.fromEntries(JSON.parse(prQueryKey) as [string, string[]][])

    void refreshPullRequests(byRepo)

    // A PR opens, merges or gets closed on github.com, not in here — so like
    // the project tree, re-pull when the window comes back. The store's own
    // staleness window keeps a flurry of focus events to one call per repo.
    const onActive = () => {
      if (document.visibilityState !== 'hidden') {
        void refreshPullRequests(byRepo)
      }
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [prQueryKey])

  // Out-of-band repo changes (a `git init` / `rm -rf` in another terminal) emit
  // no git events, so — like every git GUI — re-pull on window focus / tab
  // visibility instead of stranding the tree until a hard reload. The tree
  // fetch is cheap and runs every focus (picks up explicit create/delete +
  // session regrouping); the heavy disk crawl that surfaces brand-new repos is
  // throttled. Agent-driven changes already refresh via $workspaceChangeTick.
  useEffect(() => {
    if (!worktreeGroupingActive || !gatewayReady) {
      return
    }

    let lastScanAt = 0
    const SCAN_THROTTLE_MS = 30_000

    const onActive = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      void refreshProjects()
      void refreshProjectTree()

      // Discovery stays off while browsing every profile, for the reason the
      // first fetch leaves it out.
      if (showAllProfiles) {
        return
      }

      const now = Date.now()

      if (now - lastScanAt >= SCAN_THROTTLE_MS) {
        lastScanAt = now
        void scanAndRecordRepos(true)
      }
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [worktreeGroupingActive, showAllProfiles, gatewayReady])

  // Apply the persisted repo + worktree orders to a project's repo subtrees.
  const orderRepos = useCallback(
    (repos: SidebarWorkspaceTree[]): SidebarWorkspaceTree[] =>
      orderByIds(repos, parent => parent.id, workspaceParentOrderIds).map(parent => ({
        ...parent,
        groups: orderByIds(parent.groups, group => group.id, workspaceOrderIds)
      })),
    [workspaceParentOrderIds, workspaceOrderIds]
  )

  // ── Projects: the single top-level model (authoritative, from the backend) ──
  // `projects.tree` already unifies explicit projects + auto repos and folds
  // linked worktrees under their main repo. The desktop only layers local view
  // state on top: dismissed auto-projects, persisted repo/lane order, and the
  // overview sort. Membership is the backend tree's — never re-derived here.
  const projectModel = useMemo<SidebarProjectTree[]>(() => {
    const sorted = sortProjectsForOverview(
      filterVisibleProjects(projectTree, dismissedAutoProjects)
        // A filtered-out project drops its whole lane, header included — hiding
        // only its rows would leave a row of empty folders behind.
        .filter(project => !projectFilter.length || projectFilter.includes(project.id))
        .map(project =>
          excludeProjectSessions(
            {
              ...project,
              // Home is synthetic, so its name is ours to translate — every
              // other label is a repo basename or a name the user typed.
              label: project.isNoProject ? s.projects.home : project.label,
              repos: orderRepos(project.repos)
            },
            isHiddenFromProjects
          )
        ),
      activeProjectId
    )

    // Layer the user's manual drag-order on top of the deterministic sort. Empty
    // (default) returns `sorted` untouched; projects the user hasn't ordered yet
    // keep their sorted position rather than jumping the hand-picked list.
    return orderProjectsByIds(sorted, projectOrderIds)
  }, [
    projectTree,
    dismissedAutoProjects,
    orderRepos,
    activeProjectId,
    projectFilter,
    projectOrderIds,
    isHiddenFromProjects,
    s
  ])

  // The overview only renders in grouped mode; the model stays live regardless
  // so scoping is consistent across views.
  const agentProjectTree = worktreeGroupingActive ? projectModel : undefined

  // ── Project switcher (drill-in) ────────────────────────────────────────────
  // Grouped, single-profile view is a project switcher: ALL_PROJECTS shows the
  // overview (a list you click into); a concrete scope means you've "entered" a
  // project, so the Sessions list shows ONLY that project's worktrees/sessions.
  const projectsActive = Boolean(agentProjectTree?.length)

  // The overview node for the entered project (structure + counts, empty lanes).
  const overviewEnteredProject =
    projectsActive && projectScope !== ALL_PROJECTS
      ? agentProjectTree?.find(node => node.id === projectScope)
      : undefined

  const inProject = Boolean(overviewEnteredProject)
  const enteredProjectId = overviewEnteredProject?.id

  // Entering a project lazily hydrates its full lanes (repo -> lane -> sessions)
  // from the backend — same grouping/ids as the overview, just with rows.
  const {
    project: enteredProjectTree,
    failed: projectLoadFailed,
    loading: projectLoading,
    retry: retryProject
  } = useEnteredProjectSessions(enteredProjectId, gatewayReady, projectTree, `${activeConnectionId}:${profileScope}`)

  // Prefer the hydrated tree; fall back to the overview node (empty lanes) while
  // the drill-in fetch is in flight, so the header/structure render immediately.
  const enteredProject = useMemo<SidebarProjectTree | undefined>(() => {
    if (!overviewEnteredProject) {
      return undefined
    }

    const hydrated =
      enteredProjectTree && enteredProjectTree.id === overviewEnteredProject.id
        ? enteredProjectTree
        : overviewEnteredProject

    // The live-session overlay (creates/evictions) is applied per-repo in
    // RepoFlatSection, AFTER the visual git-worktree lanes are merged in (so
    // out-of-tree worktrees can be placed). Here we just order the snapshot and
    // drop pinned rows — the hydrated lanes come straight from the backend, so
    // they haven't been through projectModel's filter.
    // The label comes from the overview node either way — that's the model's
    // presentation copy (Home is translated there), not the raw payload's.
    return excludeProjectSessions(
      { ...hydrated, label: overviewEnteredProject.label, repos: orderRepos(hydrated.repos) },
      isHiddenFromProjects
    )
  }, [overviewEnteredProject, enteredProjectTree, orderRepos, isHiddenFromProjects])

  const enteredProjectOverlaySessions = useMemo(
    () => reconcileEnteredProjectSessions(agentSessions, overviewEnteredProject?.previewSessions),
    [agentSessions, overviewEnteredProject?.previewSessions]
  )

  // Overlay live `$sessions` onto the entered project so a just-created session
  // (which the backend snapshot hasn't folded in yet) counts as content and
  // renders immediately. Also carry over the overview's current preview rows:
  // its project tree and the separately hydrated drill-in can resolve at
  // different times, but a row visible in the overview must not disappear on
  // entry. The backend seeds each project folder as an (empty) repo, so the
  // overlay always has a lane to place a missing in-project session into.
  const enteredProjectContent = useMemo(
    () =>
      enteredProject ? overlayLiveLanes(enteredProject, enteredProjectOverlaySessions, removedSessionIds) : undefined,
    [enteredProject, enteredProjectOverlaySessions, removedSessionIds]
  )

  const scopedRepoPaths = useMemo(
    () =>
      enteredProject ? enteredProject.repos.map(repo => repo.path).filter((path): path is string => Boolean(path)) : [],
    [enteredProject]
  )

  // git worktree list is a VISUAL-only enhancer (empty lanes); never membership.
  const inEnteredProject = Boolean(enteredProject && !showAllProfiles)
  const [scopedRepoWorktrees] = useRepoWorktreeMap(scopedRepoPaths, inEnteredProject)

  // Re-probe worktree lanes on out-of-band git changes the renderer can't see.
  // A turn can `git worktree add/remove` in the terminal (e.g. you ask Hermes to
  // "remove that worktree"), and the window never blurs during an in-app chat,
  // so nothing would otherwise re-run the visual probe. Re-sync when a working
  // session settles (its turn finished) or the window refocuses (an external
  // terminal may have changed things) — only while a project is entered, and
  // only the cheap per-repo `git worktree list`, never the heavy tree scan.
  //
  // Listened to rather than rendered from: a settling turn is a side effect,
  // and reading it with `useStore` repainted this whole component — every
  // section, every row — on each status edge, to run an effect that touches no
  // markup. The rows subscribe to their own status, so nothing above them needs
  // to re-render for one of them to change color.
  useEffect(() => {
    if (!inEnteredProject) {
      return
    }

    let previous = $workingSessionIds.get()

    return $workingSessionIds.listen(working => {
      // A session leaving the working set means its turn just completed.
      const aTurnSettled = previous.some(id => !working.includes(id))

      previous = working

      if (aTurnSettled) {
        refreshWorktrees()
      }
    })
  }, [inEnteredProject])

  useEffect(() => {
    if (!inEnteredProject) {
      return
    }

    const onFocus = () => refreshWorktrees()
    window.addEventListener('focus', onFocus)

    return () => window.removeEventListener('focus', onFocus)
  }, [inEnteredProject])

  const lastProjectCwdSyncRef = useRef<null | string>(null)

  const syncProjectCwd = useCallback(
    (project: SidebarProjectTree) => {
      const target = projectTreeCwd(project)

      if (target && target !== currentCwd) {
        setCurrentCwd(target)
      }
    },
    [currentCwd]
  )

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (!inProject || !enteredProject) {
      lastProjectCwdSyncRef.current = null

      return
    }

    if (lastProjectCwdSyncRef.current === enteredProject.id) {
      return
    }

    syncProjectCwd(enteredProject)
    lastProjectCwdSyncRef.current = enteredProject.id
  }, [inProject, enteredProject, syncProjectCwd])

  // A persisted scope can go stale (project archived/removed, or a profile
  // switch swapped the whole catalog). Once projects have loaded, drop back to
  // the overview if the scoped id is gone.
  useEffect(() => {
    if (projectScope !== ALL_PROJECTS && projectsActive && !enteredProject) {
      exitProjectScope()
    }
  }, [projectScope, projectsActive, enteredProject])

  // The project overview (drill-in list) vs. the entered project's content.
  const projectOverview = projectsActive && !inProject ? agentProjectTree : undefined

  // Preview rows come from the backend tree (each project carries its
  // most-recent sessions), overlaid with live $sessions so a just-created
  // session shows under its project instantly (and with its working arc),
  // matching the flat Recents list. Keyed by project id for the rows.
  const overviewPreviews = useMemo<Record<string, SessionInfo[]>>(
    () =>
      overlayLivePreviews(
        projectOverview ?? [],
        agentSessions,
        projects,
        showAllSessions ? Infinity : PROJECT_PREVIEW_COUNT,
        {
          removed: removedSessionIds,
          // Rank before the trim, so "3 priciest in this project" isn't "3 most
          // recent, priciest first".
          rankIds: sortOrderIds
        }
      ),
    [projectOverview, agentSessions, projects, removedSessionIds, sortOrderIds, showAllSessions]
  )

  const onEnterProject = useCallback(
    (id: string) => {
      const project = projectModel.find(node => node.id === id)

      if (project) {
        syncProjectCwd(project)
      }

      enterProject(id)
    },
    [projectModel, syncProjectCwd]
  )

  // The Sessions section is a project switcher in grouped mode: its label reads
  // "Sessions" when flat, "Projects" at the overview, and the project's name
  // once you've entered one.
  const sessionsLabel =
    inProject && enteredProject ? enteredProject.label : worktreeGroupingActive ? s.projects.sectionLabel : s.sessions

  // Mirror the section's skeleton gate (projectsLoading + nothing to show yet):
  // while the skeleton is up there's no point also spinning the header count.
  const projectsSkeletonVisible =
    worktreeGroupingActive &&
    projectTreeLoading &&
    !projectOverview?.length &&
    !(inProject && (enteredProject?.sessionCount ?? 0) > 0)

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
                  (item.id === 'session-import' && currentView === 'session-import') ||
                  // Contributed rows light up at their own route.
                  (currentView === 'extension' && Boolean(item.route) && pathname === item.route)

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
                  onUnarchiveSession={onUnarchiveSession}
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
