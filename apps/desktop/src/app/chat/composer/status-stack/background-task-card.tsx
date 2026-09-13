import { memo, useState } from 'react'

import { useElapsedSeconds } from '@/components/chat/activity-timer'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { LogView } from '@/components/ui/log-view'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import type { ComposerStatusItem } from '@/store/composer-status'

interface BackgroundTaskCardProps {
  item: ComposerStatusItem
  onDismiss?: (id: string) => void
  onStop?: (id: string) => void
}

/** A compact task summary that opens into its own process detail card. */
export const BackgroundTaskCard = memo(function BackgroundTaskCard({ item, onDismiss, onStop }: BackgroundTaskCardProps) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const running = item.state === 'running'
  const elapsed = useElapsedSeconds(running, `background:${item.id}`, item.startedAt)
  const action = running ? onStop && { label: t.statusStack.stop, onClick: onStop } : onDismiss && { label: t.statusStack.dismiss, onClick: onDismiss }

  return (
    <div className="overflow-hidden rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-quaternary)/35">
      <div className="flex min-w-0 items-center gap-1 pr-1">
        <button
          aria-expanded={expanded}
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left hover:bg-(--ui-row-hover-background)"
          onClick={() => setExpanded(open => !open)}
          type="button"
        >
          <span className="flex size-3.5 shrink-0 items-center justify-center">
            {running ? (
              <GlyphSpinner ariaLabel={t.statusStack.running} className="text-[0.85rem] leading-none text-muted-foreground/80" spinner="braille" />
            ) : (
              <Codicon
                className={item.state === 'failed' ? 'text-destructive/90' : 'text-emerald-500/80'}
                name={item.state === 'failed' ? 'error' : 'pass-filled'}
                size="0.8rem"
              />
            )}
          </span>
          <span className="min-w-0 flex-1 truncate text-[0.73rem] leading-4 text-foreground/92">{item.title}</span>
          <ActivityTimerText className="text-[0.68rem] text-muted-foreground/85" seconds={elapsed} />
          <DisclosureCaret className="shrink-0 text-muted-foreground/70" open={expanded} size="0.85rem" />
        </button>
        {action && (
          <Tip label={action.label}>
            <Button
              aria-label={action.label}
              className="size-5 text-muted-foreground/65 hover:text-foreground/90"
              onClick={() => action.onClick(item.id)}
              size="icon-xs"
              type="button"
              variant="ghost"
            >
              <Codicon name="close" size="0.75rem" />
            </Button>
          </Tip>
        )}
      </div>
      {expanded && (
        <div className="space-y-1.5 border-t border-(--ui-stroke-tertiary) px-2 py-2">
          <div className="flex min-w-0 items-center gap-2 text-[0.65rem] text-muted-foreground/80">
            {item.cwd && <code className="min-w-0 flex-1 truncate">{item.cwd}</code>}
            {item.pid && <span className="shrink-0 font-mono tabular-nums">#{item.pid}</span>}
            {item.state === 'failed' && typeof item.exitCode === 'number' && item.exitCode !== 0 && (
              <span className="shrink-0 rounded bg-destructive/15 px-1 font-semibold text-destructive">{t.statusStack.exit(item.exitCode)}</span>
            )}
          </div>
          {item.output && <LogView className={cn('max-h-32 text-[0.65rem]')}>{item.output}</LogView>}
        </div>
      )}
    </div>
  )
})