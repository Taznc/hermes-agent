import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $displayTimestamps } from '@/store/display-timestamps'
import type { ReviewActionRecord } from '@/types/hermes'

import { stubThreadEnvironment } from '../test-utils'

import { Thread } from '.'

// Timeline timestamps render only when `display.timestamps` is enabled.
$displayTimestamps.set(true)

const timestamp = new Date('2026-05-01T00:00:00.000Z')
stubThreadEnvironment()

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

const reviewActions: ReviewActionRecord[] = [
  {
    target: 'memory',
    label: 'Memory',
    operation: 'add',
    success: true,
    message: 'Entry added.',
    state: 'completed',
    reason: 'Memory add completed.',
    change_summary: 'Before: no new record. After: record added.'
  },
  {
    target: 'user',
    label: 'User profile',
    operation: 'replace',
    success: false,
    message: 'No profile update needed.',
    state: 'no_op',
    reason: 'No user profile change was needed.',
    change_summary: 'No stored content was changed.'
  },
  {
    target: 'skill',
    label: 'Skill',
    operation: 'patch',
    success: false,
    message: 'Skipped.',
    state: 'skipped',
    skill_name: 'demo',
    reason: 'Skill review action was skipped.',
    change_summary: 'No stored content was changed.'
  },
  {
    target: 'memory',
    label: 'Memory',
    operation: 'remove',
    success: false,
    message: 'Declined.',
    state: 'declined',
    reason: 'Memory review action was declined.',
    change_summary: 'No stored content was changed.'
  },
  {
    target: 'memory',
    label: 'Memory',
    operation: 'add',
    success: false,
    message: 'Internal detail must remain private.',
    state: 'failed',
    reason: 'Memory add did not complete.',
    change_summary: 'No stored content was changed.'
  }
]

describe('self-improvement review expandable detail', () => {
  it('renders collapsed by default with an accessible summary disclosure', () => {
    render(<ReviewHarness reviewActions={reviewActions} text="💾 Self-improvement review: Memory updated" />)

    expect(screen.getByRole('button', { name: /show details \(1 failed\)/i })).toBeTruthy()
    expect(screen.queryByText(/Memory · add · Completed/)).toBeFalsy()
  })

  it('shows a compact target, operation, and terminal state for every reviewed target and outcome', () => {
    render(<ReviewHarness reviewActions={reviewActions} text="💾 Self-improvement review: updates reviewed" />)

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))

    expect(screen.getByText('Memory · add · Completed')).toBeTruthy()
    expect(screen.getByText('User profile · replace · No change')).toBeTruthy()
    expect(screen.getByText('Skill “demo” · patch · Skipped')).toBeTruthy()
    expect(screen.getByText('Memory · remove · Declined')).toBeTruthy()
    expect(screen.getByText('Memory · add · Failed')).toBeTruthy()
  })

  it('expands one record with only the producer-provided redacted detail', () => {
    render(<ReviewHarness reviewActions={reviewActions} text="💾 Self-improvement review: updates reviewed" />)

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    fireEvent.click(screen.getAllByRole('button', { name: /show memory review details/i })[0])

    expect(screen.getByText('Before: no new record. After: record added.')).toBeTruthy()
    expect(screen.getByText('Memory add completed.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /hide record details/i })).toBeTruthy()
    expect(screen.queryByText('Internal detail must remain private.')).toBeFalsy()
  })

  it('keeps legacy previews and messages out of the transcript', () => {
    const legacyAction: ReviewActionRecord = {
      target: 'memory',
      label: 'Memory',
      operation: 'add',
      success: true,
      message: 'raw saved content',
      content_preview: 'raw saved content'
    }

    render(<ReviewHarness reviewActions={[legacyAction]} text="💾 Self-improvement review: Memory updated" />)

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    fireEvent.click(screen.getAllByRole('button', { name: /show memory review details/i })[0])

    expect(screen.getByText(/Details are unavailable from this older Hermes version/)).toBeTruthy()
    expect(screen.queryByText('raw saved content')).toBeFalsy()
    expect(screen.queryByRole('button', { name: /inspect/i })).toBeFalsy()
  })

  it('collapses the review disclosure again', () => {
    render(<ReviewHarness reviewActions={[reviewActions[0]]} text="💾 Self-improvement review: Memory updated" />)

    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    expect(screen.getByText('Memory · add · Completed')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))
    expect(screen.queryByText('Memory · add · Completed')).toBeFalsy()
  })

  it('renders no expand toggle when a review row has no structured actions', () => {
    render(<ReviewHarness reviewActions={[]} text="💾 Self-improvement review: Memory updated" />)

    expect(screen.queryByRole('button', { name: /show details/i })).toBeFalsy()
  })
})
