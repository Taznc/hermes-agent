/**
 * The focus-mode answer bar (design B): while a card is focused it replaces
 * the one-line focus hint above the board and answers "why is this card not
 * moving?" in words —
 *
 *  - the focused card and a one-line verdict (`focusVerdict`), e.g.
 *    "Stalled — all 2 open blockers are On hold";
 *  - its direct blockers (a status-colour strip + one row each, most-stuck
 *    first) and the cards waiting on it. Hovering a row isolates its line on
 *    the board; clicking one moves the focus to that card;
 *  - the Direct / Full chain switch and Clear focus;
 *  - a legend for the line colours, with the chevron / moving-dots toggles.
 *
 * `FocusRollup` is design A's half: the same verdict, compact, on the focused
 * card itself. Both read `focusVerdict`, so they can never disagree.
 */

import { Button, cn, Codicon, useValue } from '@hermes/plugin-sdk'
import type { CSSProperties, ReactNode } from 'react'

import { $depChevrons, $depFlow } from './api'
import { edgeId } from './board-arrows'
import { $hotEdge, type FocusDepth, FocusDepthControls } from './board-arrows-layer'
import type { DependencyGraph } from './deps'
import {
  type FocusLink,
  focusLinks,
  type FocusVerdict,
  focusVerdict,
  LEGEND_STATUSES,
  linkTone,
  type VerdictKind
} from './focus-verdict'
import type { KanbanTask } from './types'
import { columnLabel, type KanbanText, useKanban } from './ui'

/** Verdict chip colours, one per kind — the same hues the lines use. */
const VERDICT_TONE: Record<VerdictKind, string> = {
  clear: linkTone('done'),
  none: 'var(--ui-text-tertiary)',
  stalled: linkTone('on_hold'),
  waiting: linkTone('review')
}

const statusName = (k: KanbanText, status: string) => (status === 'unknown' ? k.depMissing : columnLabel(k, status))

/** The verdict in words. Exported for the focused card's roll-up. */
export function verdictText(k: KanbanText, verdict: FocusVerdict): string {
  switch (verdict.kind) {
    case 'none':
      return k.depVerdictNone

    case 'clear':
      return k.depVerdictClear(verdict.blockers.length)

    case 'stalled':
      return k.depVerdictStalled(verdict.open)

    case 'waiting':
      return k.depVerdictWaiting(
        verdict.open,
        verdict.byStatus.map(([status, n]) => k.depStatusCount(n, statusName(k, status))).join(' · '),
        verdict.cleared
      )
  }
}

const VERDICT_ICON: Record<VerdictKind, string> = {
  clear: 'pass-filled',
  none: 'circle-outline',
  stalled: 'debug-pause',
  waiting: 'clock'
}

function Verdict({ verdict }: { verdict: FocusVerdict }) {
  const k = useKanban()
  const tone = VERDICT_TONE[verdict.kind]

  return (
    <div
      className="flex items-start gap-1.5 rounded-md px-2 py-1.5 text-[0.71875rem] font-semibold leading-snug"
      data-verdict={verdict.kind}
      style={{ backgroundColor: `color-mix(in srgb, ${tone} 14%, transparent)`, color: tone }}
    >
      <Codicon className="mt-px shrink-0" name={VERDICT_ICON[verdict.kind]} size="0.8rem" />
      <span className="min-w-0">{verdictText(k, verdict)}</span>
    </div>
  )
}

/** The small uppercase status pill every row leads with. */
function StatusPill({ status }: { status: string }) {
  const k = useKanban()
  const tone = linkTone(status)

  return (
    <span
      className="shrink-0 rounded-full px-1.5 py-px text-[0.5625rem] font-bold uppercase tracking-[0.04em] whitespace-nowrap"
      style={{
        backgroundColor: `color-mix(in srgb, ${tone} 26%, transparent)`,
        color: `color-mix(in srgb, ${tone} 60%, var(--ui-text-primary, white))`
      }}
    >
      {statusName(k, status)}
    </span>
  )
}

function LinkRow({ edge, link, onFocus }: { edge: string; link: FocusLink; onFocus: (key: string) => void }) {
  const k = useKanban()

  const leave = () => {
    if ($hotEdge.get() === edge) {
      $hotEdge.set(null)
    }
  }

  const body = (
    <>
      <StatusPill status={link.status} />
      <span className={cn('min-w-0 truncate', link.missing ? 'italic text-(--ui-text-quaternary)' : 'text-foreground')}>
        {link.missing ? k.depMissingTip : link.title}
      </span>
    </>
  )

  // A link to a card the board doesn't have can still be highlighted (its
  // line is simply absent) but there is nothing to move the focus to.
  if (link.missing) {
    return (
      <div
        className="flex min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-[0.6875rem]"
        data-link-row={link.key}
      >
        {body}
      </div>
    )
  }

  return (
    <button
      className="flex w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-left text-[0.6875rem] hover:bg-(--chrome-action-hover) focus-visible:bg-(--chrome-action-hover)"
      data-link-row={link.key}
      onBlur={leave}
      onClick={() => {
        leave()
        onFocus(link.key)
      }}
      onFocus={() => $hotEdge.set(edge)}
      onMouseEnter={() => $hotEdge.set(edge)}
      onMouseLeave={leave}
      title={k.depRowTip}
      type="button"
    >
      {body}
    </button>
  )
}

function Heading({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-1 text-[0.625rem] font-bold uppercase tracking-[0.06em] text-(--ui-text-tertiary)">{children}</h3>
  )
}

/** A status-colour strip: one segment per link, so the mix reads at a glance. */
function StatusStrip({ links }: { links: FocusLink[] }) {
  return (
    <div aria-hidden className="mb-1.5 flex h-1.5 gap-0.5">
      {links.map(link => (
        <span className="flex-1 rounded-sm" key={link.key} style={{ backgroundColor: linkTone(link.status) }} />
      ))}
    </div>
  )
}

function Toggle({ label, on, onToggle }: { label: string; on: boolean; onToggle: () => void }) {
  return (
    <Button
      aria-pressed={on}
      className={cn('h-5 px-1.5', on && 'text-foreground')}
      onClick={onToggle}
      size="xs"
      variant="ghost"
    >
      <Codicon name={on ? 'pass-filled' : 'circle-large-outline'} size="0.7rem" />
      {label}
    </Button>
  )
}

function Legend() {
  const k = useKanban()
  const chevrons = useValue($depChevrons)
  const flow = useValue($depFlow)

  return (
    <div className="col-span-full flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-(--ui-stroke-tertiary) pt-1.5 text-[0.625rem] text-(--ui-text-tertiary)">
      <span>{k.depLegendLead}</span>
      {LEGEND_STATUSES.map(status => (
        <span className="inline-flex items-center gap-1" key={status}>
          <i
            className="inline-block h-1 w-5 rounded-sm"
            style={
              {
                background:
                  status === 'done'
                    ? `repeating-linear-gradient(90deg, ${linkTone(status)} 0 5px, transparent 5px 9px)`
                    : linkTone(status)
              } as CSSProperties
            }
          />
          {columnLabel(k, status)}
        </span>
      ))}
      <span className="text-(--ui-text-quaternary)">· {k.depLegendHead}</span>
      <span className="ml-auto flex items-center gap-1">
        <Toggle label={k.depChevrons} on={chevrons} onToggle={() => $depChevrons.set(!chevrons)} />
        <Toggle label={k.depFlow} on={flow} onToggle={() => $depFlow.set(!flow)} />
      </span>
    </div>
  )
}

export function FocusAnswerBar({
  depth,
  focused,
  graph,
  index,
  onClear,
  onDepth,
  onFocus
}: {
  depth: FocusDepth
  focused: string
  graph: DependencyGraph
  index: Map<string, KanbanTask>
  onClear: () => void
  onDepth: (depth: FocusDepth) => void
  onFocus: (key: string) => void
}) {
  const k = useKanban()
  const task = index.get(focused)
  const { blockers, dependants } = focusLinks(graph, index, focused)
  const verdict = focusVerdict(graph, index, focused)

  return (
    <section
      aria-label={k.depFocused}
      className="mx-4 mb-2 grid shrink-0 grid-cols-[repeat(auto-fit,minmax(13.5rem,1fr))] gap-x-3 gap-y-2 rounded-lg bg-(--ui-bg-quinary) px-3 py-2 text-(--ui-text-secondary)"
      data-answer-bar
    >
      <div className="min-w-0">
        <Heading>{k.depFocused}</Heading>
        <div className="mb-1.5 line-clamp-2 text-[0.78125rem] font-semibold leading-snug text-foreground">
          {task?.title || task?.id || focused}
        </div>
        <Verdict verdict={verdict} />
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <FocusDepthControls depth={depth} onDepth={onDepth} />
          <Button className="shrink-0" onClick={onClear} size="xs" variant="ghost">
            <Codicon name="close" size="0.7rem" />
            {k.depClearFocus}
          </Button>
        </div>
      </div>
      <div className="min-w-0">
        <Heading>{k.depBlockedByHeading(blockers.length)}</Heading>
        {blockers.length > 0 && <StatusStrip links={blockers} />}
        <div className="flex max-h-32 flex-col overflow-y-auto" data-blocker-rows>
          {blockers.length > 0 ? (
            blockers.map(link => (
              <LinkRow edge={edgeId(link.key, focused)} key={link.key} link={link} onFocus={onFocus} />
            ))
          ) : (
            <span className="px-1.5 py-0.5 text-[0.6875rem] text-(--ui-text-quaternary)">{k.depNothingBlocks}</span>
          )}
        </div>
      </div>
      <div className="min-w-0">
        <Heading>{k.depBlocksHeading(dependants.length)}</Heading>
        <div className="flex max-h-32 flex-col overflow-y-auto" data-dependant-rows>
          {dependants.length > 0 ? (
            dependants.map(link => (
              <LinkRow edge={edgeId(focused, link.key)} key={link.key} link={link} onFocus={onFocus} />
            ))
          ) : (
            <span className="px-1.5 py-0.5 text-[0.6875rem] text-(--ui-text-quaternary)">{k.depNothingWaits}</span>
          )}
        </div>
      </div>
      <Legend />
    </section>
  )
}

/**
 * Design A's roll-up on the focused card: one swatch per direct blocker in
 * that blocker's status colour, then the verdict. Only on a card that HAS
 * blockers — "no blockers" is already what the absence of a blocked-by chip
 * says.
 */
export function FocusRollup({
  graph,
  index,
  taskKey
}: {
  graph: DependencyGraph
  index: Map<string, KanbanTask>
  taskKey: string
}) {
  const k = useKanban()
  const verdict = focusVerdict(graph, index, taskKey)

  if (verdict.kind === 'none') {
    return null
  }

  return (
    <div
      className="flex min-w-0 flex-col gap-1 border-t border-(--ui-stroke-tertiary) pt-1.5 text-[0.65625rem] font-semibold leading-snug"
      data-focus-rollup={verdict.kind}
    >
      <span className="flex gap-0.5">
        {verdict.blockers.map(link => (
          <i
            className="inline-block h-1.5 w-4 rounded-sm"
            key={link.key}
            style={{ backgroundColor: linkTone(link.status) }}
            title={statusName(k, link.status)}
          />
        ))}
      </span>
      <span style={{ color: VERDICT_TONE[verdict.kind] }}>{verdictText(k, verdict)}</span>
    </div>
  )
}
