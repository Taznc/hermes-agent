import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { SCAFFOLD_META_CLASS } from '@/components/chat/scaffold-row'

/**
 * The live activity strip is the row that says the app is working. It was
 * repeatedly reported as unreadable, and the cause was never one number — it
 * was three dimmers compounding on the same line:
 *
 *   `--conversation-scaffold-text` (64% alpha)
 *     × the `[data-conversation-scaffold]` fade rule (0.67)
 *     × `ActivityTimerText`'s own `text-midground/55`
 *
 * at `text-[0.56rem]` (~9px). Enlarging the font alone would have left the
 * alpha stack in place, so these tests pin the STRUCTURAL properties that
 * caused it rather than any specific size value — a future restyle may change
 * the type scale freely, but must not reintroduce the compounding.
 */
describe('live activity strip legibility', () => {
  it('does not carry the settled-scaffold fade mark', () => {
    // `[data-conversation-scaffold]` multiplies its row by opacity 0.67. It is
    // correct for a finished tool row (a record) and wrong for the live row
    // (a signal) — the live row must light itself instead.
    const { container } = render(<ActivityTimerText seconds={50} />)

    expect(container.querySelector('[data-conversation-scaffold]')).toBeNull()
  })

  it('defaults the clock to the strip scale, not the sub-10px meta scale', () => {
    const { container } = render(<ActivityTimerText seconds={50} />)
    const cls = container.firstElementChild?.className ?? ''

    // The regression was a hardcoded ~9px default. The clock now defaults to
    // the strip's token so it moves with the strip and can't drift back.
    expect(cls).toContain('var(--activity-strip-meta-font-size)')
    expect(cls).not.toMatch(/text-\[0\.[0-5]\d*rem\]/)
  })

  it('lets a settled row still opt down to the meta scale', () => {
    // Finished tool rows / delegation cards pass SCAFFOLD_META_CLASS. Quiet is
    // right for those, so the caller's class must still win via tailwind-merge
    // — the fix must not make every timer in the app loud.
    const { container } = render(<ActivityTimerText className={SCAFFOLD_META_CLASS} seconds={50} />)
    const cls = container.firstElementChild?.className ?? ''

    expect(cls).toContain('text-[0.625rem]')
    expect(cls).not.toContain('var(--activity-strip-meta-font-size)')
  })

  it('still renders the elapsed value it was given', () => {
    // StableText splits per character; assert the digits survive the styling.
    const { container } = render(<ActivityTimerText seconds={50} />)

    expect(container.textContent).toBe('50s')
  })

  it('formats a minute-plus wait as m:ss rather than a runaway second count', () => {
    const { container } = render(<ActivityTimerText seconds={95} />)

    expect(container.textContent).toBe('1:35')
  })
})
