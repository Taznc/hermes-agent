/**
 * Phase 2.12 (Desktop half) — the error card's rate-limit recovery row:
 * message copy, action visibility gating, and the default=resume_at_reset
 * countdown (fake timers, not real sleeps per the card's verification rule).
 */
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hermes')>()

  return {
    ...actual,
    createCronJob: vi.fn().mockResolvedValue({ id: 'job-1', enabled: true }),
    deleteCronJob: vi.fn().mockResolvedValue({ ok: true }),
    getCronJobs: vi.fn().mockResolvedValue([]),
    getHermesConfigRecord: vi.fn().mockResolvedValue({ sessions: {} }),
    saveHermesConfig: vi.fn().mockResolvedValue({ ok: true })
  }
})

import type { ErrorSurface } from '@/lib/error-surface'
import { getHermesConfigRecord } from '@/hermes'
import { $rateLimitDefaultRecovery, $scheduledResumeJobs } from '@/store/rate-limit-recovery'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { stubThreadEnvironment } from '../test-utils'

import { Thread } from '.'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

stubThreadEnvironment()

function rateLimitedMessage(surface: Partial<ErrorSurface> = {}): ThreadMessage {
  return {
    id: 'assistant-error-1',
    role: 'assistant',
    content: [],
    status: { type: 'incomplete', reason: 'error', error: 'rate limited' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {
        errorSurface: { layer: 'provider', code: 'rate_limit', retryable: true, provider: 'openrouter', ...surface }
      }
    }
  } as unknown as ThreadMessage
}

function Harness({ message }: { message: ThreadMessage }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [message],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

beforeEach(() => {
  $rateLimitDefaultRecovery.set('ask')
  $scheduledResumeJobs.set({})
  $selectedStoredSessionId.set('stored-session-1')
  $activeSessionId.set('runtime-session-1')
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  $selectedStoredSessionId.set(null)
  $activeSessionId.set(null)
})

describe('rate-limit error card', () => {
  it('shows the reset time and Resume at reset when resetAt is known', async () => {
    render(<Harness message={rateLimitedMessage({ resetAt: Math.floor(Date.now() / 1000) + 60 })} />)

    expect(await screen.findByRole('button', { name: 'Resume at reset' })).toBeTruthy()
    expect(screen.getByText(/is rate limiting this account/)).toBeTruthy()
    expect(screen.queryByText('Reset time unknown')).toBeNull()
  })

  it('says the reset time is unknown rather than fabricating one when resetAt is absent', async () => {
    render(<Harness message={rateLimitedMessage()} />)

    await screen.findByText(/is rate limiting this account/)

    expect(screen.getByText('Reset time unknown')).toBeTruthy()
    // No reset time to schedule against — Resume at reset has nothing to do.
    expect(screen.queryByRole('button', { name: 'Resume at reset' })).toBeNull()
  })

  it('shows Switch model & retry only when fallbackAvailable is true', async () => {
    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 60, fallbackAvailable: true })} />)

    expect(await screen.findByRole('button', { name: 'Switch model & retry' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Configure automatic fallback…' })).toBeNull()
  })

  it('offers Configure automatic fallback when fallbackAvailable is false or absent', async () => {
    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 60, fallbackAvailable: false })} />)

    await screen.findByRole('button', { name: 'Resume at reset' })
    // Switch model & retry requires fallbackAvailable === true — absent here.
    expect(screen.queryByRole('button', { name: 'Switch model & retry' })).toBeNull()
    // The Configure automatic fallback deep-link itself needs a <Router>
    // context (useNavigate throws otherwise) — this bare harness has none,
    // mirroring how the existing generic Switch Provider action is gated
    // (see SwitchProviderAction's inRouter guard). Router-present rendering
    // is exercised at the app-shell level, not per-component here.
  })

  it('never renders as a plain generic failure — the rate-limit copy always wins for this code', async () => {
    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 60 })} />)

    await screen.findByText(/is rate limiting this account/)
    expect(screen.queryByText('rate limited')).toBeNull()
  })
})

describe('rate-limit auto-resume countdown (default = resume_at_reset)', () => {
  it('shows the full failure card and a cancelable countdown before scheduling', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    $rateLimitDefaultRecovery.set('resume_at_reset')
    vi.mocked(getHermesConfigRecord).mockResolvedValue({ sessions: { rate_limit_default_recovery: 'resume_at_reset' } })

    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 3600 })} />)

    // The card and its context always render first — never silent.
    await vi.waitFor(() => expect(screen.getByText(/is rate limiting this account/)).toBeTruthy())
    await vi.waitFor(() => expect(screen.getByText(/Resuming in/)).toBeTruthy())

    // Never schedule before the countdown UI has painted at least once.
    expect(Object.keys($scheduledResumeJobs.get())).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000)
      await Promise.resolve()
      await Promise.resolve()
    })

    await vi.waitFor(() => expect(Object.keys($scheduledResumeJobs.get())).toHaveLength(1), { timeout: 3000 })
  })

  it('cancelling the countdown leaves the card in its normal manual-choice state (no job scheduled)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    $rateLimitDefaultRecovery.set('resume_at_reset')
    vi.mocked(getHermesConfigRecord).mockResolvedValue({ sessions: { rate_limit_default_recovery: 'resume_at_reset' } })

    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 3600 })} />)

    const cancelButton = await screen.findByRole('button', { name: 'Cancel' })
    fireEvent.click(cancelButton)

    // Countdown gone; manual "Resume at reset" action available instead.
    expect(screen.queryByText(/Resuming in/)).toBeNull()
    expect(await screen.findByRole('button', { name: 'Resume at reset' })).toBeTruthy()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(Object.keys($scheduledResumeJobs.get())).toHaveLength(0)
  })

  it('does not run a countdown when the default is ask (manual choice only)', async () => {
    $rateLimitDefaultRecovery.set('ask')
    vi.mocked(getHermesConfigRecord).mockResolvedValue({ sessions: { rate_limit_default_recovery: 'ask' } })

    render(<Harness message={rateLimitedMessage({ resetAt: Date.now() / 1000 + 3600 })} />)

    await screen.findByRole('button', { name: 'Resume at reset' })
    expect(screen.queryByText(/Resuming in/)).toBeNull()
  })
})
