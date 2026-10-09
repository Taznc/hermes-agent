import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { useEffect, useMemo, useState } from 'react'

import { useContributions } from '@/contrib'
import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import type { SidebarListRow } from '@/lib/session-date-groups'
import { sessionBucketLabel } from '@/lib/time'
import { $projectTree, fetchProjectSessions, projectProfile } from '@/store/projects'

/**
 * Fork extension point (ledger Part C, X02): a plugin seam on every sidebar
 * GROUP header — date / status dividers, project overview rows, profile
 * groups. Core has row-level seams (`sessionRow.*`) and a profile-group header
 * seam that knows nothing about the group's sessions; this one hands the
 * contribution the group's member session ids so a plugin can act on the
 * whole group (first user: `sidebar-archive` desktop plugin).
 *
 * Additive only: nothing renders unless a plugin registers at
 * `SIDEBAR_GROUP_ACTION_AREA`, so stock behaviour is unchanged.
 */

export { useArchiveView } from './project-archive-view'
export { $sidebarLiveDotStateById as $sessionDotStateById } from './sidebar-status-projections'
export { hasLiveTurn } from '@/store/session-dot-state'

export const SIDEBAR_GROUP_ACTION_AREA = 'fork.sidebar.groupAction'

export type SidebarGroupKind = 'date' | 'profile' | 'project' | 'status'

/** Props handed to a group-action contribution's `render`. */
export interface SidebarGroupActionProps {
  kind: SidebarGroupKind
  /** Stable group key (divider key, project id, or profile-group id). */
  groupKey: string
  /** Human label as shown in the header. */
  label: string
  /** Live ids of the sessions the group holds, in display order. Date/status
   *  and profile groups carry the rows this window has loaded (collapsed
   *  groups included); project groups carry the backend's complete owner set. */
  sessionIds: string[]
}

export interface SidebarGroupActionContribution {
  render: (props: SidebarGroupActionProps) => ReactNode
}

// The flat list's rows (date/status grouping), published by sessions-section
// so a divider — rendered by either the plain or the virtualized list, and
// only ever seeing its own key — can resolve the sessions beneath it,
// including those hidden under a collapsed divider.
export const $forkSidebarListRows = atom<readonly SidebarListRow[]>([])

export function publishForkSidebarListRows(rows: readonly SidebarListRow[]): () => void {
  $forkSidebarListRows.set(rows)

  return () => {
    if ($forkSidebarListRows.get() === rows) {
      $forkSidebarListRows.set([])
    }
  }
}

/** Session ids under `divider` in `rows`, up to the next divider. Matched by
 *  OBJECT IDENTITY, not key: a project lane builds its own divider rows with
 *  the same bucket keys ("yesterday"), and those must not resolve to the
 *  global list's group. A divider not in `rows` has no members. */
export function sessionIdsUnderDivider(rows: readonly SidebarListRow[], divider: SidebarListRow): string[] {
  const start = rows.indexOf(divider)
  const ids: string[] = []

  if (start < 0) {
    return ids
  }

  for (const row of rows.slice(start + 1)) {
    if (row.kind === 'divider') {
      break
    }

    ids.push(row.entry.session.id)
  }

  return ids
}

const GroupActionEntry = ({
  id,
  props,
  render
}: {
  id: string
  props: SidebarGroupActionProps
  render: SidebarGroupActionContribution['render']
}) => {
  const { groupKey, kind, label, sessionIds } = props
  const idsKey = sessionIds.join('\n')

  // Stable component identity per group content: ContribRender mounts the
  // function AS a component, so a fresh closure every paint would remount it
  // (and drop any open confirm dialog it holds).
  const renderSlot = useMemo(
    () => () => render({ groupKey, kind, label, sessionIds: idsKey ? idsKey.split('\n') : [] }),

    [render, groupKey, kind, label, idsKey]
  )

  return (
    <ContribBoundary id={id} variant="chip">
      <ContribRender render={renderSlot} />
    </ContribBoundary>
  )
}

/** Mount every group-action contribution for one group header. */
export function SidebarGroupActionSlot(props: SidebarGroupActionProps) {
  const contributions = useContributions(SIDEBAR_GROUP_ACTION_AREA)

  if (contributions.length === 0) {
    return null
  }

  return (
    <>
      {contributions.map(contribution => {
        const render = (contribution.data as Partial<SidebarGroupActionContribution> | undefined)?.render

        return typeof render === 'function' ? (
          <GroupActionEntry
            id={contribution.id}
            key={`${contribution.source ?? 'core'}:${contribution.id}`}
            props={props}
            render={render}
          />
        ) : null
      })}
    </>
  )
}

/** Flat-list divider flavour (date/status grouping): members come from the
 *  full flat rows sessions-section publishes — collapsed groups included —
 *  so the plain and the virtualized list resolve identically. */
function ForkListDividerGroupActionMembers({ row }: { row: Extract<SidebarListRow, { kind: 'divider' }> }) {
  const { t } = useI18n()
  const rows = useStore($forkSidebarListRows)
  const sessionIds = useMemo(() => sessionIdsUnderDivider(rows, row), [rows, row])
  const label = 'label' in row ? row.label : sessionBucketLabel(row.bucket, t.sidebar.dateDivider)

  return (
    <SidebarGroupActionSlot
      groupKey={row.key}
      kind={row.key.startsWith('status:') ? 'status' : 'date'}
      label={label}
      sessionIds={sessionIds}
    />
  )
}

/** Session-list flavour (profile groups): members are the group's rows. */
function SidebarSessionsGroupActionMembers({
  groupKey,
  kind,
  label,
  sessions
}: {
  groupKey: string
  kind: SidebarGroupKind
  label: string
  sessions: readonly Pick<SessionInfo, 'id'>[]
}) {
  const sessionIds = useMemo(() => sessions.map(session => session.id), [sessions])

  return <SidebarGroupActionSlot groupKey={groupKey} kind={kind} label={label} sessionIds={sessionIds} />
}

type ProjectActionSource = {
  id: string
  label: string
  profile?: string
  previewSessions?: Pick<SessionInfo, 'id'>[]
  repos?: { groups?: { sessions?: Pick<SessionInfo, 'id'>[] }[] }[]
  sessionIds?: string[]
}

// ── Anchor helpers: each upstream call site is ONE call into these (T2). ──

/** Divider anchor: the group action for `row` (session rows get core's
 *  action untouched), then core's own divider action. */
export function forkListDividerAction(row: SidebarListRow, coreAction?: ReactNode): ReactNode {
  if (row.kind !== 'divider') {
    return coreAction
  }

  return (
    <>
      <ForkListDividerGroupAction row={row} />
      {coreAction}
    </>
  )
}

/** sessions-section anchor: publish the flat list rows while they're the
 *  rendered list (date/status grouping only), for divider member lookup. */
export function useForkPublishListRows(rows: readonly SidebarListRow[], active: boolean): void {
  useEffect(() => (active ? publishForkSidebarListRows(rows) : undefined), [rows, active])
}

/** gateway-groups anchor: the group action on a profile/gateway group header. */
export function forkProfileGroupAction(
  group: { id: string; sessions: readonly Pick<SessionInfo, 'id'>[] },
  label: string
) {
  return <SidebarSessionsGroupAction groupKey={group.id} kind="profile" label={label} sessions={group.sessions} />
}

// ── Zero-cost gates: with no plugin registered at the area, an anchor renders
// null before computing members or subscribing to the published rows. ──

function useHasGroupActions(): boolean {
  return useContributions(SIDEBAR_GROUP_ACTION_AREA).length > 0
}

export function SidebarProjectGroupAction(props: { project: ProjectActionSource }) {
  return useHasGroupActions() ? <SidebarProjectGroupActionMembers {...props} /> : null
}

export function SidebarSessionsGroupAction(props: Parameters<typeof SidebarSessionsGroupActionMembers>[0]) {
  return useHasGroupActions() ? <SidebarSessionsGroupActionMembers {...props} /> : null
}

function ForkListDividerGroupAction(props: { row: Extract<SidebarListRow, { kind: 'divider' }> }) {
  return useHasGroupActions() ? <ForkListDividerGroupActionMembers {...props} /> : null
}

function SidebarProjectGroupActionMembers({ project }: { project: ProjectActionSource }) {
  const [hydrated, setHydrated] = useState<{ key: string; ids: string[] } | null>(null)
  const profile = projectProfile()
  const key = `${profile ?? ''}:${project.id}`
  const needsHydration = project.sessionIds === undefined
  useEffect(() => {
    if (!needsHydration || !profile) {
      return
    }

    let cancelled = false
    void fetchProjectSessions(project.id, { supersedable: false })
      .then(full => {
        if (cancelled || !full) {
          return
        }

        const rows = full.repos.flatMap(repo => repo.groups.flatMap(group => group.sessions))
        setHydrated({ key, ids: [...new Set(rows.map(s => s.id))] })
        // Hydrated members carry profile/connection and lineage; publish them to
        // the existing owner resolver, not an approximation from the 3-row preview.
        $projectTree.set(
          $projectTree.get().map(current => (current.id === full.id && projectProfile() === profile ? full : current))
        )
      })
      .catch(() => {
        /* no complete membership: do not offer a partial archive */
      })

    return () => {
      cancelled = true
    }
  }, [key, needsHydration, profile, project.id])

  const sessionIds = project.sessionIds ?? (hydrated?.key === key ? hydrated.ids : null)

  if (!sessionIds) {
    return null
  }

  return <SidebarGroupActionSlot groupKey={project.id} kind="project" label={project.label} sessionIds={sessionIds} />
}
