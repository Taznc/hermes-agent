/**
 * Focused tests for the task detail view's call-to-action banner: the answer
 * to "why is this stuck and what do I do about it" for blocked/review tasks.
 * Exercises the real @hermes/plugin-sdk (aliased to src/sdk in vite/vitest
 * config), matching the pattern in model-override.test.tsx.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { CtaBanner, latestBlockReason } from './drawer'
import type { KanbanEvent, KanbanTaskFull } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

// Plugins only ever see @hermes/plugin-sdk (enforced by eslint), so tests
// mirror the real bundle but stub usePluginI18n to echo the dotted key —
// same shim completion-notify.test.ts uses. Assertions match on those keys
// instead of translated English text.
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  cleanup()
})

const baseTask = (overrides: Partial<KanbanTaskFull>): KanbanTaskFull => ({
  id: 't_abc123',
  status: 'ready',
  title: 'Some task',
  ...overrides
})

const blockedEvent = (reason?: string, kind: KanbanEvent['kind'] = 'blocked'): KanbanEvent => ({
  id: 1,
  kind,
  payload: reason ? { reason } : null,
  created_at: 0
})

describe('latestBlockReason', () => {
  it('reads the reason off the most recent blocked event', () => {
    const events = [blockedEvent('first reason'), blockedEvent('second reason')]

    expect(latestBlockReason(events)).toBe('second reason')
  })

  it('also reads block_loop_detected reasons', () => {
    expect(latestBlockReason([blockedEvent('looped', 'block_loop_detected')])).toBe('looped')
  })

  it('returns null when there is no blocked event, or no reason recorded', () => {
    expect(latestBlockReason([])).toBeNull()
    expect(latestBlockReason([blockedEvent(undefined)])).toBeNull()
  })

  it('tolerates a stringified JSON payload', () => {
    const event: KanbanEvent = { id: 1, kind: 'blocked', payload: JSON.stringify({ reason: 'stringy' }), created_at: 0 }

    expect(latestBlockReason([event])).toBe('stringy')
  })
})

describe('CtaBanner', () => {
  it('renders nothing for a normal (non-blocked, non-review) task', () => {
    const task = baseTask({ status: 'running' })
    const { container } = render(<CtaBanner events={[]} onFocusComment={vi.fn()} onMove={vi.fn()} task={task} />)

    expect(container.innerHTML).toBe('')
  })

  it('blocked: shows the worker-recorded reason and a Reply + Unblock action', () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })
    const onFocusComment = vi.fn()
    const onMove = vi.fn()

    render(
      <CtaBanner events={[blockedEvent('Which API key should I use?')]} onFocusComment={onFocusComment} onMove={onMove} task={task} />
    )

    expect(screen.getByText('Which API key should I use?')).toBeTruthy()
    expect(screen.getByText('blockKind.needs_input')).toBeTruthy()

    fireEvent.click(screen.getByText('ctaReply'))
    expect(onFocusComment).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByText('ctaUnblock'))
    expect(onMove).toHaveBeenCalledWith('ready')
  })

  it('blocked with no reason recorded: falls back to explanatory copy', () => {
    const task = baseTask({ status: 'blocked', block_kind: null })

    render(<CtaBanner events={[]} onFocusComment={vi.fn()} onMove={vi.fn()} task={task} />)

    expect(screen.getByText('ctaBlockedNoReason')).toBeTruthy()
  })

  it('review: offers Approve (-> done) and Send back (-> ready)', () => {
    const task = baseTask({ status: 'review' })
    const onMove = vi.fn()

    render(<CtaBanner events={[]} onFocusComment={vi.fn()} onMove={onMove} task={task} />)

    expect(screen.getByText('ctaReviewTitle')).toBeTruthy()

    fireEvent.click(screen.getByText('ctaApprove'))
    expect(onMove).toHaveBeenCalledWith('done')

    fireEvent.click(screen.getByText('ctaSendBack'))
    expect(onMove).toHaveBeenCalledWith('ready')
  })
})
