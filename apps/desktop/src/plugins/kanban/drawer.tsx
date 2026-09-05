/**
 * Task drawer — the desktop port of the dashboard's task detail, flat-styled:
 * status menu + meta table, DIAGNOSTICS (the "why is this stuck" panel, with
 * reassign recovery), description (editable), result/summary, dependencies,
 * comments (+composer), activity, run history, and the worker log tail.
 */

import {
  Badge,
  Button,
  cn,
  Codicon,
  compactNumber,
  CopyButton,
  Dialog,
  DialogContent,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  host,
  Input,
  Loader,
  LogView,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'

import {
  $boardSlug,
  addComment,
  boardKey,
  deleteTask,
  estimateTask,
  fetchAttachmentDataUrl,
  fetchLog,
  fetchProfiles,
  fetchTask,
  linkTasks,
  logKey,
  patchTask,
  PROFILES_KEY,
  reassignTask,
  reclaimTask,
  taskKey,
  unlinkTasks,
  uploadAttachment
} from './api'
import { indexBoard, partitionBlockers, resolveLinks } from './deps'
import { ModelOverrideField, overridePatch } from './model-override'
import {
  type ChoiceResponse,
  columnMeta,
  type Diagnostic,
  type DiagnosticAction,
  type KanbanAttachment,
  type KanbanBoard,
  type KanbanComment,
  type KanbanEvent,
  type KanbanTask,
  type KanbanTaskDetail,
  type KanbanTaskFull,
  type ResolvedLink,
  SEVERITY_TONE,
  type TaskEstimate
} from './types'
import {
  ago,
  Avatar,
  Banner,
  Callout,
  columnLabel,
  duration,
  errText,
  FIELD_LABEL,
  isLockedTarget,
  type KanbanText,
  lockedReason,
  ScrollFade,
  Section,
  shortId,
  StatusMenu,
  useDefaultAssignee,
  useKanban
} from './ui'

/**
 * Turn a task_events row into an operator-readable line. The backend logs
 * machine payloads ("status" + {"status":"ready"}); rendering the raw kind
 * made the feed useless ("status · 2 sec. ago" after a drag). Known kinds get
 * prose with the payload folded in; unknown kinds fall back to kind + compact
 * key=value detail so new backend events still say something.
 */
function eventText(event: KanbanEvent, k: KanbanText): { detail?: string; label: string } {
  let p: Record<string, unknown> = {}

  if (typeof event.payload === 'string' && event.payload) {
    try {
      p = JSON.parse(event.payload) as Record<string, unknown>
    } catch {
      return { label: event.kind.replace(/_/g, ' '), detail: event.payload }
    }
  } else if (event.payload && typeof event.payload === 'object') {
    p = event.payload as Record<string, unknown>
  }

  const str = (key: string): null | string => {
    const value = p[key]

    return typeof value === 'string' && value ? value : null
  }

  const col = (key: string) => {
    const value = str(key)

    return value ? columnLabel(k, value) : null
  }

  switch (event.kind) {
    case 'created':
      return { label: k.evtCreated(col('status') ?? '', str('assignee') ?? '') }
    case 'status': {
      const reason = str('reason')

      return {
        label: k.evtMovedTo(col('status') ?? '?'),
        detail: reason === 'parent_reopened' ? k.evtParentReopened(str('parent') ?? '') : (reason ?? undefined)
      }
    }

    case 'assigned': {
      const assignee = str('assignee')

      return { label: assignee ? k.evtAssignedTo(assignee) : k.evtUnassigned }
    }

    case 'commented':
      return { label: k.evtCommentBy(str('author') ?? k.someone) }

    case 'claimed':
      return { label: str('source_status') === 'review' ? k.evtClaimedReview : k.evtClaimedWorker }

    case 'spawned':
      return { label: k.evtWorkerStarted, detail: p.pid != null ? `pid ${p.pid}` : undefined }

    case 'completed':
      return { label: k.evtCompleted }

    case 'blocked':
      return { label: k.evtBlocked, detail: str('reason') ?? undefined }

    case 'unblocked':
      return { label: k.evtUnblocked(col('status') ?? '') }

    case 'reclaimed':
      return { label: k.evtReclaimed, detail: str('reason') ?? undefined }

    case 'specified':
      return { label: k.evtSpecified }

    case 'promoted':
      return { label: k.evtPromoted }

    case 'scheduled':
      return { label: k.evtScheduled, detail: str('reason') ?? undefined }

    case 'archived':
      return { label: k.evtArchived }

    case 'reprioritized':
      return { label: k.evtReprioritized(String(p.priority ?? '?')) }
    default: {
      const detail = Object.entries(p)
        .filter(([, value]) => value != null && typeof value !== 'object')
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(' ')

      return { label: event.kind.replace(/_/g, ' '), detail: detail || undefined }
    }
  }
}

function MetaRow({ children, label }: { children: ReactNode; label: string }) {
  return (
    <>
      <span className="text-(--ui-text-quaternary)">{label}</span>
      <span className="min-w-0 truncate text-(--ui-text-secondary)">{children}</span>
    </>
  )
}

/**
 * ACTIVITY · 71 rendering as six identical "heartbeat" rows is pure noise —
 * group consecutive events that render to the same label+detail into one
 * summarized row (expandable) instead of listing each one. Grouping on the
 * RENDERED text (not the raw `kind`) means two different kinds that happen
 * to read identically still collapse, and a kind whose detail changes
 * between events (e.g. two different comment authors) does NOT collapse —
 * exactly the granularity a human scanning the feed wants.
 */
export interface ActivityGroup {
  events: KanbanEvent[]
  label: string
  detail?: string
}

export function groupActivity(events: KanbanEvent[], k: KanbanText): ActivityGroup[] {
  const groups: ActivityGroup[] = []

  for (const event of events) {
    const { detail, label } = eventText(event, k)
    const last = groups[groups.length - 1]

    if (last && last.label === label && last.detail === detail) {
      last.events.push(event)
    } else {
      groups.push({ detail, events: [event], label })
    }
  }

  return groups
}

export function ActivityRow({ group, k }: { group: ActivityGroup; k: KanbanText }) {
  const [expanded, setExpanded] = useState(false)
  const latest = group.events[group.events.length - 1]

  if (group.events.length === 1) {
    return (
      <li className="flex items-baseline gap-2 text-[0.6875rem]">
        <span className="shrink-0 text-(--ui-text-secondary)">{group.label}</span>
        {group.detail && (
          <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)" title={group.detail}>
            {group.detail}
          </span>
        )}
        <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(latest.created_at)}</span>
      </li>
    )
  }

  return (
    <li className="flex flex-col gap-1">
      <button
        aria-expanded={expanded}
        className="flex w-full items-baseline gap-2 text-left text-[0.6875rem] text-(--ui-text-secondary) hover:text-(--ui-text-primary)"
        onClick={() => setExpanded(v => !v)}
        type="button"
      >
        <Codicon
          className={cn('shrink-0 transition-transform duration-150', expanded && 'rotate-90')}
          name="chevron-right"
          size="0.65rem"
        />
        <span className="shrink-0">{k.activityRun(group.label, group.events.length, ago(latest.created_at) ?? '')}</span>
        <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(latest.created_at)}</span>
      </button>
      {expanded && (
        <ul className="flex flex-col gap-1 border-l border-(--ui-stroke-tertiary) pl-3.5">
          {group.events.map(event => (
            <li className="flex items-baseline gap-2 text-[0.6875rem]" key={event.id}>
              <span className="shrink-0 text-(--ui-text-secondary)">{group.label}</span>
              {group.detail && (
                <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)" title={group.detail}>
                  {group.detail}
                </span>
              )}
              <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(event.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/**
 * The dispatcher writes machine-shaped diagnostics into `run.error`
 * (`stale_lock=hermes-dev:27237`, `pid 111279 not alive`) that a human
 * cannot act on unread. Recognized shapes get a plain-language primary line;
 * the raw string stays available behind an expand toggle rather than being
 * the primary text. Unrecognized shapes fall back to showing the raw string
 * as primary — there's nothing to translate.
 */
export function runErrorText(error: string, k: KanbanText): { primary: string; raw?: string } {
  const staleLock = /^stale_lock=(.+)$/.exec(error)

  if (staleLock) {
    return { primary: k.runErrStaleLock, raw: error }
  }

  const notAlive = /^pid \d+ not alive$/.exec(error)

  if (notAlive) {
    return { primary: k.runErrPidNotAlive, raw: error }
  }

  const exited = /^pid \d+ exited with code (.+)$/.exec(error)

  if (exited) {
    return { primary: k.runErrPidExited(exited[1]), raw: error }
  }

  const signaled = /^pid \d+ killed by signal (.+)$/.exec(error)

  if (signaled) {
    return { primary: k.runErrPidSignaled(signaled[1]), raw: error }
  }

  return { primary: error }
}

export function RunErrorLine({ error, k }: { error: string; k: KanbanText }) {
  const [expanded, setExpanded] = useState(false)
  const { primary, raw } = runErrorText(error, k)
  const showRaw = raw && raw !== primary

  return (
    <div className="flex flex-col gap-0.5">
      <p className="line-clamp-2 whitespace-pre-wrap text-destructive">{primary}</p>
      {showRaw && (
        <>
          <button
            aria-expanded={expanded}
            className="flex w-fit items-baseline gap-1 text-left text-[0.625rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)"
            onClick={() => setExpanded(v => !v)}
            type="button"
          >
            <Codicon
              className={cn('shrink-0 transition-transform duration-150', expanded && 'rotate-90')}
              name="chevron-right"
              size="0.6rem"
            />
            {k.runErrRaw}
          </button>
          {expanded && (
            <p className="whitespace-pre-wrap rounded bg-(--ui-bg-quaternary) px-1.5 py-1 font-mono text-[0.625rem] text-(--ui-text-quaternary)">
              {raw}
            </p>
          )}
        </>
      )}
    </div>
  )
}


/** The reason text on the most recent `blocked` event, if any — this is the
 *  worker's own explanation for why the task is stuck, surfaced verbatim in
 *  the CTA banner rather than making the user dig through Activity for it. */
export function latestBlockReason(events: KanbanEvent[]): null | string {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]

    if (event.kind === 'blocked' || event.kind === 'block_loop_detected') {
      const payload = event.payload

      if (typeof payload === 'string' && payload) {
        try {
          const parsed = JSON.parse(payload) as Record<string, unknown>
          const reason = parsed.reason

          return typeof reason === 'string' && reason ? reason : null
        } catch {
          return null
        }
      } else if (payload && typeof payload === 'object') {
        const reason = (payload as Record<string, unknown>).reason

        return typeof reason === 'string' && reason ? reason : null
      }

      return null
    }
  }

  return null
}

/** Same lookup as `latestBlockReason`, but also carries the event's id — the
 *  stable handle that binds a clicked answer to the specific question it
 *  answers (`ChoiceResponse.question_event_id`), so a re-block with a new
 *  question can never be confused with an old, already-answered one. */
function latestBlockEvent(events: KanbanEvent[]): null | { id: number; reason: string } {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]

    if (event.kind === 'blocked' || event.kind === 'block_loop_detected') {
      const reason = latestBlockReason([event])

      return reason ? { id: event.id, reason } : null
    }
  }

  return null
}

/** One clickable option in a blocked-callout multiple-choice question — see
 *  docs/design/blocked-callout-multiple-choice-spec.md §1. */
interface BlockedChoiceOption {
  key: string
  label: string
  description?: string
}

const MAX_CHOICES_FENCE_BYTES = 4096
const MIN_OPTIONS = 2
const MAX_OPTIONS = 6

function devWarn(message: string): void {
  if (import.meta.env.DEV) {
    console.warn(`[kanban] ${message}`)
  }
}

/**
 * Parses an optional fenced ```choices JSON block out of a blocked-task
 * `reason` string. Returns `null` whenever no fence is present or the fence
 * fails any validation rule — the caller must then fall back to rendering
 * `reason` as plain text exactly as it did before this feature existed.
 * Never throws: every failure path returns `null` (+ a dev-only console
 * warning), matching the spec's "malformed input never crashes" contract.
 */
export function parseBlockedChoices(reason: string): null | { options: BlockedChoiceOption[]; prose: string } {
  if (!reason) {
    return null
  }

  // The LAST fence in the string wins (a worker's prose could quote an
  // example fence earlier); scan all matches and keep the final one.
  const fenceRe = /```choices\s*([\s\S]*?)```/g
  let lastMatch: null | RegExpExecArray = null
  let match: null | RegExpExecArray

  while ((match = fenceRe.exec(reason))) {
    lastMatch = match
  }

  if (!lastMatch) {
    return null
  }

  const fenceBody = lastMatch[1]

  if (new TextEncoder().encode(fenceBody).length > MAX_CHOICES_FENCE_BYTES) {
    devWarn(`malformed choices fence: body exceeds ${MAX_CHOICES_FENCE_BYTES} bytes`)

    return null
  }

  let parsed: unknown

  try {
    parsed = JSON.parse(fenceBody)
  } catch (err) {
    devWarn(`malformed choices fence: invalid JSON (${String(err)})`)

    return null
  }

  if (!Array.isArray(parsed) || parsed.length < MIN_OPTIONS || parsed.length > MAX_OPTIONS) {
    devWarn(`malformed choices fence: expected an array of ${MIN_OPTIONS}-${MAX_OPTIONS} options`)

    return null
  }

  const options: BlockedChoiceOption[] = []
  const seenKeys = new Set<string>()

  for (let i = 0; i < parsed.length; i++) {
    const item = parsed[i] as unknown

    if (!item || typeof item !== 'object') {
      devWarn(`malformed choices fence: option ${i} is not an object`)

      return null
    }

    const { description, key, label } = item as Record<string, unknown>

    if (typeof key !== 'string' || !key) {
      devWarn(`malformed choices fence: option ${i} missing a non-empty "key"`)

      return null
    }

    if (typeof label !== 'string' || !label) {
      devWarn(`malformed choices fence: option ${i} missing a non-empty "label"`)

      return null
    }

    if (description !== undefined && typeof description !== 'string') {
      devWarn(`malformed choices fence: option ${i} has a non-string "description"`)

      return null
    }

    if (seenKeys.has(key)) {
      devWarn(`malformed choices fence: duplicate key "${key}"`)

      return null
    }

    seenKeys.add(key)
    options.push(description ? { description, key, label } : { key, label })
  }

  const prose = reason.slice(0, lastMatch.index).trimEnd()

  return { options, prose }
}

/**
 * The clickable option list rendered in place of a plain-text CTA banner
 * paragraph when `parseBlockedChoices` finds a valid option set. A single-
 * select ARIA radiogroup with roving tabindex (see spec §4): arrow keys move
 * focus + selection, Enter/Space submits (native <button> semantics — no
 * extra key handling needed for that part). One shared submit path handles
 * both pointer and keyboard activation.
 */
function ChoiceOptions({
  comments,
  onSubmit,
  options,
  prose,
  questionEventId
}: {
  comments: KanbanComment[]
  onSubmit: (body: string, choice: ChoiceResponse) => Promise<unknown>
  options: BlockedChoiceOption[]
  prose: string
  questionEventId: number
}) {
  const k = useKanban()
  const listRef = useRef<HTMLDivElement>(null)
  const [focusIndex, setFocusIndex] = useState(0)
  const [pendingKey, setPendingKey] = useState<null | string>(null)
  const [errorKey, setErrorKey] = useState<null | string>(null)
  // Sticky local confirmation so the UI doesn't flash back to "unanswered"
  // between a successful submit and the subsequent comment-list refetch.
  const [optimisticKey, setOptimisticKey] = useState<null | string>(null)

  const answeredComment = comments.find(
    comment => comment.choice && comment.choice.question_event_id === questionEventId
  )

  const submittedKey = answeredComment?.choice?.key ?? optimisticKey
  const isSubmitted = submittedKey != null

  const isDisabled = (option: BlockedChoiceOption) =>
    isSubmitted || pendingKey !== null || (errorKey !== null && errorKey !== option.key)

  const focusDomIndex = (index: number) => {
    requestAnimationFrame(() => {
      const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]')

      buttons?.[index]?.focus()
    })
  }

  const moveFocus = (delta: number) => {
    if (options.every(isDisabled)) {
      return
    }

    let next = focusIndex

    for (let i = 0; i < options.length; i++) {
      next = (next + delta + options.length) % options.length

      if (!isDisabled(options[next])) {
        break
      }
    }

    setFocusIndex(next)
    focusDomIndex(next)
  }

  const submit = (option: BlockedChoiceOption) => {
    if (isDisabled(option)) {
      return
    }

    setErrorKey(null)
    setPendingKey(option.key)

    void onSubmit(`${option.key}) ${option.label}`, {
      key: option.key,
      label: option.label,
      question_event_id: questionEventId
    }).then(
      () => {
        setPendingKey(null)
        setOptimisticKey(option.key)
      },
      () => {
        setPendingKey(null)
        setErrorKey(option.key)
      }
    )
  }

  return (
    <div
      aria-label={prose || k.choicesGroupLabel}
      className="flex flex-col gap-1.5"
      ref={listRef}
      role="radiogroup"
    >
      {options.map((option, index) => {
        const checked = submittedKey === option.key
        const isPending = pendingKey === option.key
        const isError = errorKey === option.key
        const disabled = isDisabled(option)

        return (
          <button
            aria-checked={checked}
            className={cn(
              'flex flex-col gap-0.5 rounded-md px-2.5 py-2 text-left text-[0.75rem] shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-stroke-secondary)_50%,transparent)] transition-colors outline-none focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:cursor-default',
              checked
                ? 'bg-(--ui-bg-quaternary) text-(--ui-text-primary) shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-text-secondary)_35%,transparent)]'
                : 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
              disabled && !checked && !isError && 'opacity-50'
            )}
            disabled={disabled}
            key={option.key}
            onClick={() => submit(option)}
            onKeyDown={event => {
              if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
                event.preventDefault()
                moveFocus(1)
              } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
                event.preventDefault()
                moveFocus(-1)
              } else if (event.key === 'Home') {
                event.preventDefault()
                setFocusIndex(0)
                focusDomIndex(0)
              } else if (event.key === 'End') {
                event.preventDefault()
                setFocusIndex(options.length - 1)
                focusDomIndex(options.length - 1)
              }
            }}
            role="radio"
            tabIndex={index === focusIndex ? 0 : -1}
            type="button"
          >
            <span className="flex items-center gap-2">
              <span className="shrink-0 rounded bg-(--ui-bg-quaternary) px-1.5 py-0.5 font-mono text-[0.625rem] text-(--ui-text-tertiary)">
                {option.key}
              </span>
              <span className="min-w-0 flex-1">{option.label}</span>
              {checked && <Codicon className="shrink-0" name="check" size="0.8rem" />}
              {isPending && <Codicon className="shrink-0" name="loading" size="0.8rem" spinning />}
            </span>
            {option.description && (
              <span className="pl-[1.9rem] text-[0.6875rem] text-(--ui-text-quaternary)">{option.description}</span>
            )}
            {isError && (
              <span className="pl-[1.9rem] text-[0.6875rem] text-destructive">
                {k.choiceSubmitError} · {k.choiceRetry}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/**
 * The task detail view's top-of-drawer call to action. This is the answer to
 * "why is this stuck and what do I do about it" — rendered once, above
 * everything else, whenever the task needs a human decision right now
 * (blocked, needs an answer, or parked in review). Everything else in the
 * drawer stays informational; this is the only thing asking for action.
 */
export function CtaBanner({
  comments,
  events,
  onFocusComment,
  onMove,
  onSubmitChoice,
  task
}: {
  comments: KanbanComment[]
  events: KanbanEvent[]
  onFocusComment: () => void
  onMove: (status: string) => void
  onSubmitChoice: (body: string, choice: ChoiceResponse) => Promise<unknown>
  task: KanbanTaskFull
}) {
  const k = useKanban()

  if (task.status === 'blocked') {
    const kind = (task.block_kind ?? null) as null | 'capability' | 'needs_input' | 'transient'
    const blockEvent = latestBlockEvent(events)
    const reason = blockEvent?.reason ?? null
    // A valid ```choices fence renders clickable options instead of the plain
    // paragraph; any missing/malformed fence falls back to today's exact
    // plain-text + free-text-composer path (spec §5).
    const choices = reason ? parseBlockedChoices(reason) : null
    // needs_input reads as a literal question waiting on the user; the other
    // kinds (capability / transient / untyped legacy) are still "blocked",
    // just for a different reason — the icon + label change, the actions don't.
    const icon = kind === 'needs_input' ? 'question' : kind === 'transient' ? 'sync' : 'error'
    const tone = kind === 'transient' ? '#fbbf24' : SEVERITY_TONE.error

    return (
      <Banner
        actions={
          choices ? (
            <Button onClick={() => onMove('ready')} size="xs" variant="outline">
              <Codicon name="debug-continue" size="0.7rem" />
              {k.ctaUnblock}
            </Button>
          ) : (
            <>
              <Button onClick={onFocusComment} size="xs" variant="secondary">
                <Codicon name="comment" size="0.7rem" />
                {k.ctaReply}
              </Button>
              <Button onClick={() => onMove('ready')} size="xs" variant="outline">
                <Codicon name="debug-continue" size="0.7rem" />
                {k.ctaUnblock}
              </Button>
            </>
          )
        }
        icon={icon}
        title={kind ? k.blockKind[kind] : k.ctaBlockedTitle}
        tone={tone}
      >
        {choices ? (
          <>
            {choices.prose && (
              <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{choices.prose}</p>
            )}
            <ChoiceOptions
              comments={comments}
              onSubmit={onSubmitChoice}
              options={choices.options}
              prose={choices.prose}
              questionEventId={blockEvent!.id}
            />
          </>
        ) : (
          <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">
            {reason || k.ctaBlockedNoReason}
          </p>
        )}
      </Banner>
    )
  }

  if (task.status === 'review') {
    return (
      <Banner
        actions={
          <>
            <Button onClick={() => onMove('done')} size="xs" variant="secondary">
              <Codicon name="check" size="0.7rem" />
              {k.ctaApprove}
            </Button>
            <Button onClick={() => onMove('ready')} size="xs" variant="outline">
              <Codicon name="discard" size="0.7rem" />
              {k.ctaSendBack}
            </Button>
          </>
        }
        icon="eye"
        title={k.ctaReviewTitle}
        tone={columnMeta('review').tone}
      >
        <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{k.ctaReviewBody}</p>
      </Banner>
    )
  }

  return null
}


/** The dashboard's diagnostics panel: severity-toned, plain-English, with the
 *  backend's structured recovery actions as buttons. `reassign` is skipped —
 *  the Assignee control in the meta table IS that action, inline. */
function Diagnostics({ items, onReclaim }: { items: Diagnostic[]; onReclaim: () => void }) {
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
function AssigneeMenu({
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

// Mirrors the review pane's commit-message field: one row tall to start
// (button-height), CSS field-sizing grows it with content, button hugs the
// bottom edge as it grows.
//
// On a RUNNING task the worker polls its comment thread and folds new notes
// into the live turn (OUT-OF-BAND steer), so a plain note reaches the agent
// mid-run within a few seconds — no block/unblock dance. `onRequeue` is the
// heavier option: post the note AND reclaim so the task restarts from scratch
// with the note in context (use when the current run has gone off the rails).
function CommentComposer({
  onRequeue,
  onSubmit,
  pending,
  running
}: {
  onRequeue?: (body: string) => void
  onSubmit: (body: string) => void
  pending: boolean
  running?: boolean
}) {
  const k = useKanban()
  const [body, setBody] = useState('')

  const submit = () => {
    const trimmed = body.trim()

    if (trimmed && !pending) {
      onSubmit(trimmed)
      setBody('')
    }
  }

  const requeue = () => {
    const trimmed = body.trim()

    if (trimmed && !pending && onRequeue) {
      onRequeue(trimmed)
      setBody('')
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="relative">
        <Textarea
          className={cn('field-sizing-content max-h-40 min-h-0 resize-none', running ? 'pr-[3.5rem]' : 'pr-[5rem]')}
          data-kanban-comment-input="true"
          onChange={event => setBody(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
          placeholder={running ? k.messageWorker : k.addComment}
          rows={1}
          size="sm"
          value={body}
        />
        <Button
          className="absolute top-1 right-1"
          disabled={!body.trim() || pending}
          onClick={submit}
          size="xs"
          variant="secondary"
        >
          {running ? k.send : k.comment}
        </Button>
      </div>
      {running && onRequeue && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.625rem] leading-tight text-(--ui-text-quaternary)">{k.deliveredLive}</span>
          <Button className="shrink-0" disabled={!body.trim() || pending} onClick={requeue} size="xs" variant="outline">
            <Codicon name="debug-restart" size="0.7rem" />
            {k.requeueWithNote}
          </Button>
        </div>
      )}
    </div>
  )
}

function DescriptionSection({ body, onSave }: { body: null | string | undefined; onSave: (body: string) => void }) {
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
        <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{body}</p>
      ) : (
        <p className="text-[0.8125rem] text-(--ui-text-quaternary)">{k.noDescription}</p>
      )}
    </Section>
  )
}

// `latest_summary` is just the newest non-null run summary. A reclaim writes an
// administrative note into that slot; hide those (Runs still shows them).
const isAdminSummary = (summary: string) => /^status changed to \w+ \(dashboard\/direct\)$/.test(summary)

export const isImageAttachment = (a: KanbanAttachment) => (a.content_type ?? '').startsWith('image/')

/** One image tile: fetches its bytes lazily as a base64 data URL (the
 *  desktop plugin host has no authenticated `<img src>` door — REST goes
 *  over the Electron IPC JSON bridge) and swaps in a broken-image
 *  placeholder on either a fetch failure or a decode failure, never a
 *  crash (#cae4c2ba acceptance: broken/missing images handled gracefully). */
export function ImageThumb({
  attachment,
  onOpen
}: {
  attachment: KanbanAttachment
  onOpen: (filename: string, src: string) => void
}) {
  const k = useKanban()
  const [decodeFailed, setDecodeFailed] = useState(false)

  const { data, isError, isLoading } = useQuery({
    queryFn: () => fetchAttachmentDataUrl(attachment.id),
    queryKey: ['kanban', 'attachment-data-url', attachment.id],
    retry: false,
    staleTime: Infinity
  })

  const broken = isError || decodeFailed
  const src = data?.data_url

  return (
    <Tip label={broken ? k.brokenImage : attachment.filename}>
      <button
        aria-label={broken ? k.brokenImage : k.openImage}
        className="grid size-16 shrink-0 place-items-center overflow-hidden rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-quaternary) text-(--ui-text-quaternary) transition-colors hover:border-(--ui-stroke-secondary)"
        disabled={!src || broken}
        onClick={() => src && !broken && onOpen(attachment.filename, src)}
        type="button"
      >
        {broken ? (
          <Codicon name="warning" size="1rem" />
        ) : src ? (
          <img
            alt={attachment.filename}
            className="size-full object-cover"
            onError={() => setDecodeFailed(true)}
            src={src}
          />
        ) : (
          <Codicon name="sync" size="0.9rem" spinning={isLoading} />
        )}
      </button>
    </Tip>
  )
}

/** Image strip above the generic Attachments/Files section — every task
 *  attachment whose content_type starts with `image/`. Click to enlarge in
 *  a lightbox. */
export function ImagesSection({
  attachments,
  onOpen
}: {
  attachments: KanbanAttachment[]
  onOpen: (filename: string, src: string) => void
}) {
  const k = useKanban()

  if (attachments.length === 0) {
    return null
  }

  return (
    <Section label={k.images(attachments.length)}>
      <div className="flex flex-wrap gap-2">
        {attachments.map(attachment => (
          <ImageThumb attachment={attachment} key={attachment.id} onOpen={onOpen} />
        ))}
      </div>
    </Section>
  )
}

function AttachmentsSection({
  attachments,
  onUpload,
  pending
}: {
  attachments: KanbanAttachment[]
  onUpload: (file: File) => void
  pending: boolean
}) {
  const k = useKanban()
  const fileRef = useRef<HTMLInputElement>(null)

  return (
    <Section
      action={
        <>
          <input
            hidden
            onChange={event => {
              const file = event.target.files?.[0]

              if (file) {
                onUpload(file)
              }

              event.target.value = ''
            }}
            ref={fileRef}
            type="file"
          />
          <Button
            aria-label={k.uploadAttachment}
            disabled={pending}
            onClick={() => fileRef.current?.click()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name={pending ? 'sync' : 'cloud-upload'} size="0.8rem" spinning={pending} />
          </Button>
        </>
      }
      label={k.attachments(attachments.length)}
    >
      {attachments.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {attachments.map(attachment => (
            <li className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)" key={attachment.id}>
              <Codicon name="file" size="0.75rem" />
              {attachment.filename}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noAttachments}</p>
      )}
    </Section>
  )
}

// Rough effort estimate via the auxiliary (auto-routed) model. Tokens +
// complexity, never dollars — providers don't report cost reliably. Gated
// behind an explicit click + disclaimer since it makes a model call. The
// control keeps a stable footprint (spinner swaps in place) so there's no
// layout jump when it runs.
function EstimateSection({ id }: { id: string }) {
  const k = useKanban()
  const [result, setResult] = useState<null | TaskEstimate>(null)

  const est = useMutation({
    mutationFn: () => estimateTask(id),
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

// Left-rule accents for the two blocker halves — the board's own red/green, so
// a gating blocker reads the same here as a blocked card does on the board.
const GATING_TONE = SEVERITY_TONE.error
const SATISFIED_TONE = '#34d399'

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
              style={{ backgroundColor: `color-mix(in srgb, ${meta.tone} 15%, transparent)`, color: meta.tone }}
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
function DependenciesSection({
  detail,
  onLink,
  onOpen,
  onUnlink,
  slug,
  task
}: {
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
  const blockers = resolveLinks(detail.links.parents, index)
  const dependants = resolveLinks(detail.links.children, index)
  const { gating, satisfied } = partitionBlockers(blockers)
  // Headers earn their place only when the split is real.
  const split = gating.length > 0 && satisfied.length > 0

  const candidates = useMemo(() => {
    const linked = new Set([task.id, ...detail.links.parents, ...detail.links.children])

    return [...index.values()].filter(candidate => !linked.has(candidate.id))
  }, [detail.links.children, detail.links.parents, index, task.id])

  return (
    <Section label={k.dependencies}>
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
              <DependencyRow
                key={link.id}
                link={link}
                onOpen={onOpen}
                onUnlink={() => onUnlink(task.id, link.id)}
              />
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

const DEFAULT_LOG_TAIL_BYTES = 16_384
const MAX_LOG_TAIL_BYTES = 1_048_576 // 1 MiB — well under the backend's 2 MiB rotation size.
const FAILED_OUTCOMES = ['crashed', 'failed', 'timed_out', 'gave_up']

export function TaskDrawer({
  columns,
  id,
  onClose,
  onOpen
}: {
  columns: string[]
  id: null | string
  onClose: () => void
  onOpen: (id: string) => void
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const [lightbox, setLightbox] = useState<null | { filename: string; src: string }>(null)

  // Socket-invalidated (bindApi); the interval is only the socketless heartbeat.
  const { data: detail, error } = useQuery({
    enabled: !!id,
    queryFn: () => fetchTask(id!),
    queryKey: taskKey(slug, id ?? ''),
    refetchInterval: 30_000
  })

  const task = detail?.task
  const running = task?.status === 'running'
  const defaultAssignee = useDefaultAssignee()

  // "Show more" widens the requested tail instead of leaving a bare `...]` —
  // the acceptance criteria want a visible affordance, not silent truncation.
  // Resets to the default whenever the drawer switches to a different task.
  const [logTail, setLogTail] = useState(DEFAULT_LOG_TAIL_BYTES)

  useEffect(() => setLogTail(DEFAULT_LOG_TAIL_BYTES), [id])

  const { data: log } = useQuery({
    enabled: !!id,
    queryFn: () => fetchLog(id!, logTail),
    queryKey: logKey(slug, id ?? '', logTail),
    refetchInterval: running ? 3_000 : 15_000
  })

  // Esc closes the drawer even though it isn't modal (no backdrop to click off).
  useEffect(() => {
    if (!id) {
      return
    }

    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [id, onClose])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: taskKey(slug, id!) })
    void qc.invalidateQueries({ queryKey: ['kanban', 'board', slug] })
  }

  // Optimistic status change against the task cache; rolls back + toasts on a
  // rejected transition (the backend enforces the workflow).
  const moveMut = useMutation({
    mutationFn: (status: string) => patchTask(id!, { status }),
    onMutate: async status => {
      await qc.cancelQueries({ queryKey: taskKey(slug, id!) })
      const previous = qc.getQueryData<KanbanTaskDetail>(taskKey(slug, id!))

      if (previous) {
        qc.setQueryData(taskKey(slug, id!), { ...previous, task: { ...previous.task, status } })
      }

      return { previous }
    },
    onError: (err, _status, context) => {
      if (context?.previous) {
        qc.setQueryData(taskKey(slug, id!), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: invalidate
  })

  const mutate = (fn: () => Promise<unknown>, onDone?: () => void) => () =>
    fn().then(
      () => {
        invalidate()
        onDone?.()
      },
      (err: unknown) => host.notify({ kind: 'error', message: errText(err) })
    )

  const commentMut = useMutation({
    mutationFn: ({ body, choice }: { body: string; choice?: ChoiceResponse }) => addComment(id!, body, choice),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  // "Note & requeue" for a running task: post the note, then reclaim so the
  // dispatcher re-runs it with the note in the worker's context — the one-click
  // replacement for the block → comment → unblock dance.
  const requeueMut = useMutation({
    mutationFn: async (body: string) => {
      await addComment(id!, body)
      await reclaimTask(id!)
    },
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: () => {
      host.notify({ kind: 'info', message: k.notePosted })
      invalidate()
    }
  })

  // Priority-only PATCH — never touches status/title/body/assignee, so a
  // failed toggle can't be mistaken for a bigger write going wrong.
  const priorityMut = useMutation({
    mutationFn: (priority: number) => patchTask(id!, { priority }),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  const uploadMut = useMutation({
    mutationFn: async (file: File) =>
      uploadAttachment(id!, {
        bytes: await file.arrayBuffer(),
        contentType: file.type || undefined,
        filename: file.name
      }),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  if (!id) {
    return null
  }

  const errorMessage = error ? errText(error) : null
  const runsFailed = detail?.runs.filter(run => FAILED_OUTCOMES.includes(run.outcome ?? run.status)).length ?? 0

  const move = (status: string) => {
    if (!task || status === task.status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: lockedReason(k, status) })

      return
    }

    moveMut.mutate(status)
  }

  return (
    <div className="absolute inset-y-0 right-0 z-20 flex w-[26rem] flex-col border-l border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) duration-150 ease-out animate-in fade-in slide-in-from-right-4">
      <header className="flex flex-col gap-2 px-4 pt-3.5 pb-3">
        <div className="flex items-center gap-2">
          {task ? (
            <StatusMenu columns={columns} onMove={move} status={task.status} />
          ) : (
            <span className="font-mono text-sm text-(--ui-text-tertiary)">{shortId(id)}</span>
          )}
          {task && (
            <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)" data-selectable-text="true">
              {shortId(task.id)}
            </span>
          )}
          <div className="ml-auto flex items-center gap-0.5">
            {task && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    aria-label={k.taskActions}
                    className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
                    type="button"
                  >
                    <Codicon name="ellipsis" size="0.9rem" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    onSelect={() =>
                      void priorityMut.mutate(typeof task.priority === 'number' && task.priority > 0 ? 0 : 1)
                    }
                  >
                    <Codicon
                      name={typeof task.priority === 'number' && task.priority > 0 ? 'star-full' : 'star-empty'}
                      size="0.85rem"
                    />
                    {typeof task.priority === 'number' && task.priority > 0 ? k.removeHighPriority : k.markHighPriority}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.id)
                      host.notify({ kind: 'info', message: k.copiedId(task.id) })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTaskId}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.title || task.id)
                      host.notify({ kind: 'info', message: k.copiedTitle })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTitle}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={mutate(() => patchTask(task.id, { status: 'archived' }), onClose)}>
                    <Codicon name="archive" size="0.85rem" />
                    {k.archive}
                  </DropdownMenuItem>
                  <DropdownMenuItem className="text-destructive" onSelect={mutate(() => deleteTask(task.id), onClose)}>
                    <Codicon name="trash" size="0.85rem" />
                    {k.delete}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <button
              aria-label={k.close}
              className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
              onClick={onClose}
              type="button"
            >
              <Codicon name="close" size="0.9rem" />
            </button>
          </div>
        </div>
        {task && (
          <h2
            className="flex items-center gap-1.5 text-sm leading-snug font-semibold text-foreground"
            data-selectable-text="true"
          >
            {typeof task.priority === 'number' && task.priority > 0 && (
              <Tip label={k.highPriorityTip}>
                <span className="shrink-0 text-amber-500">
                  <Codicon name="star-full" size="0.8rem" />
                </span>
              </Tip>
            )}
            {task.title || task.id}
          </h2>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4" data-selectable-text="true">
        {errorMessage ? (
          <ErrorState title={errorMessage} />
        ) : !detail || !task ? (
          <div className="grid h-32 place-items-center">
            <Loader type="lemniscate-bloom" />
          </div>
        ) : (
          <div className="flex flex-col gap-4 text-sm">
            <CtaBanner
              comments={detail.comments}
              events={detail.events}
              onFocusComment={() => {
                const el = document.querySelector<HTMLElement>('[data-kanban-comment-input="true"]')

                el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
                el?.focus()
              }}
              onMove={move}
              onSubmitChoice={(body, choice) => commentMut.mutateAsync({ body, choice })}
              task={task}
            />

            <div className="flex flex-col gap-1.5 opacity-80">
              <div className={FIELD_LABEL}>{k.metaSectionLabel}</div>
              <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-[0.71rem]">
                <MetaRow label={k.assignee}>
                  <AssigneeMenu
                    current={task.assignee}
                    onReassign={profile => void mutate(() => reassignTask(task.id, profile))()}
                  />
                </MetaRow>
                {typeof task.priority === 'number' && <MetaRow label={k.metaPriority}>{task.priority}</MetaRow>}
                {task.tenant && <MetaRow label={k.metaTenant}>{task.tenant}</MetaRow>}
                {task.workspace_path && (
                  <MetaRow label={k.workspace}>
                    {task.workspace_kind ? `${task.workspace_kind}: ` : ''}
                    {task.workspace_path}
                  </MetaRow>
                )}
                <MetaRow label={k.model}>
                  <ModelOverrideField
                    onChange={next => void mutate(() => patchTask(task.id, overridePatch(next)))()}
                    value={{
                      effort: task.reasoning_effort ?? '',
                      model: task.model_override ?? '',
                      provider: task.provider_override ?? ''
                    }}
                  />
                </MetaRow>
                {task.created_by && <MetaRow label={k.metaCreatedBy}>{task.created_by}</MetaRow>}
                {ago(task.created_at) && <MetaRow label={k.metaCreated}>{ago(task.created_at)}</MetaRow>}
                {running && task.worker_pid ? <MetaRow label={k.metaWorkerPid}>{task.worker_pid}</MetaRow> : null}
              </div>
            </div>

            {task.status === 'ready' && !task.assignee && !defaultAssignee && (
              <Callout title={k.readyUnassignedTitle} tone={SEVERITY_TONE.warning}>
                <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">{k.readyUnassignedBody}</p>
              </Callout>
            )}

            {task.diagnostics && task.diagnostics.length > 0 && (
              <Section label={k.diagnosticsN(task.diagnostics.length)}>
                <Diagnostics items={task.diagnostics} onReclaim={() => void mutate(() => reclaimTask(task.id))()} />
              </Section>
            )}

            <DescriptionSection body={task.body} onSave={body => void mutate(() => patchTask(task.id, { body }))()} />

            <EstimateSection id={task.id} />

            {task.result && (
              <Section label={k.result}>
                <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{task.result}</p>
              </Section>
            )}

            {task.latest_summary && !isAdminSummary(task.latest_summary) && (
              <Section label={k.latestSummary}>
                <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{task.latest_summary}</p>
              </Section>
            )}

            <DependenciesSection
              detail={detail}
              onLink={parentId => void mutate(() => linkTasks(parentId, task.id))()}
              onOpen={onOpen}
              onUnlink={(parentId, childId) => void mutate(() => unlinkTasks(parentId, childId))()}
              slug={slug}
              task={task}
            />

            <Section
              action={
                <Tip label={running ? k.commentsHelpRunning : k.commentsHelp}>
                  <span className="grid size-5 place-items-center rounded text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)">
                    <Codicon name="question" size="0.8rem" />
                  </span>
                </Tip>
              }
              label={k.comments(detail.comments.length)}
            >
              {detail.comments.length > 0 && (
                <ul className="flex flex-col gap-2">
                  {detail.comments.map(comment => (
                    <li className="text-[0.75rem]" key={comment.id}>
                      <span className="font-medium text-(--ui-text-secondary)">{comment.author}</span>
                      <span className="ml-2 text-[0.625rem] text-(--ui-text-quaternary)">
                        {ago(comment.created_at)}
                      </span>
                      <p className="whitespace-pre-wrap text-(--ui-text-tertiary)">{comment.body}</p>
                    </li>
                  ))}
                </ul>
              )}
              <CommentComposer
                onRequeue={body => requeueMut.mutate(body)}
                onSubmit={body => commentMut.mutate({ body })}
                pending={commentMut.isPending || requeueMut.isPending}
                running={running}
              />
            </Section>

            {detail.events.length > 0 && (
              <Section label={k.activity(detail.events.length)}>
                <ScrollFade deps={detail.events.length} max="7rem">
                  <ul className="flex flex-col gap-1">
                    {groupActivity(detail.events, k).map(group => (
                      <ActivityRow group={group} k={k} key={group.events[0].id} />
                    ))}
                  </ul>
                </ScrollFade>
              </Section>
            )}

            {detail.runs.length > 0 && (
              <Section
                action={
                  runsFailed > 0 ? (
                    <Badge size="xs" variant="destructive">
                      {k.runsFailedCount(runsFailed)}
                    </Badge>
                  ) : undefined
                }
                label={k.runs(detail.runs.length)}
              >
                <ScrollFade max="11rem">
                  <ul className="flex flex-col gap-1.5">
                    {detail.runs.map(run => {
                      const failed = FAILED_OUTCOMES.includes(run.outcome ?? run.status)

                      return (
                        <li className="flex flex-col gap-0.5 text-[0.71rem]" key={run.id}>
                          <div className="flex items-center gap-2">
                            <Badge size="xs" variant={failed ? 'destructive' : 'muted'}>
                              {run.outcome ?? run.status}
                            </Badge>
                            {run.profile && <span className="text-(--ui-text-tertiary)">{run.profile}</span>}
                            {duration(run.started_at, run.ended_at) && (
                              <span className="text-(--ui-text-quaternary)">
                                {duration(run.started_at, run.ended_at)}
                              </span>
                            )}
                            <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">
                              {ago(run.ended_at ?? run.started_at)}
                            </span>
                          </div>
                          {run.error ? (
                            <RunErrorLine error={run.error} k={k} />
                          ) : (
                            run.summary && (
                              <p className="line-clamp-2 whitespace-pre-wrap text-(--ui-text-quaternary)">
                                {run.summary}
                              </p>
                            )
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </ScrollFade>
              </Section>
            )}

            {log?.exists && log.content && (
              <Section
                action={
                  <CopyButton
                    appearance="icon"
                    buttonSize="icon-xs"
                    buttonVariant="ghost"
                    text={() => log.content}
                  />
                }
                label={log.truncated ? k.workerLogTail : k.workerLog}
              >
                <ScrollFade deps={log.content.length} max="16rem">
                  <LogView className="border-0 px-0" content={log.content} numbered />
                </ScrollFade>
                {log.truncated && logTail < MAX_LOG_TAIL_BYTES && (
                  <Button
                    className="self-start"
                    onClick={() => setLogTail(t => Math.min(t * 4, MAX_LOG_TAIL_BYTES))}
                    size="xs"
                    variant="text"
                  >
                    {k.workerLogShowMore}
                  </Button>
                )}
              </Section>
            )}

            <ImagesSection attachments={detail.attachments.filter(isImageAttachment)} onOpen={(filename, src) => setLightbox({ filename, src })} />

            <AttachmentsSection
              attachments={detail.attachments.filter(a => !isImageAttachment(a))}
              onUpload={file => uploadMut.mutate(file)}
              pending={uploadMut.isPending}
            />
          </div>
        )}
      </div>

      <Dialog onOpenChange={open => !open && setLightbox(null)} open={!!lightbox}>
        <DialogContent
          bodyClassName="block overflow-visible p-0"
          className="w-auto max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] border-0 bg-transparent shadow-none"
          showCloseButton={false}
        >
          {lightbox && (
            <img
              alt={lightbox.filename}
              className="block max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] cursor-zoom-out rounded-lg object-contain shadow-2xl"
              onClick={() => setLightbox(null)}
              onError={() => setLightbox(null)}
              src={lightbox.src}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
