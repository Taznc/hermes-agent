import type { Unstable_TriggerAdapter } from '@assistant-ui/core'
import { ComposerPrimitive } from '@assistant-ui/react'
import type { ReactNode } from 'react'

import { composerPanelCard } from '@/components/chat/composer-dock'
import { cn } from '@/lib/utils'

// A standalone glassy panel floating just off the composer edge, inset from the
// left. Skin is the shared composerPanelCard (also used by the attach menu).
//
// The width TRACKS THE COMPOSER rather than being a fixed number. Every earlier
// value here (20rem, then 28rem, then 36rem) was a guess at one window size:
// each one measurably widened the panel, yet on a wide window the panel still
// stopped less than halfway across the composer and rows kept ellipsizing while
// a large empty gutter sat to the right. A description column that truncates
// beside unused space is the actual bug, and no constant fixes it for every
// window.
//
// The popover's containing block is `ComposerPrimitive.Root` (`relative w-full`
// in composer/index.tsx), so `100%` here IS the composer's width: the panel
// spans it minus the `left-2` inset and a matching right gutter. The `min()`
// caps it on very wide windows, where a full-bleed row would leave the eye
// travelling past dead space between a short label and its description.
const DRAWER_SHELL = cn(
  'absolute left-2 z-50 w-[min(64rem,calc(100%-1rem))] max-h-[min(22rem,calc(100vh-8rem))]',
  'p-1 text-popover-foreground',
  composerPanelCard
)

/** The panel IS the scroller: one scrolling column, nothing pinned. */
const DRAWER_SCROLLS = 'overflow-y-auto overscroll-contain'

/** The panel holds a scrolling list PLUS pinned furniture (the description
 *  footer), so the shell clips and the list inside does the scrolling —
 *  otherwise the footer scrolls away with the rows it describes. */
const DRAWER_COLUMN = 'flex flex-col overflow-hidden'

export const COMPLETION_DRAWER_CLASS = cn(DRAWER_SHELL, DRAWER_SCROLLS, 'bottom-full mb-1')

export const COMPLETION_DRAWER_BELOW_CLASS = cn(DRAWER_SHELL, DRAWER_SCROLLS, 'top-full mt-1')

export const COMPLETION_PANEL_CLASS = cn(DRAWER_SHELL, DRAWER_COLUMN, 'bottom-full mb-1')

export const COMPLETION_PANEL_BELOW_CLASS = cn(DRAWER_SHELL, DRAWER_COLUMN, 'top-full mt-1')

export function ComposerCompletionDrawer({
  adapter,
  ariaLabel,
  char,
  children
}: {
  adapter: Unstable_TriggerAdapter
  ariaLabel: string
  char: string
  children: ReactNode
}) {
  return (
    <ComposerPrimitive.Unstable_TriggerPopover
      adapter={adapter}
      aria-label={ariaLabel}
      char={char}
      className={COMPLETION_DRAWER_CLASS}
      data-slot="composer-completion-drawer"
    >
      {children}
    </ComposerPrimitive.Unstable_TriggerPopover>
  )
}

export function CompletionDrawerEmpty({ children, title }: { children?: ReactNode; title: string }) {
  return (
    <div className="px-3 py-3 text-xs text-(--ui-text-tertiary)">
      <p>{title}</p>
      {children && <p className="mt-1 text-xs text-(--ui-text-tertiary)">{children}</p>}
    </div>
  )
}
