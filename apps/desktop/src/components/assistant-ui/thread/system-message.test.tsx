import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $displayTimestamps } from '@/store/display-timestamps'
import type { ReviewActionRecord } from '@/types/hermes'

import { Thread } from '.'

// Timeline timestamps render only when `display.timestamps` is enabled.
$displayTimestamps.set(true)

const timestamp = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

function Harness({ text }: { text: string }) {
  const message = {
    id: 'system-1',
    role: 'system',
    content: [{ type: 'text', text }],
    createdAt: timestamp,
    metadata: { custom: { timelineTimestamp: timestamp.getTime() / 1000 } }
  } as unknown as ThreadMessage

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

function expectTimestampSeparated(container: HTMLElement, precedingText: string) {
  const row = container.querySelector('[data-role="system"]')
  const stamp = row?.querySelector('[data-slot="timeline-timestamp"]')?.textContent

  expect(stamp).toBeTruthy()
  expect(row?.textContent).toContain(`${precedingText} ${stamp}`)
}

afterEach(cleanup)

describe('system message timestamp text separation', () => {
  it('separates an ordinary system row timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="Review saved." />)

    expectTimestampSeparated(container, 'Review saved.')
  })

  it('separates a slash-status timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text={'slash:/model\nmodel changed'} />)

    expectTimestampSeparated(container, 'model changed')
  })

  it('separates a steer timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="steer:rerun tests" />)

    expectTimestampSeparated(container, 'rerun tests')
  })
})

// ---------------------------------------------------------------------------
// Expandable self-improvement review detail (ROADMAP.md Phase 1: Desktop
// transcript auditability). Structured per-action records ride
// metadata.custom.reviewActions the same way reactions ride
// metadata.custom.reactions — see chat-runtime.ts's toRuntimeMessage.
// ---------------------------------------------------------------------------

function ReviewHarness({ reviewActions, text }: { reviewActions: ReviewActionRecord[]; text: string }) {
  const message = {
    id: 'system-review-1',
    role: 'system',
    content: [{ type: 'text', text: `review:${text}` }],
    createdAt: timestamp,
    metadata: { custom: { reviewActions, timelineTimestamp: timestamp.getTime() / 1000 } }
  } as unknown as ThreadMessage

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

const memoryAddAction: ReviewActionRecord = {
  target: 'memory',
  label: 'Memory',
  operation: 'add',
  success: true,
  message: 'Entry added.',
  content_preview: 'User prefers terse replies'
}

const skillPatchAction: ReviewActionRecord = {
  target: 'skill',
  label: 'Skill',
  operation: 'patch',
  success: true,
  message: "Patched SKILL.md in skill 'demo' (1 replacement).",
  skill_name: 'demo',
  old_preview: 'old approach',
  new_preview: 'new approach'
}

const failedMemoryAction: ReviewActionRecord = {
  target: 'memory',
  label: 'Memory',
  operation: 'add',
  success: false,
  message: 'Adding this entry would exceed the memory char budget.'
}

describe('self-improvement review expandable detail', () => {
  it('renders collapsed by default with a "Show details" toggle', () => {
    render(<ReviewHarness reviewActions={[memoryAddAction]} text="💾 Self-improvement review: Memory updated" />)

    expect(screen.getByRole('button', { name: /show details/i })).toBeTruthy()
    expect(screen.queryByText('User prefers terse replies')).toBeFalsy()
  })

  it('expands to show each individual action on click', () => {
    render(
      <ReviewHarness
        reviewActions={[memoryAddAction, skillPatchAction]}
        text="💾 Self-improvement review: Memory updated · Skill 'demo' patched"
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))

    expect(screen.getByText(/User prefers terse replies/)).toBeTruthy()
    expect(screen.getByText(/old approach/)).toBeTruthy()
    expect(screen.getByText(/new approach/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /hide details/i })).toBeTruthy()
  })

  it('collapses again on a second click', () => {
    render(<ReviewHarness reviewActions={[memoryAddAction]} text="💾 Self-improvement review: Memory updated" />)

    const toggle = screen.getByRole('button', { name: /show details/i })
    fireEvent.click(toggle)
    expect(screen.getByText(/User prefers terse replies/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))
    expect(screen.queryByText(/User prefers terse replies/)).toBeFalsy()
  })

  it('surfaces a failed action count in the collapsed toggle label', () => {
    render(<ReviewHarness reviewActions={[failedMemoryAction]} text="💾 Self-improvement review: Memory update failed" />)

    expect(screen.getByRole('button', { name: /show details \(1 failed\)/i })).toBeTruthy()
  })

  it("shows the tool's own failure reason on a failed action once expanded", () => {
    render(<ReviewHarness reviewActions={[failedMemoryAction]} text="💾 Self-improvement review: Memory update failed" />)

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))

    expect(screen.getByText(/Adding this entry would exceed the memory char budget/)).toBeTruthy()
  })

  it('renders no expand toggle when a review row has no structured actions', () => {
    render(<ReviewHarness reviewActions={[]} text="💾 Self-improvement review: Memory updated" />)

    expect(screen.queryByRole('button', { name: /show details/i })).toBeFalsy()
  })
})
