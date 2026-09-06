import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { __resetElapsedTimerRegistryForTests } from '@/components/chat/activity-timer'

import { BackgroundTaskCard } from './background-task-card'

describe('BackgroundTaskCard', () => {
  afterEach(() => {
    __resetElapsedTimerRegistryForTests()
    vi.useRealTimers()
  })

  it('keeps a running task compact until the user expands its card', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-06T16:00:00Z'))

    render(
      <BackgroundTaskCard
        item={{
          cwd: '/repo/.worktrees/feature',
          id: 'proc-1',
          pid: 1234,
          startedAt: Date.now() - 95_000,
          state: 'running',
          title: 'npx vitest run --project ui',
          type: 'background'
        }}
        onStop={vi.fn()}
      />
    )

    expect(screen.getByText('npx vitest run --project ui')).toBeTruthy()
    expect(screen.getByRole('button', { name: /npx vitest run/i }).textContent).toContain('1:35')
    expect(screen.queryByText('/repo/.worktrees/feature')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /npx vitest run/i }))

    expect(screen.getByText('/repo/.worktrees/feature')).toBeTruthy()
    expect(screen.getByText('#1234')).toBeTruthy()
  })

  it('shows captured output and the exit result after a task finishes', () => {
    render(
      <BackgroundTaskCard
        item={{
          exitCode: 1,
          id: 'proc-2',
          output: 'FAIL src/example.test.tsx',
          state: 'failed',
          title: 'npx vitest run',
          type: 'background'
        }}
        onDismiss={vi.fn()}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /npx vitest run/i }))

    expect(screen.getByText('FAIL src/example.test.tsx')).toBeTruthy()
    expect(screen.getByText('exit 1')).toBeTruthy()
  })
})
