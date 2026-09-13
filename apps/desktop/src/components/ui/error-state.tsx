import {
  Children,
  cloneElement,
  type ComponentProps,
  isValidElement,
  type ReactElement,
  type ReactNode
} from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { AlertTriangle } from '@/lib/icons'
import { cn } from '@/lib/utils'

// The single canonical error glyph (codicon's filled error mark). Use this
// everywhere an error is surfaced (boundaries, dialogs, banners) so failures
// read identically — one icon, one color, no background chip.
export function ErrorIcon({ className, size = '1.75rem' }: { className?: string; size?: string }) {
  return <Codicon className={cn('text-destructive', className)} name="error" size={size} />
}

// Inline error banner for detail panes (born in Messaging's platform error,
// now shared with the MCP config pane): warn glyph + tinted rounded box.
// For centered full-surface failures use ErrorState below instead.
export function ErrorBanner({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-destructive',
        className
      )}
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
      <span className="min-w-0 whitespace-pre-wrap break-words">{children}</span>
    </div>
  )
}

export interface ErrorStateProps {
  /** Optional actions row/stack rendered below the copy. */
  children?: ReactNode
  className?: string
  description?: ReactNode
  /** Defaults to a destructive AlertCircle. */
  icon?: ReactNode
  title: ReactNode
}

// Shared, presentation-only error layout: the canonical ErrorIcon (no bg chip)
// over a centered title + body, with an optional actions stack. Used by the
// React error boundary, the in-dialog update error, and the boot-failure banner
// so every failure reads the same. Title/description accept nodes so Radix
// Dialog callers can pass DialogTitle/DialogDescription for accessibility.
export function ErrorState({ children, className, description, icon, title }: ErrorStateProps) {
  return (
    <div className={cn('grid gap-5', className)}>
      <div className="flex flex-col items-center gap-3 text-center">
        {icon ?? <ErrorIcon />}

        {typeof title === 'string' ? (
          <h2 className="text-center text-xl font-semibold tracking-tight">{title}</h2>
        ) : (
          title
        )}

        {typeof description === 'string' ? (
          <p className="max-w-prose text-center text-sm leading-5 text-muted-foreground">{description}</p>
        ) : (
          description
        )}
      </div>

      {/* Default grid stretch keeps arbitrary content blocks (a scrollable
          crash log `<pre>`, status text, ...) bounded to the surface width so
          they still own their own overflow. Only direct `Button` children —
          the actual action controls — are pulled to their natural width and
          centered (`justify-self-center`), so a lone Retry reads as a button
          instead of a full-bleed bar without shrink-wrapping everything else
          in the slot. */}
      {children && (
        <div className="grid gap-2">
          {Children.map(children, child => (isButtonElement(child) ? centerButton(child) : child))}
        </div>
      )}
    </div>
  )
}

function isButtonElement(child: ReactNode): child is ReactElement<ComponentProps<typeof Button>> {
  return isValidElement(child) && child.type === Button
}

function centerButton(button: ReactElement<ComponentProps<typeof Button>>) {
  const actionProps = {
    className: cn('justify-self-center', button.props.className),
    'data-error-state-action': 'true'
  }

  return cloneElement(button, actionProps)
}
