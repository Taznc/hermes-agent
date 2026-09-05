import { KeyboardSensor, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
import { sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { resolveProfileColor } from '@/lib/profile-color'
import { useStoreSelector } from '@/lib/use-session-slice'
import { cn } from '@/lib/utils'
import { $activeConnectionId } from '@/store/connections'
import {
  $sidebarCardRows,
  $sidebarGrouping,
  $sidebarListLimit,
  $sidebarOrdering,
  $sidebarProjectOrderIds,
  $sidebarRecentsOpen,
  $sidebarSessionOrderIds,
  $sidebarSessionOrderManual,
  $sidebarShowArchived,
  $sidebarWorkspaceOrderIds,
  $sidebarWorkspaceParentOrderIds,
  pinSession,
  setSidebarProjectOrderIds,
  setSidebarRecentsOpen,
  setSidebarSessionOrderIds,
  setSidebarSessionOrderManual,
  setSidebarWorkspaceOrderIds,
  setSidebarWorkspaceParentOrderIds,
  SIDEBAR_SESSIONS_PAGE_SIZE
} from '@/store/layout'
import { $sidebarPrDataWanted } from '@/store/layout'
import { $sidebarFiltersActive } from '@/store/layout'
import { $profileColors, $profileScope, normalizeProfileKey } from '@/store/profile'
import {
  $activeProjectId,
  $newProjectDropPlacement,
  $projects,
  $projectScope,
  $projectTree,
  $projectTreeLoading,
  $reposScanning,
  ALL_PROJECTS,
  enterProject,
  exitProjectScope,
  fetchProjectSessions,
  openProjectCreate,
  refreshProjects,
  refreshProjectTree,
  refreshWorktrees,
  scanAndRecordRepos
} from '@/store/projects'
import { $prBranchBySession, recoverSessionPullRequests, refreshPullRequests, sessionPrKey } from '@/store/pull-requests'
import {
  $currentCwd,
  $gatewayState,
  $sessionProfilesTruncated,
  $sessions,
  $sessionsLoading,
  $unreadFinishedSessionIds,
  markAllSessionsRead,
  setCurrentCwd
} from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { $workingSessionIds } from '@/store/session-states'
import { ackAllSessionsRead } from '@/store/session-unread'
import {
  $sidebarAllProfilesActive,
  $sidebarIsHiddenFromProjects,
  $sidebarProjectModel,
  $sidebarScopedSessions,
  $sidebarUnpinnedAgentSessions,
  $sidebarWorktreeGroupingActive
} from '@/store/sidebar-model'
import { $sidebarSessionRankIds } from '@/store/sidebar-sort'
import type { SessionInfo } from '@/types/hermes'

import type { NewSessionSplitHandler } from '../new-session-drag'

import { SIDEBAR_COMPACT_FLAT, SIDEBAR_SCROLL_Y, SidebarSectionAddButton } from './chrome'
import { SidebarLoadMoreRow } from './load-more-row'
import { orderByIds, reconcileOrderIds, resolveManualSessionOrderIds, sameIds } from './order'
import {
  excludeProjectSessions,
  overlayLiveLanes,
  overlayLivePreviews,
  ProjectBackRow,
  ProjectMenu,
  projectTreeCwd,
  reconcileEnteredProjectSessions,
  type SidebarProjectTree,
  type SidebarSessionGroup,
  StartWorkButton,
  useRepoWorktreeMap
} from './projects'
import { SidebarSessionSkeletons } from './section-states'
import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'

// How long after connecting to warm the project tree for someone who isn't in
// the grouped view. Long enough that the flat list — the thing actually on
// screen — has the connection to itself first.
const PROJECT_TREE_WARM_MS = 2_000

interface SidebarWorkspaceSectionProps {
  activeSessionId: null | string
  onResumeSession: (sessionId: string, session?: SessionInfo) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onBranchSession: (sessionId: string) => void
  onNewSessionInWorkspace: (path: null | string) => void
  /** See ChatSidebarProps.onNewSessionSplit — the new-session drag target. */
  onNewSessionSplit: NewSessionSplitHandler
  onLoadMoreSessions: () => Promise<void> | void
  onToggleUnread: (sessionId: string) => void
}

/**
 * Recents + Projects: the sidebar's project switcher / flat session list.
 * Owns the whole `scopedSessions -> ... -> projectModel` chain (via
 * store/sidebar-model.ts) plus every effect that only exists to feed this
 * one section (project tree refresh, worktree list refresh, cwd sync, PR
 * lookups). Extracted so a session/project/PR tick that only this section
 * paints differently for no longer re-renders Pinned, Messaging, or Cron.
 */
export function SidebarWorkspaceSection({
  activeSessionId,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onBranchSession,
  onNewSessionInWorkspace,
  onNewSessionSplit,
  onLoadMoreSessions,
  onToggleUnread
}: SidebarWorkspaceSectionProps) {
  const { t } = useI18n()
  const s = t.sidebar

  const grouping = useStore($sidebarGrouping)
  const ordering = useStore($sidebarOrdering)
  const cardRows = useStore($sidebarCardRows)
  const listLimit = useStore($sidebarListLimit)
  const showArchived = useStore($sidebarShowArchived)
  const agentsOpen = useStore($sidebarRecentsOpen)
  const filtersActive = useStore($sidebarFiltersActive)

  const scopedSessions = useStore($sidebarScopedSessions)
  const agentSessions = useStore($sidebarUnpinnedAgentSessions)
  const projectModel = useStore($sidebarProjectModel)
  const showAllProfiles = useStore($sidebarAllProfilesActive)

  const profileScope = useStore($profileScope)
  const activeConnectionId = useStore($activeConnectionId)

  const agentOrderIds = useStore($sidebarSessionOrderIds)
  const agentOrderManual = useStore($sidebarSessionOrderManual)
  const workspaceOrderIds = useStore($sidebarWorkspaceOrderIds)
  const workspaceParentOrderIds = useStore($sidebarWorkspaceParentOrderIds)
  const projectOrderIds = useStore($sidebarProjectOrderIds)
  const projects = useStore($projects)
  const projectTree = useStore($projectTree)
  const projectTreeLoading = useStore($projectTreeLoading)
  const removedSessionIds = useStore($removedSessionIds)
  const reposScanning = useStore($reposScanning)
  const activeProjectId = useStore($activeProjectId)
  const projectScope = useStore($projectScope)
  const gatewayState = useStore($gatewayState)
  const sessionsLoading = useStore($sessionsLoading)
  const sessionProfilesTruncated = useStore($sessionProfilesTruncated)
  const sortOrderIds = useStore($sidebarSessionRankIds)
  const prDataWanted = useStore($sidebarPrDataWanted)
  const prBranchOverrides = useStore($prBranchBySession)
  // Membership only, via a scalar selector: the header's "mark all read"
  // button needs presence, not the array itself.
  const hasUnread = useStoreSelector($unreadFinishedSessionIds, ids => ids.length > 0)

  const [recentsLoadMorePending, setRecentsLoadMorePending] = useState(false)

  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const worktreeGroupingActive = useStore($sidebarWorktreeGroupingActive)
  const gatewayReady = gatewayState === 'open'

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

      if (showAllProfiles) {
        void refreshProjectTree()

        return
      }

      void refreshProjectTree().finally(() => void scanAndRecordRepos())

      return
    }

    const warm = window.setTimeout(() => void refreshProjectTree(), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [activeConnectionId, worktreeGroupingActive, showAllProfiles, profileScope, gatewayReady])

  // Sessions the branch join can't answer for get one look at their own
  // transcript — a `gh pr create` in there names the PR outright.
  useEffect(() => {
    if (!gatewayReady) {
      return
    }

    const warm = window.setTimeout(() => void recoverSessionPullRequests(scopedSessions), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [gatewayReady, scopedSessions])

  // PR state is only fetched for someone who asked to see it — the badge or
  // the filter — and it asks about the branches on screen.
  const prLookupsByRepo = useMemo(() => {
    if (!prDataWanted) {
      return {}
    }

    const byRepo: Record<string, string[]> = {}

    for (const session of scopedSessions) {
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

  const prQueryKey = useMemo(
    () =>
      JSON.stringify(
        Object.entries(prLookupsByRepo)
          .map(([root, lookups]) => [root, [...lookups].sort()] as const)
          .sort(([a], [b]) => a.localeCompare(b))
      ),
    [prLookupsByRepo]
  )

  useEffect(() => {
    if (prQueryKey === '[]') {
      return
    }

    const byRepo = Object.fromEntries(JSON.parse(prQueryKey) as [string, string[]][])

    void refreshPullRequests(byRepo)

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

  // Out-of-band repo changes re-pull on window focus / tab visibility.
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

  useEffect(() => {
    const next = resolveManualSessionOrderIds(
      agentSessions.map(sess => sess.id),
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
  }, [agentOrderIds, agentOrderManual, agentSessions])

  const orderRepos = useCallback(
    (repos: SidebarProjectTree['repos']): SidebarProjectTree['repos'] =>
      orderByIds(repos, parent => parent.id, workspaceParentOrderIds).map(parent => ({
        ...parent,
        groups: orderByIds(parent.groups, group => group.id, workspaceOrderIds)
      })),
    [workspaceParentOrderIds, workspaceOrderIds]
  )

  const agentProjectTree = worktreeGroupingActive ? projectModel : undefined
  const projectsActive = Boolean(agentProjectTree?.length)

  const overviewEnteredProject =
    projectsActive && projectScope !== ALL_PROJECTS
      ? agentProjectTree?.find(node => node.id === projectScope)
      : undefined

  const inProject = Boolean(overviewEnteredProject)
  const enteredProjectId = overviewEnteredProject?.id

  const [enteredProjectTree, setEnteredProjectTree] = useState<SidebarProjectTree | null>(null)

  useEffect(() => {
    if (!enteredProjectId || !gatewayReady) {
      setEnteredProjectTree(null)

      return
    }

    let cancelled = false

    void fetchProjectSessions(enteredProjectId).then(project => {
      if (!cancelled) {
        setEnteredProjectTree(project)
      }
    })

    return () => {
      cancelled = true
    }
  }, [enteredProjectId, gatewayReady, projectTree])

  const isHiddenFromProjects = useStore($sidebarIsHiddenFromProjects)

  const enteredProject = useMemo<SidebarProjectTree | undefined>(() => {
    if (!overviewEnteredProject) {
      return undefined
    }

    const hydrated =
      enteredProjectTree && enteredProjectTree.id === overviewEnteredProject.id ? enteredProjectTree : overviewEnteredProject

    return excludeProjectSessions(
      { ...hydrated, label: overviewEnteredProject.label, repos: orderRepos(hydrated.repos) },
      isHiddenFromProjects
    )
  }, [overviewEnteredProject, enteredProjectTree, orderRepos, isHiddenFromProjects])

  const scopedRepoPaths = useMemo(
    () =>
      enteredProject ? enteredProject.repos.map(repo => repo.path).filter((path): path is string => Boolean(path)) : [],
    [enteredProject]
  )

  const inEnteredProject = Boolean(enteredProject && !showAllProfiles)
  const [scopedRepoWorktrees] = useRepoWorktreeMap(scopedRepoPaths, inEnteredProject)

  useEffect(() => {
    if (!inEnteredProject) {
      return
    }

    let previous = $workingSessionIds.get()

    return $workingSessionIds.listen(working => {
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

  const syncProjectCwd = useCallback((project: SidebarProjectTree) => {
    const target = projectTreeCwd(project)

    if (target && target !== $currentCwd.get()) {
      setCurrentCwd(target)
    }
  }, [])

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

  useEffect(() => {
    if (projectScope !== ALL_PROJECTS && projectsActive && !enteredProject) {
      exitProjectScope()
    }
  }, [projectScope, projectsActive, enteredProject])

  const projectOverview = projectsActive && !inProject ? agentProjectTree : undefined

  const overviewPreviews = useMemo<Record<string, SessionInfo[]>>(
    () =>
      overlayLivePreviews(projectOverview ?? [], agentSessions, projects, listLimit === 'all' ? Infinity : listLimit, {
        removed: removedSessionIds,
        rankIds: sortOrderIds
      }),
    [projectOverview, agentSessions, projects, removedSessionIds, sortOrderIds, listLimit]
  )

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

  const sessionsLabel =
    inProject && enteredProject ? enteredProject.label : worktreeGroupingActive ? s.projects.sectionLabel : s.sessions

  const projectsSkeletonVisible =
    worktreeGroupingActive &&
    projectTreeLoading &&
    !projectOverview?.length &&
    !(inProject && (enteredProject?.sessionCount ?? 0) > 0)

  const displayRecentsCountRef = useRef(0)
  const loadedRecentsCountRef = useRef(0)
  const allSessions = useStore($sessions)
  const loadedSessionCount = showAllProfiles ? allSessions.length : scopedSessions.length
  displayRecentsCountRef.current = agentSessions.length
  loadedRecentsCountRef.current = loadedSessionCount

  const hasMoreSessions =
    !showArchived &&
    (showAllProfiles
      ? Object.values(sessionProfilesTruncated).some(Boolean)
      : Boolean(sessionProfilesTruncated[profileScope]))

  const onLoadMoreRecents = useCallback(async () => {
    if (recentsLoadMorePending) {
      return
    }

    setRecentsLoadMorePending(true)

    try {
      const startVisible = displayRecentsCountRef.current
      const targetVisible = startVisible + SIDEBAR_SESSIONS_PAGE_SIZE
      let lastLoaded = loadedRecentsCountRef.current

      for (let attempt = 0; attempt < 6; attempt += 1) {
        await Promise.resolve(onLoadMoreSessions())
        await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve()))

        const visibleNow = displayRecentsCountRef.current
        const loadedNow = loadedRecentsCountRef.current

        if (visibleNow >= targetVisible) {
          break
        }

        if (loadedNow <= lastLoaded) {
          break
        }

        lastLoaded = loadedNow
      }
    } finally {
      setRecentsLoadMorePending(false)
    }
  }, [onLoadMoreSessions, recentsLoadMorePending])

  const rankedGlobally = ordering === 'cost' || ordering === 'tokens'
  const profileGrouped = showAllProfiles && grouping === 'profile'

  const profileColors = useStore($profileColors)

  const profileGroups = useMemo<SidebarSessionGroup[] | undefined>(() => {
    if (!profileGrouped) {
      return undefined
    }

    const groups = new Map<string, SidebarSessionGroup>()

    for (const session of agentSessions) {
      const key = normalizeProfileKey(session.profile)

      const group = groups.get(key) ?? {
        color: resolveProfileColor(key, profileColors),
        id: key,
        label: key,
        mode: 'profile' as const,
        path: null,
        sessions: []
      }

      group.sessions.push(session)

      groups.set(key, group)
    }

    return [...groups.values()].sort((a, b) =>
      a.id === 'default' ? -1 : b.id === 'default' ? 1 : a.label.localeCompare(b.label)
    )
  }, [profileGrouped, agentSessions, profileColors])

  const displayAgentSessions = agentSessions
  const displayAgentGroups = profileGroups

  const activeRepoTrees = useMemo<SidebarProjectTree['repos']>(
    () => (agentProjectTree ? agentProjectTree.flatMap(project => project.repos) : []),
    [agentProjectTree]
  )

  const recentsVirtualizes =
    !displayAgentGroups?.length &&
    !projectOverview?.length &&
    !(inProject && enteredProjectContent) &&
    displayAgentSessions.length >= VIRTUALIZE_THRESHOLD

  useEffect(() => {
    if (!activeRepoTrees.length) {
      return
    }

    const nextParents = reconcileOrderIds(
      activeRepoTrees.map(parent => parent.id),
      workspaceParentOrderIds
    )

    if (!sameIds(nextParents, workspaceParentOrderIds)) {
      setSidebarWorkspaceParentOrderIds(nextParents)
    }

    const nextWorktrees = reconcileOrderIds(
      activeRepoTrees.flatMap(parent => parent.groups.map(group => group.id)),
      workspaceOrderIds
    )

    if (!sameIds(nextWorktrees, workspaceOrderIds)) {
      setSidebarWorkspaceOrderIds(nextWorktrees)
    }
  }, [activeRepoTrees, workspaceParentOrderIds, workspaceOrderIds])

  const showSessionSkeletons = sessionsLoading && scopedSessions.length === 0

  const sessionsMode: 'archived' | 'flat' | 'project' | 'projects' = showArchived
    ? 'archived'
    : inProject
      ? 'project'
      : worktreeGroupingActive
        ? 'projects'
        : 'flat'

  const reorderSessions = (ids: string[]) => {
    setSidebarSessionOrderManual(true)
    setSidebarSessionOrderIds(ids)
  }

  const reorderProjects = (ids: string[]) => setSidebarProjectOrderIds(ids)

  return (
    <div
      className={cn('flex min-h-0 flex-1 flex-col pb-1.75', SIDEBAR_SCROLL_Y, '[scrollbar-gutter:stable]')}
      data-sessions-mode={sessionsMode}
      data-sessions-project={inProject ? (enteredProjectId ?? undefined) : undefined}
    >
      <SidebarSessionsSection
        activeProjectId={activeProjectId}
        activeSessionId={activeSessionId}
        card={cardRows}
        collapsible={!inProject}
        contentClassName={cn(
          'flex min-h-0 flex-1 flex-col gap-px pb-1.75',
          SIDEBAR_SCROLL_Y,
          !recentsVirtualizes && SIDEBAR_COMPACT_FLAT
        )}
        dndSensors={dndSensors}
        emptyState={
          showSessionSkeletons ? (
            <SidebarSessionSkeletons />
          ) : (
            <div className="grid min-h-16 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
              {inProject ? s.projectEmpty : filtersActive ? s.noFilterMatches : s.noSessions}
            </div>
          )
        }
        footer={
          !worktreeGroupingActive && !showSessionSkeletons && hasMoreSessions ? (
            <SidebarLoadMoreRow
              loading={sessionsLoading || recentsLoadMorePending}
              onClick={() => void onLoadMoreRecents()}
              step={0}
            />
          ) : null
        }
        forceEmptyState={showSessionSkeletons}
        grouping={showArchived || rankedGlobally ? 'none' : grouping === 'status' ? 'status' : 'date'}
        groups={displayAgentGroups}
        headerAction={
          <div className="flex shrink-0 items-center gap-0.5">
            {hasUnread && (
              <Tip label={s.markAllRead}>
                <Button
                  aria-label={s.markAllRead}
                  className="text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100"
                  onClick={event => {
                    event.stopPropagation()
                    markAllSessionsRead()
                    ackAllSessionsRead()
                  }}
                  size="icon-xs"
                  variant="ghost"
                >
                  <Codicon name="check-all" size="0.75rem" />
                </Button>
              </Tip>
            )}
            {inProject && enteredProject ? (
              <div className="group/workspace flex shrink-0 items-center gap-0.5">
                {enteredProject.path && <StartWorkButton repoPath={enteredProject.path} />}
                {!enteredProject.isNoProject && (
                  <ProjectMenu
                    isActive={enteredProject.id === activeProjectId}
                    onExitScope={exitProjectScope}
                    project={enteredProject}
                    scoped
                  />
                )}
                <div className="grid size-6 place-items-center">
                  <Tip label={s.showProjects}>
                    <Button
                      aria-label={s.showProjects}
                      className="text-(--ui-text-tertiary) opacity-70 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100 focus-visible:opacity-100"
                      onClick={event => {
                        event.stopPropagation()
                        exitProjectScope()
                      }}
                      size="icon-xs"
                      variant="ghost"
                    >
                      <Codicon name="list-unordered" size="0.75rem" />
                    </Button>
                  </Tip>
                </div>
              </div>
            ) : (
              <>
                {/* The flat-list header "+" is a drag source too — the
                    same gesture as the nav's "New session" row: drag
                    it onto a chat zone's tab strip / edge / center to
                    create the session exactly there. Project-overview
                    mode drags its "+" (the "New project" button) with
                    the project-drag variant: a drop opens the SAME
                    project dialog, and the created project starts at
                    the dropped spot. */}
                {!showAllProfiles ? (
                  <SidebarSectionAddButton
                    ariaLabel={worktreeGroupingActive ? s.projects.newButton : s.nav['new-session']}
                    onNewProjectDrag={
                      worktreeGroupingActive
                        ? {
                            // Dragging the "New project" + arms WHERE the
                            // project should start; the dialog flow consumes
                            // it on create (see $newProjectDropPlacement).
                            onArm: placement => $newProjectDropPlacement.set(placement)
                          }
                        : undefined
                    }
                    onNewSessionSplit={worktreeGroupingActive ? undefined : onNewSessionSplit}
                    onPlainClick={() => {
                      if (worktreeGroupingActive) {
                        openProjectCreate()
                      } else {
                        onNewSessionInWorkspace(null)
                      }
                    }}
                  />
                ) : null}
              </>
            )}
          </div>
        }
        label={sessionsLabel}
        labelMeta={
          worktreeGroupingActive ? (
            reposScanning && !projectsSkeletonVisible ? (
              <GlyphSpinner ariaLabel={s.loading} className="text-[0.6875rem] text-(--ui-text-quaternary)" />
            ) : undefined
          ) : undefined
        }
        liveSessions={inProject ? enteredProjectOverlaySessions : undefined}
        manualOrderIds={agentOrderManual ? agentOrderIds : sortOrderIds}
        onArchiveSession={onArchiveSession}
        onBranchSession={onBranchSession}
        onDeleteSession={onDeleteSession}
        onEnterProject={onEnterProject}
        onNewSessionInWorkspace={onNewSessionInWorkspace}
        onNewSessionSplit={onNewSessionSplit}
        onReorderProjects={showAllProfiles ? undefined : reorderProjects}
        onReorderSessions={showAllProfiles ? undefined : reorderSessions}
        onResumeSession={onResumeSession}
        onToggle={() => setSidebarRecentsOpen(!agentsOpen)}
        onTogglePin={pinSession}
        onToggleUnread={onToggleUnread}
        open={agentsOpen}
        pinned={false}
        projectBackRow={inProject ? <ProjectBackRow label={s.projects.back} onClick={exitProjectScope} /> : undefined}
        projectContent={inProject ? enteredProjectContent : undefined}
        projectOverview={projectOverview}
        projectOverviewPreviews={overviewPreviews}
        projectRepoWorktrees={inProject ? scopedRepoWorktrees : undefined}
        projectsLoading={worktreeGroupingActive ? projectTreeLoading : false}
        removedSessionIds={inProject ? removedSessionIds : undefined}
        rootClassName={cn(
          'min-h-32 flex-1 overflow-hidden p-0',
          !recentsVirtualizes && 'compact:min-h-0 compact:flex-none compact:overflow-visible'
        )}
        sessions={displayAgentSessions}
        sortable={!showAllProfiles && agentSessions.length > 1}
      />
    </div>
  )
}
