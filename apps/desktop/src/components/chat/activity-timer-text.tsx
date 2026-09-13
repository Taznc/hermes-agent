import { cn } from '@/lib/utils'

import { formatElapsed } from './activity-timer'
import { StableText } from './stable-text'

interface ActivityTimerTextProps {
  seconds: number
  className?: string
}

/**
 * The elapsed clock beside a live row.
 *
 * The default is the LIVE case — the activity strip, where the number is the
 * thing a waiting user is actually looking for — so it is sized and lit to be
 * read at a glance. Settled rows (a finished tool call, a delegation card, a
 * reasoning block) are records rather than signals, and they pass
 * `SCAFFOLD_META_CLASS` to step it back down to the meta scale; `cn`'s
 * tailwind-merge lets a caller's size/colour win over these.
 *
 * It used to default to `text-[0.56rem]` at 55% alpha — ~9px, then multiplied
 * again by the 0.67 scaffold fade on the row around it. That is below the
 * threshold where digits resolve at a glance, which is the whole job.
 */
export function ActivityTimerText({ seconds, className }: ActivityTimerTextProps) {
  return (
    <StableText
      className={cn(
        'shrink-0 text-[length:var(--activity-strip-meta-font-size)] leading-none font-semibold',
        'tabular-nums tracking-[0.01em] text-(--activity-strip-meta)',
        className
      )}
    >
      {formatElapsed(seconds)}
    </StableText>
  )
}
