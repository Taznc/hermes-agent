import { MessagePrimitive, useAuiState } from '@assistant-ui/react'
import { type FC, useState } from 'react'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { messageContentText } from '@/components/assistant-ui/thread/content'
import { MessageTimelineTimestamp } from '@/components/assistant-ui/thread/timeline-timestamp'
import { SCAFFOLD_LABEL_CLASS } from '@/components/chat/scaffold-row'
import { Codicon } from '@/components/ui/codicon'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { ToolIcon } from '@/components/ui/tool-icon'
import { useI18n } from '@/i18n'
import { LinkifiedText } from '@/lib/external-link'
import { cn } from '@/lib/utils'
import type { ReviewActionRecord } from '@/types/hermes'

const SLASH_STATUS_RE = /^slash:(?<command>\/[^\n]+)\n(?<output>[\s\S]*)$/
const STEER_NOTE_RE = /^steer:(?<text>[\s\S]+)$/
const REVIEW_NOTE_RE = /^review:(?<label>[^:\n]+):?\s*(?<detail>[\s\S]*)$/

// Glyph per operation verb — mirrors the ➕/✏️/➖ prefixes the backend's
// verbose-mode compact summary already uses (agent/background_review.py),
// so the expanded row reads consistently with anyone who also sees the
// CLI/TUI's plain-text form.
const OPERATION_ICON: Record<string, string> = {
  add: 'add',
  create: 'add',
  replace: 'edit',
  patch: 'edit',
  edit: 'edit',
  remove: 'trash'
}

/** One compact, individually expandable review record. */
function ReviewActionRow({ action }: { action: ReviewActionRecord }) {
  const { t } = useI18n()
  const copy = t.assistant.thread.review
  const [open, setOpen] = useState(false)
  const state = action.state ?? (action.success ? 'completed' : 'failed')
  const icon = OPERATION_ICON[action.operation] ?? 'circle'
  const targetLabel = copy.target(action.target)
  const target = action.skill_name ? `${targetLabel} “${action.skill_name}”` : targetLabel
  const operation = copy.operation(action.operation)
  const stateLabel = copy.state(state)
  const hasSafeDetail = Boolean(action.change_summary || action.reason)

  return (
    <li className="min-w-0 py-0.5">
      <button
        aria-expanded={open}
        aria-label={open ? copy.hideRecordDetails : copy.showRecordDetails(target)}
        className="flex min-w-0 items-start gap-1.5 bg-transparent text-left"
        onClick={() => setOpen(value => !value)}
        type="button"
      >
        <span className="flex h-(--conversation-line-height) w-3 shrink-0 items-center justify-center">
          <Codicon
            className={state === 'failed' ? 'text-destructive' : 'text-(--ui-text-tertiary)'}
            name={state === 'failed' ? 'warning' : icon}
            size="0.75rem"
          />
        </span>
        <span className="min-w-0 wrap-anywhere text-[0.6875rem] leading-5 text-muted-foreground/80">
          <span className="font-medium text-muted-foreground">{copy.recordSummary(target, operation, stateLabel)}</span>
        </span>
        <DisclosureCaret className="mt-1 shrink-0 text-muted-foreground/55" open={open} size="0.625rem" />
      </button>
      {open && (
        <div className="ml-4.5 mt-0.5 min-w-0 wrap-anywhere text-[0.6875rem] leading-5 text-muted-foreground/70">
          {hasSafeDetail ? (
            <>
              {action.change_summary && <p>{action.change_summary}</p>}
              {action.reason && (
                <p className={state === 'failed' ? 'text-destructive/90' : undefined}>{action.reason}</p>
              )}
            </>
          ) : (
            <p>{copy.legacyDetail}</p>
          )}
        </div>
      )}
    </li>
  )
}

/**
 * The self-improvement review's per-action detail list, behind a disclosure
 * caret next to the summary row. Collapsed by default so an ordinary "saved
 * something" glance doesn't grow the transcript; opens to show exactly which
 * memory/skill mutations happened, including failed/skipped attempts, so the
 * user never has to trust an opaque one-line summary (ROADMAP.md Phase 1).
 */
function ReviewActionsDisclosure({ actions }: { actions: ReviewActionRecord[] }) {
  const { t } = useI18n()
  const copy = t.assistant.thread.review
  const [open, setOpen] = useState(false)

  const failedCount = actions.filter(
    action => (action.state ?? (action.success ? 'completed' : 'failed')) === 'failed'
  ).length

  return (
    <span className="ml-1 inline-flex items-center align-middle">
      <button
        aria-expanded={open}
        className={cn(
          SCAFFOLD_LABEL_CLASS,
          'inline-flex items-center gap-1 bg-transparent text-muted-foreground/55 transition-colors hover:text-foreground'
        )}
        onClick={() => setOpen(value => !value)}
        type="button"
      >
        {open ? copy.hideDetails : failedCount > 0 ? copy.showDetailsWithFailures(failedCount) : copy.showDetails}
        <DisclosureCaret className="text-muted-foreground/55" open={open} size="0.625rem" />
      </button>
      {open && (
        <ul className="mt-1 block w-full list-none space-y-0.5 pl-0">
          {actions.map((action, index) => (
            // Records have no stable id; the review pass emits them once and
            // the list never reorders in place, so positional key is safe.
            <ReviewActionRow action={action} key={index} />
          ))}
        </ul>
      )}
    </span>
  )
}

export const SystemMessage: FC = () => {
  const text = useAuiState(s => messageContentText(s.message.content))
  const asyncResult = useAuiState(s => s.message.metadata.custom?.asyncResult)

  const reviewActions = useAuiState(s => {
    const custom = (s.message.metadata?.custom ?? {}) as { reviewActions?: ReviewActionRecord[] }

    return custom.reviewActions
  })

  if (!text) {
    return null
  }

  if (typeof asyncResult === 'string' && asyncResult) {
    return (
      <MessagePrimitive.Root
        className="flex w-full min-w-0 flex-col gap-2 self-start py-1"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <div className="text-[0.6875rem] leading-5 text-muted-foreground/55">
          {text} <MessageTimelineTimestamp />
        </div>
        <MarkdownTextContent isRunning={false} text={asyncResult} />
      </MessagePrimitive.Root>
    )
  }

  // The self-improvement review saved something to memory/skills — the same
  // kind of event as a landed `memory` write, so it wears the same chrome:
  // brain glyph with the gold→purple glow, gradient label, purple detail,
  // left-aligned in the reading column like every other scaffold line.
  const reviewNote = text.match(REVIEW_NOTE_RE)

  if (reviewNote?.groups) {
    const detail = reviewNote.groups.detail.trim()

    return (
      <MessagePrimitive.Root
        className="flex w-full min-w-0 max-w-full flex-wrap items-start gap-1.5 self-start py-0.5"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <span className="tool-memory-legendary-glyph flex h-(--conversation-line-height) w-3.5 shrink-0 items-center justify-center">
          <ToolIcon className="text-(--tool-memory-legendary-icon)" name="brain" size="0.875rem" />
        </span>
        <span className={cn(SCAFFOLD_LABEL_CLASS, 'tool-memory-legendary-title shrink-0 text-transparent')}>
          {reviewNote.groups.label.trim()}
        </span>
        {detail && (
          <span className={cn(SCAFFOLD_LABEL_CLASS, 'tool-memory-legendary-meta min-w-0 wrap-anywhere')}>{detail}</span>
        )}
        {reviewActions?.length ? <ReviewActionsDisclosure actions={reviewActions} /> : null}
      </MessagePrimitive.Root>
    )
  }

  const steerNote = text.match(STEER_NOTE_RE)

  if (steerNote?.groups) {
    return (
      <MessagePrimitive.Root
        className="flex max-w-[min(86%,44rem)] items-center gap-1.5 self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/60"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <Codicon className="text-muted-foreground/55" name="compass" size="0.75rem" />
        <span className="text-muted-foreground/55">steered</span>
        <span className="text-muted-foreground/35">·</span>
        <span className="whitespace-pre-wrap">{steerNote.groups.text.trim()}</span> <MessageTimelineTimestamp />
      </MessagePrimitive.Root>
    )
  }

  const slashStatus = text.match(SLASH_STATUS_RE)

  if (slashStatus?.groups) {
    const output = slashStatus.groups.output.trim()
    // Single-line status (e.g. "model → x") reads best centered inline; padded
    // multiline output (catalogs, usage tables) needs left-aligned, wider room
    // or the column alignment breaks.
    const multiline = output.includes('\n')

    return (
      <MessagePrimitive.Root
        className={cn(
          'w-[60%] max-w-[44rem] self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/60',
          multiline ? 'text-left' : 'text-center'
        )}
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <span className="font-mono text-muted-foreground/55">{slashStatus.groups.command}</span>
        {multiline ? (
          <LinkifiedText className="mt-0.5 block whitespace-pre-wrap" explicitOnly pretty={false} text={output} />
        ) : (
          <>
            <span className="mx-1.5 text-muted-foreground/35">·</span>
            <LinkifiedText className="whitespace-pre-wrap" explicitOnly pretty={false} text={output} />
          </>
        )}{' '}
        <MessageTimelineTimestamp className={cn(multiline ? 'mt-0.5 block' : 'ml-1.5')} />
      </MessagePrimitive.Root>
    )
  }

  const multiline = text.includes('\n')

  return (
    <MessagePrimitive.Root
      className={cn(
        'w-[60%] max-w-[44rem] self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/55',
        multiline ? 'text-left' : 'text-center'
      )}
      data-role="system"
      data-slot="aui_system-message-root"
    >
      <LinkifiedText className="whitespace-pre-wrap" explicitOnly pretty={false} text={text} />{' '}
      <MessageTimelineTimestamp className={cn(multiline ? 'mt-0.5 block' : 'ml-1.5')} />
    </MessagePrimitive.Root>
  )
}
