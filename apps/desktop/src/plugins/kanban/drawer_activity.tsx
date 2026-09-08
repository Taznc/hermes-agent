/**
 * The drawer's ACTIVITY tab: the event feed, run history, comments, and the
 * comment composer. Everything here is "what happened / what do I say back".
 */

import { Badge, Button, cn, Codicon, Textarea, Tip } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { type ActivityGroup, isFailedRun, runErrorText } from './drawer_events'
import { eventTone, type KanbanComment, type KanbanRun, outcomeTone, SEVERITY_TONE } from './types'
import { AccentRow, ago, duration, type KanbanText, ScrollFade, Section, useKanban, wash } from './ui'

/**
 * One row in the activity feed. Consecutive identical events collapse into a
 * single expandable row (see `groupActivity`).
 *
 * The row's tone comes from `eventTone(kind)`, which deliberately leaves
 * heartbeats — by far the highest-volume kind on a long-running card —
 * uncolored and unwashed, so color still means "look here".
 */
export function ActivityRow({ group, k }: { group: ActivityGroup; k: KanbanText }) {
  const [expanded, setExpanded] = useState(false)
  const latest = group.events[group.events.length - 1]
  const kind = latest.kind
  const tone = eventTone(kind)
  // A grouped run of identical events is by definition repetitive chatter;
  // never let it wash a block of the feed.
  const quiet = tone === 'var(--ui-text-quaternary)' || group.events.length > 1

  if (group.events.length === 1) {
    return (
      <AccentRow className="flex items-baseline gap-2 text-[0.6875rem]" quiet={quiet} tone={tone}>
        <span className="shrink-0 text-(--ui-text-secondary)">{group.label}</span>
        {group.detail && (
          <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)" title={group.detail}>
            {group.detail}
          </span>
        )}
        <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(latest.created_at)}</span>
      </AccentRow>
    )
  }

  return (
    <AccentRow className="flex flex-col gap-1" quiet tone={tone}>
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
        <ul className="flex flex-col gap-1 pl-3.5">
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
    </AccentRow>
  )
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

/** One run, tinted by its outcome so a crashed or blocked run is findable by
 *  color in a long history. The tone comes from `outcomeTone`, which resolves
 *  through COLUMN_META / SEVERITY_TONE — never a hand-picked value. */
export function RunRow({ k, run }: { k: KanbanText; run: KanbanRun }) {
  const outcome = run.outcome ?? run.status
  const tone = outcomeTone(outcome)
  const modelParts = [run.model, run.provider, run.reasoning_effort].filter((value): value is string => value != null)

  const tokenParts = [
    run.input_tokens != null ? `in ${run.input_tokens.toLocaleString()}` : null,
    run.output_tokens != null ? `out ${run.output_tokens.toLocaleString()}` : null,
    run.cache_read_tokens != null ? `cache ${run.cache_read_tokens.toLocaleString()}` : null,
    run.reasoning_tokens != null ? `reasoning ${run.reasoning_tokens.toLocaleString()}` : null
  ].filter((value): value is string => value != null)

  const usageParts = [
    run.api_calls != null ? `API ${run.api_calls.toLocaleString()}` : null,
    run.tool_calls != null ? `tools ${run.tool_calls.toLocaleString()}` : null,
    run.estimated_cost_usd != null ? `estimated cost: $${run.estimated_cost_usd.toFixed(4)}` : null
  ].filter((value): value is string => value != null)

  return (
    <AccentRow className="flex flex-col gap-0.5 py-1 text-[0.71rem]" tone={tone}>
      <div className="flex items-center gap-2">
        <span
          className="shrink-0 rounded px-1 py-px text-[0.5625rem] font-semibold uppercase tracking-wide"
          style={{ backgroundColor: wash(tone, 15), color: tone }}
        >
          {outcome}
        </span>
        {run.profile && <span className="text-(--ui-text-tertiary)">{run.profile}</span>}
        {duration(run.started_at, run.ended_at) && (
          <span className="text-(--ui-text-quaternary)">{duration(run.started_at, run.ended_at)}</span>
        )}
        <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(run.ended_at ?? run.started_at)}</span>
      </div>
      {modelParts.length > 0 && (
        <p className="text-(--ui-text-quaternary)">model: {modelParts.join(' · ')}</p>
      )}
      {tokenParts.length > 0 && (
        <p className="text-(--ui-text-quaternary)">tokens: {tokenParts.join(' · ')}</p>
      )}
      {usageParts.length > 0 && (
        <p className="text-(--ui-text-quaternary)">calls: {usageParts.join(' · ')}</p>
      )}
      {run.error ? (
        <RunErrorLine error={run.error} k={k} />
      ) : (
        run.summary && <p className="line-clamp-2 whitespace-pre-wrap text-(--ui-text-quaternary)">{run.summary}</p>
      )}
    </AccentRow>
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
export function CommentComposer({
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

/** The comment thread + composer. The composer carries the
 *  `data-kanban-comment-input` hook the CTA banner's Reply deep-link focuses,
 *  which is why this whole section must be MOUNTED (not just reachable) once
 *  the Activity tab is selected.
 *
 *  The thread is bounded by its own scroller. A long review conversation runs
 *  to thousands of pixels, and left unbounded it buries the event feed and run
 *  history under it — which would defeat the whole point of giving Activity
 *  its own tab. */
export function CommentsSection({
  comments,
  onRequeue,
  onSubmit,
  pending,
  running
}: {
  comments: KanbanComment[]
  onRequeue: (body: string) => void
  onSubmit: (body: string) => void
  pending: boolean
  running: boolean
}) {
  const k = useKanban()

  return (
    <Section
      action={
        <Tip label={running ? k.commentsHelpRunning : k.commentsHelp}>
          <span className="grid size-5 place-items-center rounded text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)">
            <Codicon name="question" size="0.8rem" />
          </span>
        </Tip>
      }
      label={k.comments(comments.length)}
    >
      <CommentComposer onRequeue={onRequeue} onSubmit={onSubmit} pending={pending} running={running} />
      {comments.length > 0 && (
        <ScrollFade deps={comments.length} max="20rem">
          <ul className="flex flex-col gap-2">
            {comments.map(comment => (
              <li className="text-[0.75rem]" key={comment.id}>
                <span className="font-medium text-(--ui-text-secondary)">{comment.author}</span>
                <span className="ml-2 text-[0.625rem] text-(--ui-text-quaternary)">{ago(comment.created_at)}</span>
                <p className="whitespace-pre-wrap text-(--ui-text-tertiary)">{comment.body}</p>
              </li>
            ))}
          </ul>
        </ScrollFade>
      )}
    </Section>
  )
}

/** Run history, newest state first, each row outcome-tinted. */
export function RunsSection({ runs }: { runs: KanbanRun[] }) {
  const k = useKanban()
  const failed = runs.filter(run => isFailedRun(run.outcome ?? run.status)).length

  if (runs.length === 0) {
    return null
  }

  return (
    <Section
      action={
        failed > 0 ? (
          <Badge size="xs" variant="destructive">
            {k.runsFailedCount(failed)}
          </Badge>
        ) : undefined
      }
      label={k.runs(runs.length)}
      tone={failed > 0 ? SEVERITY_TONE.error : undefined}
    >
      <ScrollFade max="13rem">
        <ul className="flex flex-col gap-1.5">
          {runs.map(run => (
            <RunRow k={k} key={run.id} run={run} />
          ))}
        </ul>
      </ScrollFade>
    </Section>
  )
}
