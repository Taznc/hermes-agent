'use client'

import { type ReactNode, useCallback, useRef, useState } from 'react'

import { useResizeObserver } from '@/hooks/use-resize-observer'
import { useI18n } from '@/i18n'
import { ChevronDown } from '@/lib/icons'
import { cn } from '@/lib/utils'

interface ExpandableBlockProps {
  children: ReactNode
  className?: string
}

export function ExpandableBlock({ children, className }: ExpandableBlockProps) {
  const { t } = useI18n()
  const innerRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)

  // Measure inside ResizeObserver timing only (layout is clean there). A
  // synchronous mount-time scrollHeight read forces a reflow per instance,
  // and a tool-heavy transcript mounts dozens of these on a session switch.
  const measure = useCallback(() => {
    const el = innerRef.current

    if (el) {
      setOverflowing(el.scrollHeight > 121)
    }
  }, [])

  useResizeObserver(measure, innerRef)

  return (
    <div className="relative">
      <div
        className={cn(
          // `scrollbar-overlay` opts out of the app-wide classic thin gutters so
          // this scroller keeps platform overlay bars (no always-on track).
          'scrollbar-overlay overflow-y-auto overflow-x-auto',
          expanded ? 'max-h-[40dvh]' : 'max-h-[7.5rem]',
          className
        )}
        ref={innerRef}
      >
        {children}
      </div>
      {overflowing && (
        // Pure overflow cue, decorative only: hints more content sits below
        // the visible area. Never carries the click target and never
        // intercepts pointer events, so it can't fight the scrollbar drag or
        // text selection on the last visible line underneath it.
        <div
          className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-linear-to-t from-[var(--expandable-fade-from,var(--ui-chat-surface-background))] to-transparent"
          data-testid="expandable-fade"
        />
      )}
      {overflowing && (
        // The toggle lives OUTSIDE the scrollable box entirely, in its own
        // full-width row below it — never on top of the scroll container.
        // The old placement pinned a small icon-only button inside the
        // scroller's own bottom-right corner: exactly where a vertical
        // scrollbar (right edge) and a horizontal scrollbar (bottom edge, for
        // wide code) both live, so the native scrollbar constantly ate the
        // click. Living below the box, at full width, this control can never
        // overlap either scrollbar regardless of scroll position, code
        // width, or viewport size — and the much larger hit area (full card
        // width) plus a visible hover fill make it easy to find and click.
        <button
          aria-expanded={expanded}
          aria-label={expanded ? t.common.collapse : t.common.expand}
          className={cn(
            'flex w-full cursor-pointer items-center justify-center gap-1 border-t py-1 text-[0.6875rem]',
            'border-(--ui-stroke-tertiary)/50 bg-[var(--expandable-fade-from,var(--ui-chat-surface-background))]',
            'text-muted-foreground/70 transition-colors hover:bg-accent/40 hover:text-foreground',
            'focus-visible:bg-accent/40 focus-visible:text-foreground focus-visible:outline-none'
          )}
          onClick={() => setExpanded(v => !v)}
          type="button"
        >
          <span>{expanded ? t.common.collapse : t.common.expand}</span>
          <ChevronDown className={cn('size-3.5 transition-transform', expanded && 'rotate-180')} />
        </button>
      )}
    </div>
  )
}
