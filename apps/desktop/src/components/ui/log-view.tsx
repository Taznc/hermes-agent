import { type ComponentProps, Fragment } from 'react'

import { cn } from '@/lib/utils'

// Shared raw-log viewer: no bg, hairline border, tight padding, small mono.
// One style everywhere we surface logs. Pass a max-h-* via className.
// Selectable by default — logs exist to be read and copied.
//
// `numbered` is an opt-in variant, not a global behavior change: every other
// call site (install/boot-failure overlays) wants short human-readable status
// lines that wrap-to-fit, which is what the default mode still does. The
// kanban worker log is the one surface that needs to hold a long shell
// command and let a human refer to a specific line — for that, wrapping
// mid-token destroys the one reason to use a mono font. Numbered mode keeps
// every line on its own row with a stable line-number gutter and scrolls
// horizontally instead of wrapping (see DESIGN.md "Logs" + this file's own
// "one style everywhere" comment — decided to extend rather than change the
// shared default so the other call sites are untouched).
export interface LogViewProps extends Omit<ComponentProps<'div'>, 'children'> {
  numbered?: boolean
  /** Raw log text for numbered mode (children is ignored in that mode). */
  content?: string
  children?: ComponentProps<'div'>['children']
}

export function LogView({ children, className, content, numbered, ...props }: LogViewProps) {
  if (numbered) {
    const lines = (content ?? '').split('\n')

    // A trailing newline produces one phantom empty last line — drop it so
    // the gutter count matches what a human would call "N lines".
    if (lines.length > 1 && lines[lines.length - 1] === '') {
      lines.pop()
    }

    const gutterWidth = `${String(lines.length).length}ch`

    return (
      <div
        className={cn(
          'overflow-auto rounded-lg border border-(--ui-stroke-tertiary) font-mono text-[0.6875rem] leading-[1.5] text-(--ui-text-tertiary)',
          className
        )}
        data-selectable-text="true"
        {...props}
      >
        {/* `max-content` on the text column (not 1fr) lets the grid grow past
         *  the container so the OUTER `overflow-auto` scrolls horizontally
         *  instead of wrapping — the alignment-preserving behavior the card
         *  asked for. */}
        <div className="inline-grid min-w-full grid-cols-[auto_max-content] gap-x-2.5 px-2.5 py-1.5">
          {lines.map((line, index) => (
            <Fragment key={index}>
              <span
                aria-hidden="true"
                className="select-none text-right text-(--ui-text-quaternary)/70 tabular-nums"
                style={{ minWidth: gutterWidth }}
              >
                {index + 1}
              </span>
              <span className="whitespace-pre">{line.length > 0 ? line : '\u00a0'}</span>
            </Fragment>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'overflow-auto rounded-lg border border-(--ui-stroke-tertiary) px-2.5 py-1.5 font-mono text-[0.6875rem] leading-[1.5] whitespace-pre-wrap break-words text-(--ui-text-tertiary)',
        className
      )}
      data-selectable-text="true"
      {...props}
    >
      {children}
    </div>
  )
}
