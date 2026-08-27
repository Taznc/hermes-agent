/**
 * Focused tests for the task detail view's call-to-action banner: the answer
 * to "why is this stuck and what do I do about it" for blocked/review tasks.
 * Exercises the real @hermes/plugin-sdk (aliased to src/sdk in vite/vitest
 * config), matching the pattern in model-override.test.tsx.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { CtaBanner, latestBlockReason, parseBlockedChoices } from './drawer'
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

    const { container } = render(
      <CtaBanner
        comments={[]}
        events={[]}
        onFocusComment={vi.fn()}
        onMove={vi.fn()}
        onSubmitChoice={vi.fn()}
        task={task}
      />
    )

    expect(container.innerHTML).toBe('')
  })

  it('blocked: shows the worker-recorded reason and a Reply + Unblock action', () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })
    const onFocusComment = vi.fn()
    const onMove = vi.fn()

    render(
      <CtaBanner
        comments={[]}
        events={[blockedEvent('Which API key should I use?')]}
        onFocusComment={onFocusComment}
        onMove={onMove}
        onSubmitChoice={vi.fn()}
        task={task}
      />
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

    render(
      <CtaBanner comments={[]} events={[]} onFocusComment={vi.fn()} onMove={vi.fn()} onSubmitChoice={vi.fn()} task={task} />
    )

    expect(screen.getByText('ctaBlockedNoReason')).toBeTruthy()
  })

  it('review: offers Approve (-> done) and Send back (-> ready)', () => {
    const task = baseTask({ status: 'review' })
    const onMove = vi.fn()

    render(
      <CtaBanner comments={[]} events={[]} onFocusComment={vi.fn()} onMove={onMove} onSubmitChoice={vi.fn()} task={task} />
    )

    expect(screen.getByText('ctaReviewTitle')).toBeTruthy()

    fireEvent.click(screen.getByText('ctaApprove'))
    expect(onMove).toHaveBeenCalledWith('done')

    fireEvent.click(screen.getByText('ctaSendBack'))
    expect(onMove).toHaveBeenCalledWith('ready')
  })
})

describe('parseBlockedChoices', () => {
  const validFence = [
    'Which environment should this ship to?',
    '',
    '```choices',
    JSON.stringify([
      { key: 'A', label: 'Staging' },
      { key: 'B', label: 'Production', description: 'Goes live immediately' }
    ]),
    '```'
  ].join('\n')

  it('parses a valid fence into prose + options', () => {
    const result = parseBlockedChoices(validFence)

    expect(result).not.toBeNull()
    expect(result?.prose).toBe('Which environment should this ship to?')
    expect(result?.options).toEqual([
      { key: 'A', label: 'Staging' },
      { key: 'B', label: 'Production', description: 'Goes live immediately' }
    ])
  })

  it('returns null when there is no fence', () => {
    expect(parseBlockedChoices('Just a plain question, no options.')).toBeNull()
  })

  it('returns null on invalid JSON in the fence', () => {
    expect(parseBlockedChoices('Question?\n```choices\nnot json\n```')).toBeNull()
  })

  it('returns null when the array has fewer than 2 or more than 6 options', () => {
    const one = JSON.stringify([{ key: 'A', label: 'Only one' }])
    const seven = JSON.stringify(Array.from({ length: 7 }, (_, i) => ({ key: `k${i}`, label: `L${i}` })))

    expect(parseBlockedChoices(`Q?\n\`\`\`choices\n${one}\n\`\`\``)).toBeNull()
    expect(parseBlockedChoices(`Q?\n\`\`\`choices\n${seven}\n\`\`\``)).toBeNull()
  })

  it('returns null when an option is missing key or label', () => {
    const bad = JSON.stringify([{ key: 'A' }, { key: 'B', label: 'B' }])

    expect(parseBlockedChoices(`Q?\n\`\`\`choices\n${bad}\n\`\`\``)).toBeNull()
  })

  it('returns null on duplicate keys', () => {
    const dup = JSON.stringify([
      { key: 'A', label: 'First' },
      { key: 'A', label: 'Second' }
    ])

    expect(parseBlockedChoices(`Q?\n\`\`\`choices\n${dup}\n\`\`\``)).toBeNull()
  })

  it('uses the LAST fence when multiple are present', () => {
    const first = JSON.stringify([
      { key: 'X', label: 'Wrong' },
      { key: 'Y', label: 'Also wrong' }
    ])

    const second = JSON.stringify([
      { key: 'A', label: 'Right' },
      { key: 'B', label: 'Also right' }
    ])

    const reason = `Q?\n\`\`\`choices\n${first}\n\`\`\`\nmore prose\n\`\`\`choices\n${second}\n\`\`\``

    expect(parseBlockedChoices(reason)?.options).toEqual([
      { key: 'A', label: 'Right' },
      { key: 'B', label: 'Also right' }
    ])
  })
})

describe('CtaBanner with structured choices', () => {
  const choicesReason = [
    'Pick one:',
    '```choices',
    JSON.stringify([
      { key: 'A', label: 'Option A' },
      { key: 'B', label: 'Option B' }
    ]),
    '```'
  ].join('\n')

  it('renders options as clickable radio buttons instead of the plain reason text', () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })

    render(
      <CtaBanner
        comments={[]}
        events={[blockedEvent(choicesReason)]}
        onFocusComment={vi.fn()}
        onMove={vi.fn()}
        onSubmitChoice={vi.fn()}
        task={task}
      />
    )

    expect(screen.getByRole('radiogroup')).toBeTruthy()
    const radios = screen.getAllByRole('radio')

    expect(radios).toHaveLength(2)
    expect(screen.getByText('Option A')).toBeTruthy()
    expect(screen.getByText('Option B')).toBeTruthy()
    // Reply button is not shown when options ARE the reply.
    expect(screen.queryByText('ctaReply')).toBeNull()
  })

  it('clicking an option submits a structured choice and marks it selected', async () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })
    const onSubmitChoice = vi.fn().mockResolvedValue(undefined)

    render(
      <CtaBanner
        comments={[]}
        events={[blockedEvent(choicesReason)]}
        onFocusComment={vi.fn()}
        onMove={vi.fn()}
        onSubmitChoice={onSubmitChoice}
        task={task}
      />
    )

    fireEvent.click(screen.getByText('Option A'))

    expect(onSubmitChoice).toHaveBeenCalledWith('A) Option A', { key: 'A', label: 'Option A', question_event_id: 1 })

    await vi.waitFor(() => {
      expect(screen.getByText('Option A').closest('button')?.getAttribute('aria-checked')).toBe('true')
    })
    expect(screen.getByText('Option B').closest('button')?.hasAttribute('disabled')).toBe(true)
  })

  it('shows the answered state (read-only, checked) when a matching choice comment already exists', () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })

    const comment = {
      author: 'dashboard',
      body: 'B) Option B',
      choice: { key: 'B', label: 'Option B', question_event_id: 1 },
      created_at: 0,
      id: 1
    }

    render(
      <CtaBanner
        comments={[comment]}
        events={[blockedEvent(choicesReason)]}
        onFocusComment={vi.fn()}
        onMove={vi.fn()}
        onSubmitChoice={vi.fn()}
        task={task}
      />
    )

    expect(screen.getByText('Option B').closest('button')?.getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText('Option A').closest('button')?.hasAttribute('disabled')).toBe(true)
  })

  it('falls back to plain text + Reply when the fence is malformed', () => {
    const task = baseTask({ status: 'blocked', block_kind: 'needs_input' })
    const onFocusComment = vi.fn()
    const malformed = 'Pick one:\n```choices\nnot valid json\n```'

    render(
      <CtaBanner
        comments={[]}
        events={[blockedEvent(malformed)]}
        onFocusComment={onFocusComment}
        onMove={vi.fn()}
        onSubmitChoice={vi.fn()}
        task={task}
      />
    )

    expect(screen.getByText(/Pick one:/, { selector: 'p' })).toBeTruthy()
    expect(screen.queryByRole('radiogroup')).toBeNull()
    fireEvent.click(screen.getByText('ctaReply'))
    expect(onFocusComment).toHaveBeenCalledTimes(1)
  })
})
