import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'

afterEach(() => cleanup())

// Regression coverage for the shared-consumer break found in review round 1:
// ErrorState must distinguish direct action controls from arbitrary content in
// the same slot. jsdom verifies that semantic boundary through a stable data
// hook; the task's real Chrome probe owns the width and overflow assertions.
describe('ErrorState action-slot composition', () => {
  it('marks a lone Retry action without tagging an adjacent content block', () => {
    render(
      <ErrorState description="offline" title="Offline">
        <Button data-testid="agent-retry">Retry</Button>
        <pre data-testid="crash-log">{'x'.repeat(300)}</pre>
      </ErrorState>
    )

    const retry = screen.getByRole('button', { name: 'Retry' })
    const log = screen.getByTestId('crash-log')

    expect(retry.getAttribute('data-error-state-action')).toBe('true')
    expect(log.tagName).toBe('PRE')
    expect(log.getAttribute('data-error-state-action')).toBeNull()
    expect(log.textContent).toBe('x'.repeat(300))
  })

  it('marks each Button action while leaving status content unmarked', () => {
    render(
      <ErrorState description="offline" title="Offline">
        <Button>Retry</Button>
        <Button variant="text">Copy</Button>
        <p data-testid="status-text">boot failed: connection refused</p>
      </ErrorState>
    )

    expect(screen.getByRole('button', { name: 'Retry' }).getAttribute('data-error-state-action')).toBe(
      'true'
    )
    expect(screen.getByRole('button', { name: 'Copy' }).getAttribute('data-error-state-action')).toBe(
      'true'
    )
    expect(screen.getByTestId('status-text').getAttribute('data-error-state-action')).toBeNull()
    expect(screen.getByText('boot failed: connection refused').textContent).toBe(
      'boot failed: connection refused'
    )
  })
})
