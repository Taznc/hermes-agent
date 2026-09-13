/** Shared kanban UI atoms: formatters, the identity avatar, the status menu,
 *  section chrome, and the masked scroller. Pure SDK + tokens. */

import {
  atom,
  Button,
  cn,
  coarseElapsed,
  Codicon,
  CompactMarkdown,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  FadeScroll,
  host,
  profileColor,
  profileColorSoft,
  relativeTime,
  TextTab,
  TextTabMeta,
  useQuery
} from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react'

import { fetchOrchestration, ORCHESTRATION_KEY } from './api'
import { columnLabel, useKanban } from './i18n'
import { columnMeta, type KanbanTask, laneDropAllowed } from './types'

// Plugin-scoped i18n lives in ./i18n; re-exported so components import strings
// and chrome from one place (./ui).
export { columnHelp, columnLabel, type KanbanText, lockedReason, useKanban } from './i18n'

/** One-shot "open the new-task dialog in this lane" request, so a command that
 *  fires from ANYWHERE (keybind, palette) can reach the board page without the
 *  page having to exist yet: the handler navigates and drops the lane here, the
 *  page consumes it on arrival and clears it. Ephemeral by design — never
 *  persisted, so a remount can't reopen a dialog the user already dismissed. */
export const $newTaskLane = atom<null | string>(null)

/** Orchestration knobs (cached app-wide; the settings panel invalidates). */
export function useOrchestration() {
  return useQuery({ queryKey: ORCHESTRATION_KEY, queryFn: fetchOrchestration, staleTime: 60_000 }).data
}

/** The dispatcher's configured fallback for unassigned ready cards
 *  (`kanban.default_assignee`) — '' when unset, i.e. unassigned never runs. */
export function useDefaultAssignee(): string {
  return useOrchestration()?.default_assignee.trim() ?? ''
}

// System-owned drop targets — you can drag a card OUT of these, never INTO
// them, so lanes/menus must not offer them as targets. `running`/`review` are
// claimed by the dispatcher; `scheduled` needs a wake-up time only an agent or
// the CLI can attach (a bare status drag is refused with a 409). The reason
// copy lives in the plugin i18n bundle (`locked.*`); see `lockedReason`.
export const LOCKED_COLUMNS = ['review', 'running', 'scheduled'] as const

export const isLockedTarget = (name: string): boolean => (LOCKED_COLUMNS as readonly string[]).includes(name)

export const shortId = (id?: null | string) => (id ?? '').replace(/^t_/, '').slice(0, 6)

/**
 * The FULL task id as a click-to-copy chip. `shortId` truncates (`t_44ca59a3`
 * → `44ca59`), which made the on-card id disagree with the id used by every
 * CLI command and agent report — this chip always shows the exact string
 * `kanban_show`/`hermes kanban` accept, and one click puts it on the
 * clipboard. Click never bubbles (cards/rows have their own click actions).
 */
export function IdChip({ className, id }: { className?: string; id: string }) {
  const k = useKanban()
  const [copied, setCopied] = useState(false)

  return (
    <button
      className={cn(
        'inline-flex min-w-0 items-center gap-1 rounded px-1 py-px font-mono text-(--ui-text-quaternary) transition-colors hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)',
        className
      )}
      onClick={event => {
        event.stopPropagation()
        void navigator.clipboard.writeText(id)
        host.notify({ kind: 'info', message: k.copiedId(id) })
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      }}
      title={k.copyTaskId}
      type="button"
    >
      <span className="truncate">{id}</span>
      <Codicon className="shrink-0" name={copied ? 'check' : 'copy'} size="0.65rem" />
    </button>
  )
}

/** The one way this plugin tints a surface with a status/severity tone. Every
 *  caller goes through it so "how strong is a wash" is a single decision and
 *  the tone itself always traces back to COLUMN_META / SEVERITY_TONE — there
 *  is no second palette and no raw color anywhere downstream. */
export const wash = (tone: string, percent: number) => `color-mix(in srgb, ${tone} ${percent}%, transparent)`

// The electron REST bridge throws `Error("409: {\"detail\":\"…\"}")`; pull out
// the human-readable detail for a toast.
export function errText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const brace = raw.indexOf('{')

  if (brace !== -1) {
    try {
      return (JSON.parse(raw.slice(brace)) as { detail?: string }).detail ?? raw
    } catch {
      // Not JSON — fall through to the raw message.
    }
  }

  return raw
}

/** Backend timestamps are epoch SECONDS; the canonical formatter takes ms. */
export const ago = (seconds?: null | number): null | string => (seconds ? relativeTime(seconds * 1000) : null)

const ELAPSED_SUFFIX = { day: 'd', hour: 'h', minute: 'm', second: 's' } as const

/** Compact run duration ("42s", "3m") off the canonical elapsed bucketing. */
export function duration(start?: null | number, end?: null | number): null | string {
  if (!start || !end || end < start) {
    return null
  }

  const { unit, value } = coarseElapsed((end - start) * 1000)

  return `${value}${ELAPSED_SUFFIX[unit]}`
}

// ── liveness ─────────────────────────────────────────────────────────────────

/** Live elapsed label ("34s", "2m") that keeps ticking while mounted. */
function useTicking(start?: null | number): null | string {
  const [, force] = useState(0)

  useEffect(() => {
    if (!start) {
      return
    }

    const id = window.setInterval(() => force(n => n + 1), 5_000)

    return () => window.clearInterval(id)
  }, [start])

  if (!start) {
    return null
  }

  const { unit, value } = coarseElapsed(Math.max(0, Date.now() - start * 1000))

  return `${value}${ELAPSED_SUFFIX[unit]}`
}

export type ArcState = 'queued' | 'running' | 'stale'

/**
 * The card's machine-activity state. The board looked dead between "I made a
 * card" and "it's suddenly running" — this narrates the in-between. Only the
 * working states animate the border arc (see kanban.css): running = brisk
 * sweep, no-heartbeat = amber crawl. `queued` (triage / assigned-ready /
 * review) renders as the footer's named-agent chip — motion means work.
 */
export function arcState(task: KanbanTask, fallbackAssignee: string): ArcState | null {
  if (task.status === 'running') {
    // No heartbeat for 2+ min = the worker may have died; the dispatcher will
    // reclaim it, but be honest instead of sweeping green forever.
    const stale = task.last_heartbeat_at ? Date.now() / 1000 - task.last_heartbeat_at > 120 : false

    return stale ? 'stale' : 'running'
  }

  const queued =
    task.status === 'triage' ||
    task.status === 'review' ||
    (task.status === 'ready' && Boolean(task.assignee || fallbackAssignee))

  return queued ? 'queued' : null
}

/** Ticking "working · 34s" line for running cards (elapsed since claim). */
export function RunClock({ task }: { task: KanbanTask }) {
  const k = useKanban()
  const elapsed = useTicking(task.started_at)

  if (!elapsed) {
    return null
  }

  return (
    <span className="shrink-0 font-medium" style={{ color: columnMeta('running').tone }}>
      {k.working} · {elapsed}
    </span>
  )
}

function initials(name: string): string {
  const parts = name
    .trim()
    .split(/[\s_\-./]+/)
    .filter(Boolean)

  return `${parts[0]?.[0] ?? '?'}${parts[1]?.[0] ?? ''}`.toUpperCase()
}

export function Avatar({ name, size = '1.25rem' }: { name: string; size?: string }) {
  // Same identity hue the rest of the app uses (profileColor); default/empty
  // profiles are neutral. Soft tag fill + colored glyph, per the app's tags.
  const color = profileColor(name)

  return (
    <span
      className="grid shrink-0 place-items-center rounded-full font-semibold"
      style={{
        backgroundColor: color ? profileColorSoft(color, 22) : 'var(--ui-bg-quaternary)',
        color: color ?? 'var(--ui-text-secondary)',
        fontSize: '0.5625rem',
        height: size,
        width: size
      }}
      title={name}
    >
      {initials(name)}
    </span>
  )
}

// Jira-style status control: a colored button showing the current state, click
// to transition. Options carry their column dot; the active one is checked.
export function StatusMenu({
  columns,
  onMove,
  status
}: {
  columns: string[]
  onMove: (status: string) => void
  status: string
}) {
  const k = useKanban()
  const meta = columnMeta(status)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-[0.6875rem] font-semibold uppercase tracking-wide transition-[filter] hover:brightness-105"
          style={{ backgroundColor: wash(meta.tone, 15), color: meta.tone }}
          type="button"
        >
          <Codicon name={meta.codicon} size="0.75rem" />
          {columnLabel(k, status)}
          <Codicon name="chevron-down" size="0.7rem" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        {columns
          // Same lane predicate the board's menus and drop handler use, so the
          // drawer can never offer a transition the backend refuses with a 400.
          .filter(name => name === status || (!isLockedTarget(name) && laneDropAllowed(status, name)))
          .map(name => (
            <DropdownMenuItem key={name} onSelect={() => onMove(name)}>
              <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
              {columnLabel(k, name)}
              {name === status && <Codicon className="ml-auto" name="check" size="0.8rem" />}
            </DropdownMenuItem>
          ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// The board's one field/section-label style — hoisted so Section (here), the
// create dialog's Field, and the orchestration panel all read identically.
export const FIELD_LABEL = 'text-[0.62rem] font-semibold uppercase tracking-[0.14em] text-(--ui-text-quaternary)'

export function Section({
  action,
  children,
  label,
  tone
}: {
  action?: ReactNode
  children: ReactNode
  label: string
  /** Optional accent for the section's label — a tone-colored label + dot is
   *  how a section says "this is about a blocked/failed/review state" without
   *  wrapping itself in a box (DESIGN.md: flat, not boxed). */
  tone?: string
}) {
  return (
    <section className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <div className={cn(FIELD_LABEL, 'flex items-center gap-1.5')} style={tone ? { color: tone } : undefined}>
          {tone && <span className="size-1.5 rounded-full" style={{ backgroundColor: tone }} />}
          {label}
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

// Tinted advisory panel: a `tone`-washed body with a matching left rule and a
// tone-colored icon+title header. Shared by the drawer's diagnostics and its
// ready-but-unassigned warning so both read identically.
export function Callout({
  children,
  icon = 'warning',
  title,
  tone
}: {
  children?: ReactNode
  icon?: string
  title: ReactNode
  tone: string
}) {
  return (
    <div
      className="flex flex-col gap-2 rounded-md p-2.5"
      style={{ backgroundColor: wash(tone, 7), borderLeft: `2px solid ${tone}` }}
    >
      <div className="flex items-start gap-1.5 text-[0.75rem] font-medium" style={{ color: tone }}>
        <Codicon className="mt-px shrink-0" name={icon} size="0.8rem" />
        <span>{title}</span>
      </div>
      {children}
    </div>
  )
}

// The task detail view's top-of-drawer call-to-action: a full-bordered,
// tone-washed banner (louder than Callout's left-rule treatment) reserved for
// states that need a human decision right now — blocked, needs review, needs
// an answer. Rendered once, above the meta table, so it's the first thing a
// user sees on a card that needs them; everything else stays informational.
export function Banner({
  actions,
  children,
  icon,
  title,
  tone
}: {
  actions?: ReactNode
  children?: ReactNode
  icon: string
  title: ReactNode
  tone: string
}) {
  return (
    <div
      className="flex flex-col gap-2 rounded-lg border p-3"
      style={{
        backgroundColor: wash(tone, 10),
        borderColor: wash(tone, 32)
      }}
    >
      <div className="flex items-center gap-2">
        <Codicon className="shrink-0" name={icon} size="0.9rem" style={{ color: tone }} />
        <span className="text-[0.8125rem] font-semibold" style={{ color: tone }}>
          {title}
        </span>
      </div>
      {children}
      {actions && <div className="flex flex-wrap gap-1.5">{actions}</div>}
    </div>
  )
}

// A short, edge-masked scroll area. Thin wrapper over the app's FadeScroll so
// the drawer's scrollers behave exactly like the ones in chat; kept as a local
// name because every call site here passes `max`.
export function ScrollFade({ children, deps, max = '9rem' }: { children: ReactNode; deps?: unknown; max?: string }) {
  return (
    <FadeScroll deps={deps} maxHeight={max}>
      {children}
    </FadeScroll>
  )
}

// ── tabs ─────────────────────────────────────────────────────────────────────

/** One tab in the drawer's tab strip. `count` renders as the quiet meta slot
 *  beside the label (TextTabMeta) so "Activity 71" reads as one control. */
export interface TabSpec {
  id: string
  label: string
  count?: number
}

/**
 * The drawer's tab strip. Flat by construction: the app's `TextTab` primitive
 * (underline-on-active, no pill, no box) sitting on a single hairline, so the
 * strip groups the panels below it without nesting a second surface inside the
 * drawer. Tab state belongs to the caller — this is pure presentation.
 */
export function TabStrip({
  active,
  onSelect,
  tabs
}: {
  active: string
  onSelect: (id: string) => void
  tabs: TabSpec[]
}) {
  return (
    <div className="flex items-center gap-3 border-b border-(--ui-stroke-tertiary) px-4" role="tablist">
      {tabs.map(tab => (
        <TextTab
          active={active === tab.id}
          aria-controls={`kanban-tabpanel-${tab.id}`}
          aria-selected={active === tab.id}
          key={tab.id}
          onClick={() => onSelect(tab.id)}
          role="tab"
        >
          {tab.label}
          {tab.count != null && tab.count > 0 && <TextTabMeta>{tab.count}</TextTabMeta>}
        </TextTab>
      ))}
    </div>
  )
}

// ── rows ─────────────────────────────────────────────────────────────────────

/**
 * A list row carrying a tone as a left rule + the faintest possible wash. Used
 * for run rows and activity rows so a crashed run or a block event is findable
 * by color while the highest-volume rows (heartbeats) stay the quietest thing
 * in the list — they resolve to a neutral tone and get no wash at all.
 */
export function AccentRow({
  children,
  className,
  quiet = false,
  tone
}: {
  children: ReactNode
  className?: string
  /** True for high-volume/no-meaning rows: rule only, no fill. */
  quiet?: boolean
  tone: string
}) {
  return (
    <li
      className={cn('rounded-r py-0.5 pl-2', className)}
      style={{
        backgroundColor: quiet ? undefined : wash(tone, 5),
        borderLeft: `2px solid ${quiet ? wash(tone, 45) : tone}`
      }}
    >
      {children}
    </li>
  )
}

// ── markdown ─────────────────────────────────────────────────────────────────

/** How tall the collapsed description may grow, in `em`. ~8 lines at the
 *  compact renderer's leading — the operator's stated ask. */
const COLLAPSED_EM = 13

/**
 * Rendered markdown that starts clamped to ~8 lines with a Show more / Show
 * less affordance. The toggle only appears when the content actually overflows
 * (measured after layout), so a two-line description has no dead control.
 *
 * This renders — it never edits. The inline editor above it hands the user the
 * RAW source in a Textarea; the two are deliberately different views of the
 * same string.
 */
export function CollapsibleMarkdown({ text }: { text: string }) {
  const k = useKanban()
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Re-measure on text change: a card switch or an edit can flip a description
  // from "fits" to "needs the toggle" without the component remounting.
  useLayoutEffect(() => {
    const el = ref.current

    if (el) {
      setOverflows(el.scrollHeight > el.clientHeight + 1)
    }
  }, [text])

  useEffect(() => setExpanded(false), [text])

  return (
    <div className="flex flex-col gap-1">
      <div
        className="overflow-hidden"
        ref={ref}
        style={
          expanded
            ? undefined
            : {
                maskImage: `linear-gradient(to bottom, var(--ui-text-primary) ${COLLAPSED_EM - 2}em, transparent)`,
                maxHeight: `${COLLAPSED_EM}em`
              }
        }
      >
        <CompactMarkdown className="text-[0.78rem] text-(--ui-text-secondary)" text={text} />
      </div>
      {(overflows || expanded) && (
        <Button className="self-start" onClick={() => setExpanded(v => !v)} size="xs" variant="text">
          {expanded ? k.showLess : k.showMore}
        </Button>
      )}
    </div>
  )
}
