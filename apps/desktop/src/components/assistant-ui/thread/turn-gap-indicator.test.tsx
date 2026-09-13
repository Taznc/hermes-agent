// The gap case: the agent is working, the composer's arc border is on and Stop
// is armed, but the tail bubble has settled — a sealed interim row, or a turn
// whose last message completed while the agent kept going. The transcript used
// to show nothing there, and the seconds went uncounted.
import { type ThreadMessage } from '@assistant-ui/react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetElapsedTimerRegistryForTests } from '@/components/chat/activity-timer'
import { I18nProvider } from '@/i18n'
import { $activeSessionId, $busy, $messages, $turnStartedAt } from '@/store/session'

import { stubThreadEnvironment, ThreadRuntime, userMessage } from '../test-utils'

import { Thread } from '.'
stubThreadEnvironment()

const createdAt = new Date('2026-05-01T00:00:00.000Z')
const sessionId = 'session-turn-gap'

function assistant(id: string, content: unknown[], running: boolean): ThreadMessage {
  return {
    id,
    role: 'assistant',
    content,
    status: running ? { type: 'running' } : { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as unknown as ThreadMessage
}

const toolCall = (toolName: string, settled: boolean) => ({
  type: 'tool-call',
  toolCallId: `${toolName}-1`,
  toolName,
  args: {},
  ...(settled ? { result: 'ok' } : {})
})

const Harness = ({ locale = 'en', messages }: { locale?: 'en' | 'ja'; messages: ThreadMessage[] }) => (
  <I18nProvider configClient={null} initialLocale={locale}>
    <ThreadRuntime messages={messages}>
      <Thread />
    </ThreadRuntime>
  </I18nProvider>
)

const timerText = (value: string) => screen.getAllByText((_, node) => node?.textContent === value)

describe('the turn timer covers the gaps, not just the streaming', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'))
    vi.spyOn(globalThis.document, 'hasFocus').mockReturnValue(true)
    __resetElapsedTimerRegistryForTests()
    $activeSessionId.set(sessionId)
    $messages.set([])
    $turnStartedAt.set(Date.now())
    $busy.set(true)
  })

  afterEach(() => {
    cleanup()
    $activeSessionId.set(null)
    $turnStartedAt.set(null)
    $busy.set(false)
    __resetElapsedTimerRegistryForTests()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('times a settled tail bubble while the session is still working', () => {
    // The sealed-bubble gap. Nothing is running at message level; the session
    // is busy, so the transcript owes the user a line and a count — measured
    // from the last thing the turn produced, not from when the row appeared.
    const { container } = render(
      <Harness
        messages={[userMessage('u1', 'do the thing'), assistant('a1', [{ type: 'text', text: 'On it.' }], false)]}
      />
    )

    act(() => vi.advanceTimersByTime(7_000))

    expect(container.querySelector('[data-slot="aui_turn-activity"]')).not.toBeNull()
    expect(timerText('7s').length).toBeGreaterThan(0)
  })

  it('times the gap between a finished tool call and the next thing', () => {
    const { container } = render(
      <Harness messages={[userMessage('u1', 'read it'), assistant('a1', [toolCall('read_file', true)], true)]} />
    )

    act(() => vi.advanceTimersByTime(9_000))

    expect(container.querySelector('[data-slot="aui_turn-activity"]')).not.toBeNull()
    expect(timerText('9s').length).toBeGreaterThan(0)
  })

  it('stays silent under a tool call still in flight — that row has its own timer', () => {
    const { container } = render(
      <Harness messages={[userMessage('u1', 'run it'), assistant('a1', [toolCall('terminal', false)], true)]} />
    )

    act(() => vi.advanceTimersByTime(9_000))

    expect(container.querySelector('[data-slot="aui_turn-activity"]')).toBeNull()
  })

  it('keeps a completed operation visible briefly before settling the row away', () => {
    const { container } = render(
      <Harness
        messages={[userMessage('u1', 'do the thing'), assistant('a1', [{ type: 'text', text: 'Done.' }], false)]}
      />
    )

    act(() => vi.advanceTimersByTime(7_000))
    expect(container.querySelector('[data-slot="aui_turn-activity"]')).not.toBeNull()

    act(() => $busy.set(false))

    expect(container.querySelector('[data-terminal-activity="success"]')).not.toBeNull()
    expect(container.querySelector('[data-activity-mark="success"]')).not.toBeNull()
    expect(container.querySelector('[data-terminal-activity="success"]')?.getAttribute('aria-label')).toBe('Work complete')

    act(() => vi.advanceTimersByTime(1_999))
    expect(container.querySelector('[data-terminal-activity="success"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(1))
    expect(container.querySelector('[data-slot="aui_turn-activity"]')).toBeNull()
  })

  it('does not let an earlier completion truncate a later completion', () => {
    const messages = [userMessage('u1', 'do the thing'), assistant('a1', [{ type: 'text', text: 'Done.' }], false)]
    const { container } = render(<Harness messages={messages} />)

    act(() => vi.advanceTimersByTime(7_000))
    act(() => $busy.set(false))
    expect(container.querySelector('[data-terminal-activity="success"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(1_000))
    act(() => $busy.set(true))
    act(() => vi.advanceTimersByTime(7_000))
    act(() => $busy.set(false))
    expect(container.querySelector('[data-terminal-activity="success"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(1_001))
    expect(container.querySelector('[data-terminal-activity="success"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(999))
    expect(container.querySelector('[data-slot="aui_turn-activity"]')).toBeNull()
  })

  it('does not duplicate a fatal error with a terminal failure activity row', () => {
    const fatalMessages = [
      userMessage('u1', 'do the thing'),
      {
        ...assistant('a1', [{ type: 'text', text: 'The operation failed.' }], false),
        status: { type: 'error', error: new Error('fatal') }
      } as unknown as ThreadMessage
    ]

    const { container } = render(<Harness messages={fatalMessages} />)

    act(() => vi.advanceTimersByTime(7_000))
    act(() => $busy.set(false))

    expect(container.querySelector('[data-terminal-activity="failure"]')).toBeNull()
  })

  it('localizes the terminal activity status label', () => {
    const messages = [userMessage('u1', 'do the thing'), assistant('a1', [{ type: 'text', text: 'Done.' }], false)]
    const { container } = render(<Harness locale="ja" messages={messages} />)

    act(() => vi.advanceTimersByTime(7_000))
    act(() => $busy.set(false))

    expect(container.querySelector('[data-terminal-activity="success"]')?.getAttribute('aria-label')).toBe(
      '作業が完了しました'
    )
  })

  it('retains a recoverable failure briefly before settling the row away', () => {
    const failedMessages = [
      userMessage('u1', 'do the thing'),
      {
        ...assistant('a1', [{ type: 'text', text: 'Could not finish.' }], false),
        status: { type: 'incomplete' }
      } as ThreadMessage
    ]

    const { container } = render(<Harness messages={failedMessages} />)

    act(() => vi.advanceTimersByTime(7_000))
    act(() => $busy.set(false))

    expect(container.querySelector('[data-terminal-activity="failure"]')).not.toBeNull()
    expect(container.querySelector('[data-activity-mark="failure"]')).not.toBeNull()
    expect(container.querySelector('[data-terminal-activity="failure"]')?.getAttribute('aria-label')).toBe(
      'Work needs attention'
    )

    act(() => vi.advanceTimersByTime(1_999))
    expect(container.querySelector('[data-terminal-activity="failure"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(1))
    expect(container.querySelector('[data-slot="aui_turn-activity"]')).toBeNull()
  })
})
