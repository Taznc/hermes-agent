import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useEffect } from 'react'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { ActionsContextMenu, ActionsMenu, type MenuKit, renderActionItem } from '@/components/ui/actions-menu'
import { Codicon } from '@/components/ui/codicon'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { Tip } from '@/components/ui/tooltip'
import type { HermesRepoStatus } from '@/global'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { worktreeRisk, type WorktreeRisk } from '@/lib/worktree-risk'
import { openWorktreeDialog, registerRepoStatusCwd, repoStatusForCwd } from '@/store/coding-status'
import { copyPath, revealPath } from '@/store/projects'

import { SidebarRowLead } from '../chrome'

// Branch/worktree labels routinely share a long prefix (`bb/coding-context-…`),
// so plain end-truncation (`truncate`) hides exactly the suffix that tells two
// lanes apart — both render as "bb/coding-context…". Keep the tail pinned and
// ellipsize the HEAD instead, so `…context-facts-rpc` and `…context-persona`
// stay distinguishable. Falls back to whole-string for short labels.
function LaneLabel({ label, title }: { label: string; title?: string }) {
  const tailLen = Math.min(14, Math.floor(label.length / 2))
  const head = label.slice(0, label.length - tailLen)
  const tail = label.slice(label.length - tailLen)

  return (
    // overflow-hidden: the pinned tail is shrink-0, so at extreme narrow widths
    // it must clip inside the label rather than push the trailing icons out.
    <span className="flex min-w-0 overflow-hidden" title={title}>
      <span className="truncate">{head}</span>
      <span className="shrink-0 whitespace-pre">{tail}</span>
    </span>
  )
}

// "+" affordance shared by repo and worktree headers — reveals on header hover.
// Also a drag source: dragging it starts the new-session drag pinned to the
// project's path (`onPointerDown`), so the created session inherits the
// project's cwd. A sub-threshold release stays an ordinary click (`onClick`).
export function WorkspaceAddButton({
  label,
  onClick,
  onPointerDown
}: {
  label: string
  onClick: () => void
  onPointerDown?: (event: React.PointerEvent<HTMLButtonElement>) => void
}) {
  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/workspace:opacity-100"
        onClick={onClick}
        onPointerDown={onPointerDown}
        type="button"
      >
        <Codicon name="add" size="0.75rem" />
      </button>
    </Tip>
  )
}

// Reveals the next page of already-loaded rows within a workspace/worktree.
// Hangs off the lane instead of sitting in a row, so it repeats the row's
// trailing inset (SidebarRowShell's `pr-2`) to stay on the edge the rows stop at.
export function WorkspaceShowMoreButton({
  count,
  label,
  onClick
}: {
  count: number
  label: string
  onClick: () => void
}) {
  const { t } = useI18n()
  const text = t.sidebar.showMoreIn(count, label)

  return (
    <Tip label={text}>
      <button
        aria-label={text}
        className="mr-2 ml-auto grid size-5 place-items-center rounded-sm bg-transparent text-(--ui-text-tertiary) transition-colors hover:bg-(--ui-control-hover-background) hover:text-foreground"
        onClick={onClick}
        type="button"
      >
        <Codicon name="ellipsis" size="0.75rem" />
      </button>
    </Tip>
  )
}

// Per-worktree actions (linked worktree lanes only), mirroring the session row
// and ProjectMenu kebab: reveal in the file manager, copy path, and remove the
// worktree (runs a real `git worktree remove` via the caller's confirm dialog).
// Shared by the kebab dropdown and the header's right-click menu so both match.
function useWorkspaceItems({ path, onRemove }: { path: null | string; onRemove: () => void }) {
  const { t } = useI18n()
  const p = t.sidebar.projects

  return (kit: MenuKit) => (
    <>
      {renderActionItem(kit, {
        disabled: !path,
        icon: 'folder-opened',
        key: 'reveal',
        label: p.reveal,
        onSelect: () => void revealPath(path)
      })}
      {renderActionItem(kit, {
        disabled: !path,
        icon: 'copy',
        key: 'copy',
        label: p.copyPath,
        onSelect: () => void copyPath(path)
      })}
      <kit.Separator />
      {renderActionItem(kit, {
        icon: 'trash',
        key: 'remove',
        label: `${p.removeWorktree}…`,
        onSelect: onRemove,
        variant: 'destructive'
      })}
    </>
  )
}

export function WorkspaceMenu({ path, onRemove }: { path: null | string; onRemove: () => void }) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const items = useWorkspaceItems({ onRemove, path })

  return (
    <ActionsMenu ariaLabel={p.menu} contentClassName="w-48" items={items}>
      <button
        aria-label={p.menu}
        className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/workspace:opacity-100 data-[state=open]:opacity-100"
        onClick={event => event.stopPropagation()}
        type="button"
      >
        <Codicon name="kebab-vertical" size="0.75rem" />
      </button>
    </ActionsMenu>
  )
}

// Wrap a worktree lane's header so right-clicking it opens the same actions as
// its kebab. `disabled` renders children bare (a lane with no removable path).
export function WorkspaceContextMenu({
  path,
  onRemove,
  children
}: {
  path: null | string
  onRemove?: () => void
  children: React.ReactNode
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const items = useWorkspaceItems({ onRemove: onRemove ?? (() => {}), path })

  return (
    <ActionsContextMenu ariaLabel={p.menu} contentClassName="w-48" disabled={!onRemove} items={items}>
      {children}
    </ActionsContextMenu>
  )
}

// "New worktree": prompt for a branch name, then git spins up a fresh worktree
// for that branch under the repo (the lightest way) and we open a new session
// inside it. Naming is explicit — no auto-generated `hermes/work-<ts>` trees.
// The base branch defaults to the remote default (origin/HEAD); the user can
// pick any local or remote-tracking branch via a filterable combobox.
export function StartWorkButton({ repoPath }: { repoPath: string }) {
  const { t } = useI18n()
  const p = t.sidebar.projects

  return (
    <Tip label={p.startWork}>
      <button
        aria-label={p.startWork}
        className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100"
        // Publish the intent. The one WorktreeDialog in the sidebar renders it.
        // This button pins its own repo, so it targets this section.
        onClick={() => void openWorktreeDialog({ repoPath })}
        type="button"
      >
        <Codicon name="git-branch" size="0.75rem" />
      </button>
    </Tip>
  )
}

// Semantic dot per DESIGN.md's severity tokens: clean lanes stay silent
// (matches the "silence is the default state" rule for badges elsewhere in
// the sidebar); a merge conflict is the one destructive-red case; uncommitted
// changes are the amber "act on this" case; unpushed/unmerged share the
// informational blue since neither blocks anything, they just mean "this
// worktree still holds work the default branch doesn't have yet"; an
// unresolvable probe renders a hollow muted ring rather than hiding outright,
// so "we don't know" is visually distinct from "everything is clean".
const RISK_TONE: Record<Exclude<WorktreeRisk, 'clean' | 'unknown'>, StatusTone> = {
  conflicted: 'bad',
  uncommitted: 'warn',
  unmerged: 'good',
  unpushed: 'good'
}

function riskTooltip(
  risk: WorktreeRisk,
  status: HermesRepoStatus | null,
  s: ReturnType<typeof useI18n>['t']['statusStack']['coding']
): string {
  if (risk === 'unknown') {
    return s.riskUnknown
  }

  if (!status) {
    return s.riskUnknown
  }

  const parts: string[] = []

  if (status.conflicted > 0) {
    parts.push(s.riskConflicted)
  }

  if (status.changed > 0) {
    parts.push(s.riskUncommitted(status.changed))
  }

  if (status.unpushed > 0) {
    parts.push(s.riskUnpushed(status.unpushed))
  }

  if (status.mergedIntoBase === false && status.defaultBranch) {
    parts.push(s.riskUnmerged(status.defaultBranch))
  }

  return parts.join(' · ') || s.riskUnknown
}

// One lane's git-health dot. Self-subscribing (registers/reads the SAME
// per-cwd probe the composer coding rail uses — no new poller, no new IPC):
// rides the existing refresh edges (cwd change, turn settle, focus,
// `$worktreeRefreshToken`). Renders nothing for a clean lane or a blank path
// (silence is the default state); a probe that hasn't landed yet, or that
// resolved to "unknown", shows a hollow muted ring rather than nothing, so a
// genuinely-unresolvable lane never looks indistinguishable from clean.
export function WorktreeRiskBadge({ path }: { path: null | string }) {
  const { t } = useI18n()
  const s = t.statusStack.coding
  const status = useStore(repoStatusForCwd(path))

  useEffect(() => registerRepoStatusCwd(path), [path])

  if (!path) {
    return null
  }

  const risk = worktreeRisk(status)

  if (risk === 'clean') {
    return null
  }

  if (risk === 'unknown') {
    return (
      <Tip label={s.riskUnknown}>
        <span
          aria-label={s.riskUnknown}
          className="box-border size-1.5 shrink-0 rounded-full border border-dashed border-(--ui-text-quaternary)"
          role="status"
        />
      </Tip>
    )
  }

  return (
    <Tip label={riskTooltip(risk, status, s)}>
      <StatusDot aria-label={riskTooltip(risk, status, s)} className="shrink-0" role="status" tone={RISK_TONE[risk]} />
    </Tip>
  )
}

// Collapsible header shared by the repo (emphasis) and worktree levels: a toggle
// button with a leading glyph, plus an optional trailing action (the +).
export function WorkspaceHeader({
  action,
  emphasis = false,
  icon,
  label,
  onToggle,
  open,
  title,
  ref,
  ...rest
}: {
  action?: React.ReactNode
  emphasis?: boolean
  icon: React.ReactNode
  label: string
  onToggle: () => void
  open: boolean
  /** Hover tooltip — the lane's full on-disk path (worktree / repo root). */
  title?: string
} & React.ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'group/workspace flex min-h-6 items-center gap-1 px-2 pt-1 text-[0.6875rem]',
        emphasis ? 'font-semibold text-(--ui-text-secondary)' : 'font-medium text-(--ui-text-tertiary)'
      )}
      ref={ref}
      {...rest}
    >
      <button
        className={cn(
          'flex min-w-0 flex-1 items-center gap-1.5 bg-transparent text-left',
          emphasis ? 'hover:text-foreground' : 'hover:text-(--ui-text-secondary)'
        )}
        onClick={onToggle}
        type="button"
      >
        <SidebarRowLead>{icon}</SidebarRowLead>
        <LaneLabel label={label} title={title ? `${label}\n${title}` : label} />
        <DisclosureCaret
          className="shrink-0 text-(--ui-text-tertiary) opacity-0 transition group-hover/workspace:opacity-100"
          open={open}
        />
      </button>
      {action}
    </div>
  )
}
