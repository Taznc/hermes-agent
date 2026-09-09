/**
 * The drawer's call-to-action banner — the top-of-Overview answer to "why is
 * this stuck and what do I do about it", including the structured
 * multiple-choice question rendering (see
 * docs/design/blocked-callout-multiple-choice-spec.md) and the four-way
 * blocked-cause split (manual / automatic / review_no_verdict / unknown)
 * resolved by `resolveBlockCause` in `./status-guidance`.
 */

import { Button, cn, Codicon, CopyButton, host } from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'

import { latestBlockEvent } from './drawer_events'
import { resolveBlockCause, runErrorText } from './status-guidance'
import {
  type ChoiceResponse,
  columnMeta,
  type KanbanComment,
  type KanbanEvent,
  type KanbanRun,
  type KanbanTaskFull,
  SEVERITY_TONE
} from './types'
import { Banner, useKanban } from './ui'

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
 * Pulls copy-pasteable unblock commands out of a block reason. Workers are
 * instructed (KANBAN_GUIDANCE rule 4 + the kanban_block schema) to put the
 * exact command that unblocks them in a ```cmd fence; the board renders each
 * one as a monospace block with a copy button instead of burying it in prose.
 * `sh`/`bash`/`shell` are accepted as aliases so a worker that reflexively
 * writes ```bash still gets the affordance.
 */
export function parseCmdFences(text: string): { commands: string[]; prose: string } {
  const commands: string[] = []
  const prose = text
    .replace(/```(?:cmd|sh|bash|shell)\s*\n?([\s\S]*?)```/g, (_match, body: string) => {
      const trimmed = body.trim()

      if (trimmed) {
        commands.push(trimmed)
      }

      return ''
    })
    .replace(/\n{3,}/g, '\n\n')
    .trim()

  return { commands, prose }
}

/** How much post-first-line prose shows without a toggle. Short context stays
 *  inline; a wall of text collapses behind Show more so the card stays a card. */
const REASON_DETAIL_INLINE_CHARS = 220

/**
 * Structured rendering of a worker's block reason: the first line is THE ASK
 * and renders emphasized; any ```cmd fence becomes a copy-button command
 * block; remaining prose is detail, collapsed behind Show more when long.
 * This is the card-side half of the worker-protocol contract — even an
 * old-style prose-wall reason degrades into first-line + collapsed detail
 * instead of an unreadable paragraph.
 */
export function BlockReasonBody({ reason }: { reason: string }) {
  const k = useKanban()
  const [expanded, setExpanded] = useState(false)
  const { commands, prose } = parseCmdFences(reason)

  const newline = prose.indexOf('\n')
  const ask = newline === -1 ? prose : prose.slice(0, newline).trimEnd()
  const detail = newline === -1 ? '' : prose.slice(newline + 1).trim()
  const collapsible = detail.length > REASON_DETAIL_INLINE_CHARS

  return (
    <div className="flex flex-col gap-1.5">
      {ask && <p className="text-[0.78rem] leading-relaxed font-medium text-(--ui-text-primary)">{ask}</p>}
      {commands.map(command => (
        <div
          className="flex items-start gap-1 rounded-md bg-(--ui-bg-quaternary) py-1 pr-1 pl-2"
          key={command}
        >
          <code className="min-w-0 flex-1 self-center font-mono text-[0.6875rem] leading-relaxed break-all whitespace-pre-wrap text-(--ui-text-secondary)">
            {command}
          </code>
          <CopyButton appearance="icon" buttonSize="icon-xs" buttonVariant="ghost" text={command} />
        </div>
      ))}
      {detail && (!collapsible || expanded) && (
        <p className="text-[0.71rem] leading-relaxed whitespace-pre-wrap text-(--ui-text-tertiary)">{detail}</p>
      )}
      {collapsible && (
        <Button className="self-start" onClick={() => setExpanded(v => !v)} size="xs" variant="text">
          {expanded ? k.showLess : k.showMore}
        </Button>
      )}
    </div>
  )
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
 *
 * `onFocusComment` is a DEEP LINK, not a scroll: the comment composer lives on
 * the Activity tab, so the drawer's handler switches tabs first and focuses
 * once the input has mounted. Wiring it to a bare querySelector here would
 * make Reply a dead button whenever Overview is showing.
 *
 * `blocked` is not one state — `resolveBlockCause` (status-guidance.ts)
 * resolves it to one of four real origins, each with its own copy/actions:
 *  A. manual `kanban_block(reason=...)` — the worker's own words, verbatim
 *     (or parsed into clickable options when a ```choices fence is present).
 *  B. automatic circuit-breaker trip (gave_up/crashed/timed_out/...) — a
 *     structured failure, not a question: Retry + copy-log-command primary.
 *  C. a reviewer's run exited cleanly with no verdict — never a question.
 *  D. no cause found anywhere (born blocked / legacy edit) — honest,
 *     never-fabricated diagnostic.
 */
export function CtaBanner({
  comments,
  events,
  onFocusComment,
  onMove,
  onSubmitChoice,
  runs = [],
  task
}: {
  comments: KanbanComment[]
  events: KanbanEvent[]
  onFocusComment: () => void
  onMove: (status: string) => void
  onSubmitChoice: (body: string, choice: ChoiceResponse) => Promise<unknown>
  runs?: KanbanRun[]
  task: KanbanTaskFull
}) {
  const k = useKanban()

  const copyLogCommand = () => {
    void navigator.clipboard.writeText(`hermes kanban log ${task.id}`)
    host.notify({ kind: 'info', message: k.commandCopied })
  }

  if (task.status === 'blocked') {
    const cause = resolveBlockCause(task, events, runs)

    // C. A reviewer's claimed run exited cleanly without a verdict — never a
    // question for the user to answer, never framed as a missing reason.
    // The task is sticky here until an explicit unblock reopens it for
    // another review pass (kanban_db._has_sticky_block).
    if (cause.origin === 'review_no_verdict') {
      return (
        <Banner
          actions={
            <Button onClick={() => onMove('ready')} size="xs" variant="secondary">
              <Codicon name="debug-continue" size="0.7rem" />
              {k.ctaRequeueReview}
            </Button>
          }
          icon="eye"
          title={k.ctaReviewNoVerdictTitle}
          tone={columnMeta('review').tone}
        >
          <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{k.ctaReviewNoVerdictBody}</p>
        </Banner>
      )
    }

    // A. A worker's own typed kanban_block(reason=...) — authoritative,
    // shown verbatim, with the choices-fence path untouched.
    if (cause.origin === 'manual') {
      const kind = cause.kind
      const blockEvent = latestBlockEvent(events)
      const reason = cause.reason || null
      // A valid ```choices fence renders clickable options instead of the
      // plain paragraph; any missing/malformed fence falls back to the
      // plain-text + free-text-composer path (spec §5).
      const choices = reason ? parseBlockedChoices(reason) : null
      // needs_input reads as a literal question waiting on the user; the
      // other kinds (capability / transient / untyped legacy) are still
      // "blocked", just for a different reason — the icon + label change,
      // the actions don't.
      const icon = kind === 'needs_input' ? 'question' : kind === 'transient' ? 'sync' : 'error'
      const tone = kind === 'transient' ? columnMeta('review').tone : SEVERITY_TONE.error

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
          title={
            blockEvent?.intentionalInitialBlock
              ? k.ctaInitialBlockTitle
              : kind ? k.blockKind[kind] : k.ctaBlockedTitle
          }
          tone={tone}
        >
          {choices ? (
            <>
              {choices.prose && <BlockReasonBody reason={choices.prose} />}
              <ChoiceOptions
                comments={comments}
                onSubmit={onSubmitChoice}
                options={choices.options}
                prose={choices.prose}
                questionEventId={blockEvent!.id}
              />
            </>
          ) : reason ? (
            <BlockReasonBody reason={reason} />
          ) : (
            <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{k.ctaBlockedNoReason}</p>
          )}
        </Banner>
      )
    }

    // B2. The dispatcher's review-round cap — a scope/quality decision, not a
    // worker failure and not a missing reason. The reviewer's last feedback is
    // the cause and renders verbatim through the same structured body a manual
    // block uses. Retry is deliberately NOT primary: bouncing the card back to
    // Ready re-enters the loop the cap just stopped, so the intervention
    // (reassign/rescope) leads and the round counts frame why.
    if (cause.origin === 'review_round_cap') {
      return (
        <Banner
          actions={
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
          }
          icon="eye"
          title={
            cause.rounds != null && cause.max != null
              ? k.ctaReviewRoundCapTitleCounted(cause.rounds, cause.max)
              : k.ctaReviewRoundCapTitle
          }
          tone={SEVERITY_TONE.warning}
        >
          <div className="flex flex-col gap-1.5">
            <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{k.ctaReviewRoundCapBody}</p>
            {cause.reason && <BlockReasonBody reason={cause.reason} />}
          </div>
        </Banner>
      )
    }

    // B. Automatic circuit-breaker trip (gave_up / crashed / timed_out /
    // protocol_violation / rate_limited / stale) — this is NOT the worker
    // omitting a reason, and NOT a question for the user: it's a structured
    // failure with a real cause. Primary action is retry/reassign, not
    // reply — demoted to a secondary action here.
    if (cause.origin === 'automatic') {
      const { primary } = runErrorText(cause.raw, k)

      return (
        <Banner
          actions={
            <>
              <Button onClick={() => onMove('ready')} size="xs" variant="secondary">
                <Codicon name="debug-continue" size="0.7rem" />
                {k.ctaRetry}
              </Button>
              <Button onClick={copyLogCommand} size="xs" variant="outline">
                <Codicon name="copy" size="0.7rem" />
                {k.ctaCopyLogCommand}
              </Button>
              <Button onClick={onFocusComment} size="xs" variant="outline">
                <Codicon name="comment" size="0.7rem" />
                {k.ctaReply}
              </Button>
            </>
          }
          icon="error"
          title={k.ctaBlockedAutomaticTitle}
          tone={SEVERITY_TONE.error}
        >
          <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{primary}</p>
        </Banner>
      )
    }

    // D. No cause found anywhere (born blocked, or a legacy/direct-DB edit
    // with zero events) — honest generic diagnostic, never fabricated.
    return (
      <Banner
        actions={
          <>
            <Button onClick={() => onMove('ready')} size="xs" variant="secondary">
              <Codicon name="debug-continue" size="0.7rem" />
              {k.ctaRetry}
            </Button>
            <Button onClick={copyLogCommand} size="xs" variant="outline">
              <Codicon name="copy" size="0.7rem" />
              {k.ctaCopyLogCommand}
            </Button>
          </>
        }
        icon="error"
        title={k.ctaBlockedTitle}
        tone={SEVERITY_TONE.error}
      >
        <p className="text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">{k.ctaBlockedNoReason}</p>
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
