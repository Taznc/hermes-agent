import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'

afterEach(() => cleanup())

// Regression coverage for the shared-consumer break found in review round 1:
// centering the Retry affordance (justify-self-center on Button actions)
// must not touch arbitrary non-Button content in the same slot (the crash
// boundary's scrollable log block). jsdom has no layout engine, so this
// intentionally asserts the BEHAVIOR CONTRACT — which element type receives
// the centering treatment — not a pixel measurement (that lives in the real
// Chrome/CDP probe recorded in the task handoff), and not a frozen class
// string (see "Assert on data-* hooks, not CSS class strings").
describe('ErrorState action-slot composition', () => {
  it('centers a lone Retry button without touching an adjacent content block', () => {
    render(
      <ErrorState description="offline" title="Offline">
        <Button data-testid="agent-retry">Retry</Button>
        <pre data-testid="crash-log">{'x'.repeat(300)}</pre>
      </ErrorState>
    )

    const retry = screen.getByTestId('agent-retry')
    const log = screen.getByTestId('crash-log')

    // The action control is a real <button>, centered to its natural width
    // (justify-self-center) — never full-bleed, never shrink-wrapped away.
    expect(retry.tagName).toBe('BUTTON')
    expect(retry.className).toContain('justify-self-center')

    // A non-Button content block passes through completely unmodified: same
    // element, same full text, no centering class applied to it (that is
    // precisely the regression — the old fix pulled `<pre>` narrow too).
    expect(log.tagName).toBe('PRE')
    expect(log.textContent).toBe('x'.repeat(300))
    expect(log.className).not.toContain('justify-self-center')
  })

  it('leaves multiple Button actions and a status paragraph each independently correct', () => {
    render(
      <ErrorState description="offline" title="Offline">
        <Button data-testid="retry-btn">Retry</Button>
        <Button data-testid="copy-btn" variant="text">
          Copy
        </Button>
        <p data-testid="status-text">boot failed: connection refused</p>
      </ErrorState>
    )

    expect(screen.getByTestId('retry-btn').className).toContain('justify-self-center')
    expect(screen.getByTestId('copy-btn').className).toContain('justify-self-center')
    expect(screen.getByTestId('status-text').className).not.toContain('justify-self-center')
    expect(screen.getByTestId('status-text').textContent).toBe('boot failed: connection refused')
  })
})
