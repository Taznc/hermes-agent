/**
 * The drawer's OVERVIEW tab: everything that answers "what is this card".
 * Diagnostics, the dense meta table, the collapsed description, estimate,
 * dependencies, and result / latest summary. The CTA banner itself lives in
 * `./drawer_cta` — it is rendered once, above the tab strip, not inside a
 * tab body.
 */

import {
  Button,
  cn,
  Codicon,
  compactNumber,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  host,
  Input,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useMemo, useState } from 'react'

import { boardKey, estimateTask, fetchProfiles, PROFILES_KEY } from './api'
import { indexBoard, partitionBlockers, resolveLinks } from './deps'
import {
  columnMeta,
  type Diagnostic,
  type DiagnosticAction,
  type KanbanBoard,
  type KanbanTask,
  type KanbanTaskDetail,
  type KanbanTaskFull,
  type ResolvedLink,
  SEVERITY_TONE,
  type TaskEstimate
} from './types'
import { Avatar, Callout, CollapsibleMarkdown, columnLabel, errText, FIELD_LABEL, Section, shortId, useKanban, wash } from './ui'

/** One label/value pair in the dense meta grid. The value cell truncates
 *  rather than wrapping — a long `workspace_path` used to eat three lines
 *  above the fold, which is precisely what the redesign is fixing. */
export function MetaRow({ children, label, title }: { children: ReactNode; label: string; title?: string }) {
  return (
    <>
      <span className="text-(--ui-text-quaternary)">{label}</span>
      <span className="min-w-0 truncate text-(--ui-text-secondary)" title={title}>
        {children}
      </span>
    </>
  )
}

/** The dashboard's diagnostics panel: severity-toned, plain-English, with the
 *  backend's structured recovery actions as buttons. `reassign` is skipped —
 *  the Assignee control in the meta table IS that action, inline. */
export function Diagnostics({ items, onReclaim }: { items: Diagnostic[]; onReclaim: () => void }) {
  const k = useKanban()

  const act = (action: DiagnosticAction) => {
    if (action.kind === 'reclaim') {
      onReclaim()
    } else if (action.kind === 'cli_hint') {
      void navigator.clipboard.writeText(String(action.payload?.command ?? action.label))
      host.notify({ kind: 'info', message: k.commandCopied })
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {items.map(diag => {
        const tone = SEVERITY_TONE[diag.severity]
        const actions = diag.actions.filter(action => action.kind === 'reclaim' || action.kind === 'cli_hint')

        return (
          <Callout
            key={`${diag.kind}-${diag.last_seen_at}`}
            title={`${diag.title}${diag.count > 1 ? ` ×${diag.count}` : ''}`}
            tone={tone}
          >
            <p className="whitespace-pre-wrap text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
              {diag.detail}
            </p>
            {actions.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {actions.map(action => (
                  <Button
                    key={`${action.kind}-${action.label}`}
                    onClick={() => act(action)}
                    size="xs"
                    variant={action.suggested ? 'secondary' : 'outline'}
                  >
                    {action.kind === 'cli_hint' && <Codicon name="copy" size="0.7rem" />}
                    {action.label}
                  </Button>
                ))}
              </div>
            )}
          </Callout>
        )
      })}
    </div>
  )
}

/** Jira-style inline assignee editor: the meta row IS the control — click the
 *  assignee to reassign (reclaims a running worker first, resets the failure
 *  streak — the explicit human recovery action). */
export function AssigneeMenu({
  current,
  onReassign
}: {
  current: null | string | undefined
  onReassign: (p: string) => void
}) {
  const k = useKanban()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="-mx-1 inline-flex max-w-full items-center gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-(--chrome-action-hover)"
          type="button"
        >
          {current ? (
            <>
              <Avatar name={current} size="0.875rem" />
              <span className="truncate">{current}</span>
            </>
          ) : (
            <span className="text-(--ui-text-quaternary)">{k.unassigned}</span>
          )}
          <Codicon className="shrink-0 text-(--ui-text-quaternary)" name="chevron-down" size="0.65rem" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        {(roster?.profiles ?? []).map(profile => (
          <DropdownMenuItem key={profile.name} onSelect={() => onReassign(profile.name)}>
            <Avatar name={profile.name} size="0.875rem" />
            {profile.name}
            {profile.name === current && <Codicon className="ml-auto" name="check" size="0.8rem" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * The description, as rendered markdown collapsed to ~8 lines, with the
 * existing inline edit intact.
 *
 * Editing and reading are deliberately different views of the same string:
 * the pencil swaps in a Textarea holding the RAW markdown source, so what you
 * type is what gets saved. Rendering only happens on the read side.
 */
export function DescriptionSection({
  body,
  onSave
}: {
  body: null | string | undefined
  onSave: (body: string) => void
}) {
  const k = useKanban()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  return (
    <Section
      action={
        <Button
          aria-label={editing ? k.cancelEdit : k.editDescription}
          onClick={() => {
            setDraft(body ?? '')
            setEditing(!editing)
          }}
          size="icon-xs"
          variant="ghost"
        >
          <Codicon name={editing ? 'close' : 'edit'} size="0.75rem" />
        </Button>
      }
      label={k.description}
    >
      {editing ? (
        <div className="flex flex-col gap-1.5">
          <Textarea
            className="min-h-24 text-[0.75rem]"
            data-kanban-description-input="true"
            onChange={event => setDraft(event.target.value)}
            value={draft}
          />
          <Button
            className="self-end"
            onClick={() => {
              onSave(draft)
              setEditing(false)
            }}
            size="xs"
            variant="secondary"
          >
            {k.save}
          </Button>
        </div>
      ) : body ? (
        <CollapsibleMarkdown text={body} />
      ) : (
        <p className="text-[0.8125rem] text-(--ui-text-quaternary)">{k.noDescription}</p>
      )}
    </Section>
  )
}

// Rough effort estimate via the auxiliary (auto-routed) model. Tokens +
// complexity, never dollars — providers don't report cost reliably. Gated
// behind an explicit click + disclaimer since it makes a model call. The
// control keeps a stable footprint (spinner swaps in place) so there's no
// layout jump when it runs.
export function EstimateSection({ board, id }: { board?: string; id: string }) {
  const k = useKanban()
  const [result, setResult] = useState<null | TaskEstimate>(null)

  const est = useMutation({
    mutationFn: () => estimateTask(id, board),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: r => {
      if (r.ok) {
        setResult(r)
      } else {
        host.notify({ kind: 'warning', message: r.reason || k.couldNotEstimate })
      }
    }
  })

  // A new task resets the cached estimate (the drawer reuses one instance).
  useEffect(() => setResult(null), [id])

  return (
    <Section label={k.estimate}>
      {result?.ok ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2 text-[0.8125rem]">
            <span className="font-medium tabular-nums text-(--ui-text-secondary)">
              ~{compactNumber(result.est_tokens)} {k.tokUnit}
            </span>
            {result.complexity && (
              <span className="text-(--ui-text-tertiary)">
                · {k.complexity[result.complexity] ?? result.complexity}
              </span>
            )}
            <Tip label={k.reEstimate}>
              <Button
                aria-label={k.reEstimate}
                className="ml-auto"
                disabled={est.isPending}
                onClick={() => est.mutate()}
                size="icon-xs"
                variant="ghost"
              >
                <Codicon name="refresh" size="0.75rem" spinning={est.isPending} />
              </Button>
            </Tip>
          </div>
          {result.rationale && (
            <p className="text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)">{result.rationale}</p>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Button disabled={est.isPending} onClick={() => est.mutate()} size="xs" variant="outline">
            <Codicon name={est.isPending ? 'loading' : 'dashboard'} size="0.75rem" spinning={est.isPending} />
            {est.isPending ? k.estimating : k.estimateEffort}
          </Button>
          <Tip label={k.estimateTipLong}>
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.makesModelCall}</span>
          </Tip>
        </div>
      )}
    </Section>
  )
}

// Left-rule accents for the two blocker halves — the board's own tones, so a
// gating blocker reads the same here as a blocked card does on the board and a
// satisfied one reads like a completed run.
const GATING_TONE = SEVERITY_TONE.error
const SATISFIED_TONE = columnMeta('running').tone

/**
 * One dependency, as a row you can read without opening it: status dot, status
 * pill, title, assignee, short id, and a remove button. The row body navigates;
 * the remove button stops propagation so cutting a link never also opens it.
 *
 * A `missing` link (an id the board cache can't see — deleted, or filtered out
 * by the current tenant/archive view) keeps its row and its remove button. A
 * dangling link is precisely the thing the user needs to be able to cut, so it
 * says so in muted text rather than disappearing.
 */
function DependencyRow({
  accent = 'transparent',
  link,
  onOpen,
  onUnlink
}: {
  accent?: string
  link: ResolvedLink
  onOpen: (id: string) => void
  onUnlink: () => void
}) {
  const k = useKanban()
  const meta = columnMeta(link.status)

  return (
    <li
      className="group/dep flex items-center gap-1.5 rounded-r pr-0.5 transition-colors hover:bg-(--chrome-action-hover)"
      style={{ borderLeft: `2px solid ${accent}` }}
    >
      <button
        className="flex min-w-0 flex-1 items-center gap-1.5 py-1 pl-1.5 text-left"
        onClick={() => onOpen(link.id)}
        type="button"
      >
        <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: meta.tone }} />
        {link.missing ? (
          <Tip label={k.depMissingTip}>
            <span className="min-w-0 flex-1 truncate text-[0.71rem] italic text-(--ui-text-quaternary)">
              {k.depMissing}
            </span>
          </Tip>
        ) : (
          <>
            <span
              className="shrink-0 rounded px-1 py-px text-[0.5625rem] font-semibold uppercase tracking-wide"
              style={{ backgroundColor: wash(meta.tone, 15), color: meta.tone }}
            >
              {columnLabel(k, link.status)}
            </span>
            <span className="min-w-0 flex-1 truncate text-[0.71rem] text-(--ui-text-secondary)" title={link.title}>
              {link.title || shortId(link.id)}
            </span>
          </>
        )}
        {link.assignee && <Avatar name={link.assignee} size="0.875rem" />}
        <span className="shrink-0 font-mono text-[0.5625rem] text-(--ui-text-quaternary)">{shortId(link.id)}</span>
      </button>
      <Tip label={k.depUnlinkTip}>
        <button
          aria-label={k.depUnlink}
          className="grid size-5 shrink-0 place-items-center rounded text-(--ui-text-quaternary) opacity-0 transition-[opacity,color] group-hover/dep:opacity-100 hover:text-destructive focus-visible:opacity-100"
          onClick={event => {
            event.stopPropagation()
            onUnlink()
          }}
          type="button"
        >
          <Codicon name="close" size="0.7rem" />
        </button>
      </Tip>
    </li>
  )
}

// The inline "link a blocker" picker: type to filter the board's own cards by
// title or id, click one to gate this task on it. Deliberately cache-only and
// in-file — it's a one-shot list, not a surface worth a component of its own.
function DependencyPicker({
  candidates,
  onCancel,
  onPick
}: {
  candidates: KanbanTask[]
  onCancel: () => void
  onPick: (id: string) => void
}) {
  const k = useKanban()
  const [query, setQuery] = useState('')

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase()

    return candidates
      .filter(
        candidate =>
          !needle || candidate.title.toLowerCase().includes(needle) || candidate.id.toLowerCase().includes(needle)
      )
      .slice(0, 8)
  }, [candidates, query])

  return (
    <div className="flex flex-col gap-1 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-tertiary) p-1.5">
      <Input
        autoFocus
        onChange={event => setQuery(event.target.value)}
        onKeyDown={event => event.key === 'Escape' && onCancel()}
        placeholder={k.filterCards}
        size="xs"
        value={query}
      />
      {matches.length > 0 ? (
        <ul className="flex flex-col">
          {matches.map(candidate => (
            <li key={candidate.id}>
              <button
                className="flex w-full min-w-0 items-center gap-1.5 rounded px-1 py-1 text-left transition-colors hover:bg-(--chrome-action-hover)"
                onClick={() => onPick(candidate.id)}
                type="button"
              >
                <span
                  className="size-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: columnMeta(candidate.status).tone }}
                />
                <span className="min-w-0 flex-1 truncate text-[0.71rem] text-(--ui-text-secondary)">
                  {candidate.title || candidate.id}
                </span>
                <span className="shrink-0 font-mono text-[0.5625rem] text-(--ui-text-quaternary)">
                  {shortId(candidate.id)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-1 py-1.5 text-[0.6875rem] text-(--ui-text-quaternary)">{k.noMatch}</p>
      )}
      <Button className="self-end" onClick={onCancel} size="xs" variant="ghost">
        {k.cancel}
      </Button>
    </div>
  )
}

/**
 * DEPENDENCIES — the chain, readable without opening every linked card.
 *
 * Blockers (`links.parents`) are split into the ones still holding the gate and
 * the ones already satisfied; the subgroup headers only appear when BOTH halves
 * exist, because a flat list is calmer when every blocker still gates.
 * Dependants (`links.children`) follow as a plain group.
 *
 * Resolution is CACHE-ONLY: ids are matched against the board the query cache
 * already holds, never a new fetch. The drawer doesn't own the archived toggle
 * (the board page does), so the lookup walks the non-archived key, then the
 * archived one, then any cached board for this slug. A miss is an acceptable
 * degraded state — those rows render as `missing` and can still be cut.
 */
export function DependenciesSection({
  board: taskBoard,
  detail,
  onLink,
  onOpen,
  onUnlink,
  slug,
  task
}: {
  /** The card's own board (All Boards mode), from the caller's board cache.
   *  NOT derivable from `detail` — `GET /tasks/:id` returns the task row as
   *  stored, and a board slug is not a column on it. */
  board?: string
  detail: KanbanTaskDetail
  onLink: (parentId: string) => void
  onOpen: (id: string) => void
  onUnlink: (parentId: string, childId: string) => void
  slug: string
  task: KanbanTaskFull
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const [picking, setPicking] = useState(false)

  // A new task closes an open picker (the drawer reuses one instance).
  useEffect(() => setPicking(false), [task.id])

  const board =
    qc.getQueryData<KanbanBoard>(boardKey(slug, false)) ??
    qc.getQueryData<KanbanBoard>(boardKey(slug, true)) ??
    qc.getQueriesData<KanbanBoard>({ queryKey: ['kanban', 'board', slug] }).find(([, data]) => !!data)?.[1]

  const index = useMemo(() => indexBoard(board), [board])
  // Links live within ONE board, so every id in `detail.links` belongs to this
  // task's board — resolve against that board's rows, not a same-id card from
  // somewhere else in the merged All Boards index.
  const blockers = resolveLinks(detail.links.parents, index, taskBoard)
  const dependants = resolveLinks(detail.links.children, index, taskBoard)
  const { gating, satisfied } = partitionBlockers(blockers)
  // Headers earn their place only when the split is real.
  const split = gating.length > 0 && satisfied.length > 0

  const candidates = useMemo(() => {
    const linked = new Set([task.id, ...detail.links.parents, ...detail.links.children])

    return [...index.values()].filter(
      candidate =>
        // In All Boards mode `index` is the MERGED cache, so an unfiltered list
        // offers foreign-board cards the write can never link: `linkTasks` pins
        // the request to the child's board and the backend rejects an id its DB
        // has never seen (400 "unknown task(s)"). Offer only same-board cards.
        // In single-board mode neither side carries a `board`, so this compares
        // undefined to undefined and every candidate stays offered.
        (candidate.board ?? undefined) === taskBoard && !linked.has(candidate.id)
    )
  }, [detail.links.children, detail.links.parents, index, task.id, taskBoard])

  return (
    <Section label={k.dependencies} tone={gating.length > 0 ? GATING_TONE : undefined}>
      {gating.length > 0 && (
        <Callout title={k.depWaitingBanner(gating.length, blockers.length)} tone={SEVERITY_TONE.warning} />
      )}

      {blockers.length > 0 && gating.length === 0 && (
        <Tip label={k.depClearTip}>
          <div className="flex items-center gap-1.5 text-[0.6875rem] font-medium" style={{ color: SATISFIED_TONE }}>
            <Codicon name="pass" size="0.75rem" />
            {k.depClear}
          </div>
        </Tip>
      )}

      {blockers.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.blockedBy}</div>
          {split && <div className={cn(FIELD_LABEL, 'pl-1.5')}>{k.depGating}</div>}
          {gating.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {gating.map(link => (
                <DependencyRow
                  accent={GATING_TONE}
                  key={link.id}
                  link={link}
                  onOpen={onOpen}
                  onUnlink={() => onUnlink(link.id, task.id)}
                />
              ))}
            </ul>
          )}
          {split && <div className={cn(FIELD_LABEL, 'pt-1 pl-1.5')}>{k.depSatisfied}</div>}
          {satisfied.length > 0 && (
            <ul className="flex flex-col gap-0.5">
              {satisfied.map(link => (
                <DependencyRow
                  accent={SATISFIED_TONE}
                  key={link.id}
                  link={link}
                  onOpen={onOpen}
                  onUnlink={() => onUnlink(link.id, task.id)}
                />
              ))}
            </ul>
          )}
        </div>
      )}

      {dependants.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.blocks}</div>
          <ul className="flex flex-col gap-0.5">
            {dependants.map(link => (
              <DependencyRow key={link.id} link={link} onOpen={onOpen} onUnlink={() => onUnlink(task.id, link.id)} />
            ))}
          </ul>
        </div>
      )}

      {picking ? (
        <DependencyPicker
          candidates={candidates}
          onCancel={() => setPicking(false)}
          onPick={id => {
            onLink(id)
            setPicking(false)
          }}
        />
      ) : (
        <button
          aria-label={k.parent}
          className="flex items-center justify-center gap-1 rounded-md border border-dashed border-(--ui-stroke-secondary) py-1 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:border-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-foreground"
          onClick={() => setPicking(true)}
          type="button"
        >
          <Codicon name="add" size="0.7rem" />
          <span className="truncate">{k.parent}</span>
        </button>
      )}
    </Section>
  )
}

/** `latest_summary` is just the newest non-null run summary. A reclaim writes
 *  an administrative note into that slot; hide those (Runs still shows them). */
export const isAdminSummary = (summary: string) => /^status changed to \w+ \(dashboard\/direct\)$/.test(summary)
