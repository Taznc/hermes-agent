/**
 * SIDEBAR SESSION MODEL — the shared derivation chain every sidebar section
 * (Pinned, Recents/Projects, Messaging, Search) reads from, instead of each
 * subscribing to the raw session/project/pr stores and re-deriving its own
 * copy of scope + filtering + pin resolution.
 *
 * Why this exists: `sidebar/index.tsx` used to compute this whole chain
 * (scopedSessions -> visibleSessions -> sortedSessions -> sessionByAnyId ->
 * pinnedSessions -> pinnedIdentitySet -> isPinnedSession -> isHiddenFromProjects
 * -> projectModel) as component-local `useMemo`s fed by ~15 `useStore()` calls
 * at the sidebar ROOT. Every one of those atoms ticking (a dot-state edge, a
 * turn completing, a PR poll) re-rendered the entire sidebar tree and
 * re-evaluated the whole chain, even for a change only one section could ever
 * paint differently for.
 *
 * Moving the chain into `computed()` stores fixes that WITHOUT extra
 * component plumbing: nanostores' `computed` already short-circuits a
 * recompute (and therefore a re-notify) unless one of its own direct
 * dependencies changed reference (see `computed/index.js`'s `args.some(...)`
 * check) — the same discipline `$sidebarStatusExcludedIds` and
 * `$liveTurnSessionIds` (session-dot-state.ts) already use. A section
 * component then subscribes to ONLY the leaf store its render actually needs
 * (`$sidebarPinnedSessions`, `$sidebarProjectModel`, ...), so a tick that
 * doesn't change that leaf's value never reaches that section's render.
 *
 * Session-list churn itself ($sessions republishing a new array reference on
 * every turn completion) is NOT addressed here — that's the upstream #67245 /
 * #67195 territory this card's body already flags as out of scope. What this
 * file removes is the *sidebar's own* amplification of that churn: one root
 * re-render fanning out to every section regardless of which one the tick
 * actually concerns.
 */
import { computed } from 'nanostores'

import { orderByIds } from '@/app/chat/sidebar/order'
import { filterSessionsByProfileScope } from '@/app/chat/sidebar/profile-scope'
import { orderProjectsByIds, sortProjectsForOverview } from '@/app/chat/sidebar/projects/model'
import {
  excludeProjectSessions,
  liveSessionProjectId,
  sessionRecency,
  type SidebarProjectTree
} from '@/app/chat/sidebar/projects/workspace-groups'
import { buildSessionByAnyId, resolvePinnedSessions } from '@/app/chat/sidebar/session-index'
import { translateNow } from '@/i18n'
import { normalizeSessionSource, sessionSourceLabel } from '@/lib/session-source'
import { stableSet } from '@/lib/stable-array'
import type { SessionInfo } from '@/types/hermes'

import {
  $dismissedAutoProjectIds,
  $pinnedSessionIds,
  $sidebarFiltersActive,
  $sidebarGrouping,
  $sidebarPrFilter,
  $sidebarProfileFilter,
  $sidebarProjectFilter,
  $sidebarProjectOrderIds,
  $sidebarShowArchived,
  $sidebarStatusFilter,
  $sidebarWorkspaceOrderIds,
  $sidebarWorkspaceParentOrderIds,
  filterVisibleProjects
} from './layout'
import {
  $profiles,
  $profileScope,
  ALL_PROFILES,
  messagingTotalsKey,
  normalizeProfileKey,
  sidebarProfileForScope
} from './profile'
import { $activeProjectId, $projects, $projectTree } from './projects'
import { $pullRequestsByBranch, $prBranchBySession as _prBranchBySession, pullRequestBucket, sessionPrKey } from './pull-requests'
import {
  $cronSessions,
  $messagingPlatformTotals,
  $messagingSessions,
  $messagingTruncated,
  $sessions,
  $sessionsLoading
} from './session'
import { $sidebarStatusExcludedIds } from './session-dot-state'
import { $unconfirmedPinWrites } from './session-pin-sync'
import { $archivedSessions } from './sidebar-archive'

// `$prBranchBySession` isn't read here (sessionPrKey reads through it via its
// own module-level `.get()`), but the filter predicate below recomputes when
// it changes — a recovered PR has to re-ask sessionPrKey with the key it just
// learned, same comment as the sidebar root's original `prLookupsByRepo`.
void _prBranchBySession

/** Sessions in scope: the archived set when Archived is on, else the live
 *  list — narrowed to the active profile scope. Reference changes whenever
 *  `$sessions`/`$archivedSessions` do (session-list churn is out of scope for
 *  this file, see header), but nothing downstream of a section that doesn't
 *  read this store pays for that. */
export const $sidebarScopedSessions = computed(
  [$sessions, $archivedSessions, $sidebarShowArchived, $profileScope],
  (sessions, archived, showArchived, profileScope) =>
    filterSessionsByProfileScope(showArchived ? archived : sessions, profileScope)
)

/** True once a second profile exists AND the sidebar is scoped to "all" — the
 *  gate for cross-profile grouping/tags. Boolean-valued, so nanostores' `!==`
 *  gate on `atom.set()` already dedupes: a section subscribing to this never
 *  re-renders unless the gate itself flips. */
export const $sidebarAllProfilesActive = computed(
  [$profiles, $profileScope],
  (profiles, scope) => profiles.length > 1 && scope === ALL_PROFILES
)

/** Whether the sidebar is showing the grouped-by-workspace (Projects) view.
 *  Messaging groups and the Cron section only render in the flat view — the
 *  workspace tree has no room for either — so both read this instead of
 *  duplicating the `grouping === 'project' && !showArchived` check. */
export const $sidebarWorktreeGroupingActive = computed(
  [$sidebarGrouping, $sidebarShowArchived],
  (grouping, showArchived) => grouping === 'project' && !showArchived
)

/** One predicate for the status/project/profile/PR filters, shared by the
 *  flat list (via `$sidebarVisibleSessions`) and the project lanes (via
 *  `$sidebarIsHiddenFromProjects`) so both narrow by the same rule. Republishes
 *  a fresh closure whenever an input changes — the same shape a
 *  `useCallback(fn, deps)` gave, just backed by store deps instead of props. */
export const $sidebarSessionMatchesFilters = computed(
  [
    $sidebarStatusExcludedIds,
    $sidebarProjectFilter,
    $sidebarProfileFilter,
    $sidebarAllProfilesActive,
    $sidebarPrFilter,
    $pullRequestsByBranch,
    $projects
  ],
  (statusExcludedIds, projectFilter, profileFilter, showAllProfiles, prFilter, pullRequests, projects) =>
    (session: SessionInfo): boolean => {
      if (statusExcludedIds.has(session.id)) {
        return false
      }

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
      return !projectFilter.length || projectFilter.includes(liveSessionProjectId(session, projects) ?? '')
    }
)

/** Whether any filter narrows the session pool at all. */
export const $sidebarFiltersNarrow = computed(
  [$sidebarStatusFilter, $sidebarProjectFilter, $sidebarPrFilter, $sidebarAllProfilesActive, $sidebarProfileFilter],
  (statusFilter, projectFilter, prFilter, showAllProfiles, profileFilter) =>
    statusFilter.length > 0 ||
    projectFilter.length > 0 ||
    prFilter.length > 0 ||
    (showAllProfiles && profileFilter.length > 0)
)

/** Scoped sessions narrowed by the active filters (status/project/profile/PR). */
export const $sidebarVisibleSessions = computed(
  [$sidebarScopedSessions, $sidebarFiltersNarrow, $sidebarSessionMatchesFilters],
  (scoped, narrow, matches) => (narrow ? scoped.filter(matches) : scoped)
)

/** Visible sessions ordered newest-first (activity recency). */
export const $sidebarSortedSessions = computed($sidebarVisibleSessions, visible =>
  [...visible].sort((a, b) => sessionRecency(b) - sessionRecency(a))
)

export const $sidebarVisibleCronSessions = computed([$cronSessions, $profileScope], (cron, scope) =>
  filterSessionsByProfileScope(cron, scope)
)

export const $sidebarVisibleMessagingSessions = computed([$messagingSessions, $profileScope], (messaging, scope) =>
  filterSessionsByProfileScope(messaging, scope)
)

/** Every visible/cron/messaging session indexed by every id a pin might be
 *  stored under (see `buildSessionByAnyId`). */
export const $sidebarSessionByAnyId = computed(
  [$sidebarVisibleSessions, $sidebarVisibleCronSessions, $sidebarVisibleMessagingSessions],
  (visible, cron, messaging) => buildSessionByAnyId(visible, cron, messaging)
)

/** The Pinned section's rows (local pin order first, then server-flagged pins
 *  the local set doesn't know about yet — see `resolvePinnedSessions`). */
export const $sidebarPinnedSessions = computed(
  [
    $pinnedSessionIds,
    $sidebarSessionByAnyId,
    $sidebarVisibleSessions,
    $cronSessions,
    $messagingSessions,
    $unconfirmedPinWrites
  ],
  (pinnedIds, sessionByAnyId, visible, cron, messaging, unconfirmedPinWrites) =>
    resolvePinnedSessions(pinnedIds, sessionByAnyId, [...visible, ...cron, ...messaging], unconfirmedPinWrites)
)

let pinnedIdentitySetCache: ReadonlySet<string> = new Set()

/** Every id a pin is reachable under (raw stored ids + both identities of
 *  each resolved pinned session) — see the identity note in session-index.ts
 *  for why both are needed. `stableSet`-guarded so a recompute that lands on
 *  the same membership doesn't push a fresh Set reference through
 *  `$sidebarIsPinnedSession` / `$sidebarIsHiddenFromProjects` below. */
export const $sidebarPinnedIdentitySet = computed(
  [$pinnedSessionIds, $sidebarPinnedSessions],
  (pinnedIds, pinnedSessions) => {
    const ids = new Set(pinnedIds)

    for (const session of pinnedSessions) {
      ids.add(session.id)

      if (session._lineage_root_id) {
        ids.add(session._lineage_root_id)
      }
    }

    return (pinnedIdentitySetCache = stableSet(pinnedIdentitySetCache, ids))
  }
)

/** A pinned session belongs to the Pinned section and nowhere else. Republishes
 *  a fresh predicate only when the identity set itself changed membership
 *  (thanks to the `stableSet` guard above). */
export const $sidebarIsPinnedSession = computed(
  $sidebarPinnedIdentitySet,
  identitySet =>
    (session: SessionInfo): boolean =>
      identitySet.has(session.id) || (session._lineage_root_id != null && identitySet.has(session._lineage_root_id))
)

/** What the project tree drops: pins (their own section) plus anything the
 *  active filters exclude — the same rule the flat list applies, so filtering
 *  reads the same whether you're looking at lanes or the flat view. */
export const $sidebarIsHiddenFromProjects = computed(
  [$sidebarIsPinnedSession, $sidebarFiltersNarrow, $sidebarSessionMatchesFilters],
  (isPinnedSession, filtersNarrow, sessionMatchesFilters) =>
    (session: SessionInfo): boolean => isPinnedSession(session) || (filtersNarrow && !sessionMatchesFilters(session))
)

/** Sorted sessions with pins excluded — recents order, feeds the flat
 *  Recents list and (via its own recency sort) the grouped-by-profile view. */
export const $sidebarUnpinnedAgentSessions = computed(
  [$sidebarSortedSessions, $sidebarIsPinnedSession],
  (sorted, isPinnedSession) => sorted.filter(session => !isPinnedSession(session))
)

/** Apply the persisted repo + worktree orders to a project's repo subtrees. */
function orderRepos(
  repos: SidebarProjectTree['repos'],
  workspaceParentOrderIds: string[],
  workspaceOrderIds: string[]
): SidebarProjectTree['repos'] {
  return orderByIds(repos, parent => parent.id, workspaceParentOrderIds).map(parent => ({
    ...parent,
    groups: orderByIds(parent.groups, group => group.id, workspaceOrderIds)
  }))
}

/** The single top-level project model: the backend's `projects.tree`
 *  unified with dismissed-auto-project filtering, the project filter, pinned
 *  + filtered-out session exclusion, persisted repo/lane order, and the
 *  overview sort/drag-order. Membership is always the backend tree's — never
 *  re-derived here. */
export const $sidebarProjectModel = computed(
  [
    $projectTree,
    $dismissedAutoProjectIds,
    $sidebarProjectFilter,
    $sidebarProjectOrderIds,
    $activeProjectId,
    $sidebarWorkspaceParentOrderIds,
    $sidebarWorkspaceOrderIds,
    $sidebarIsHiddenFromProjects
  ],
  (
    projectTree,
    dismissedAutoProjects,
    projectFilter,
    projectOrderIds,
    activeProjectId,
    workspaceParentOrderIds,
    workspaceOrderIds,
    isHiddenFromProjects
  ) => {
    const sorted = sortProjectsForOverview(
      filterVisibleProjects(projectTree, dismissedAutoProjects)
        .filter(project => !projectFilter.length || projectFilter.includes(project.id))
        .map(project =>
          excludeProjectSessions(
            {
              ...project,
              // Home is synthetic, so its name is ours to translate. Reading
              // through `translateNow` here matches the existing pattern in
              // store/projects.ts (`projectsStaleBackendError`) for a store
              // module needing a translated string outside a component.
              label: project.isNoProject ? translateNow('sidebar.projects.home') : project.label,
              repos: orderRepos(project.repos, workspaceParentOrderIds, workspaceOrderIds)
            },
            isHiddenFromProjects
          )
        ),
      activeProjectId
    )

    return orderProjectsByIds(sorted, projectOrderIds)
  }
)

export interface SidebarMessagingPlatformGroup {
  sourceId: string
  label: string
  sessions: SessionInfo[]
  total: number
  hasMore: boolean
}

/** Each messaging platform as its own group: split the messaging slice by
 *  source (newest platform first, rows within a platform by recency), pinned
 *  rows discounted from the per-platform total/hasMore accounting. */
export const $sidebarMessagingGroups = computed(
  [
    $sidebarVisibleMessagingSessions,
    $messagingPlatformTotals,
    $messagingTruncated,
    $sidebarIsPinnedSession,
    $profileScope
  ],
  (visibleMessagingSessions, messagingPlatformTotals, messagingTruncated, isPinnedSession, profileScope) => {
    if (!visibleMessagingSessions.length) {
      return [] as SidebarMessagingPlatformGroup[]
    }

    const messagingProfile = sidebarProfileForScope(profileScope)
    const bySource = new Map<string, SessionInfo[]>()
    const pinnedBySource = new Map<string, number>()

    for (const session of visibleMessagingSessions) {
      const sourceId = normalizeSessionSource(session.source)

      if (!sourceId) {
        continue
      }

      if (isPinnedSession(session)) {
        pinnedBySource.set(sourceId, (pinnedBySource.get(sourceId) ?? 0) + 1)

        continue
      }

      const list = bySource.get(sourceId) ?? []
      list.push(session)
      bySource.set(sourceId, list)
    }

    return [...bySource.entries()]
      .map(([sourceId, list]) => {
        const ordered = [...list].sort((a, b) => sessionRecency(b) - sessionRecency(a))
        const known = messagingPlatformTotals[messagingTotalsKey(messagingProfile, sourceId)]
        const unpinnedKnown = known == null ? null : Math.max(0, known - (pinnedBySource.get(sourceId) ?? 0))
        const total = Math.max(ordered.length, unpinnedKnown ?? 0)

        return {
          hasMore: unpinnedKnown != null ? unpinnedKnown > ordered.length : messagingTruncated,
          label: sessionSourceLabel(sourceId) ?? sourceId,
          sessions: ordered,
          sourceId,
          total
        }
      })
      .sort((a, b) => sessionRecency(b.sessions[0]) - sessionRecency(a.sessions[0]))
  }
)

/** Whether the sidebar has anything at all to show below the nav (vs. the
 *  blank state). Boolean-valued: a section-owning consumer of the heavier
 *  stores above may recompute often, but THIS store only renotifies when the
 *  yes/no answer flips — a root subscribing to it stays a "layout/scope" read,
 *  not a per-row one. */
export const $sidebarShowSessionSections = computed(
  [$sessionsLoading, $sidebarScopedSessions, $sidebarFiltersActive, $sidebarSortedSessions, $sidebarProjectModel],
  (sessionsLoading, scoped, filtersActive, sorted, projectModel) => {
    const showSkeletons = sessionsLoading && scoped.length === 0

    return showSkeletons || filtersActive || sorted.length > 0 || projectModel.length > 0
  }
)
