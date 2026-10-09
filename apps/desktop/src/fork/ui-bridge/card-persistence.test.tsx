import type { ThreadMessage } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useMemo } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { stubThreadEnvironment, stubThreadViewportSize, ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { Thread } from '@/components/assistant-ui/thread'
import { splitRunItems } from '@/components/assistant-ui/tool/fallback'
import { registry } from '@/contrib/registry'
import { UI_REQUEST_AREA } from '@/fork/ui-bridge/types'
import { toChatMessages } from '@/lib/chat-messages/hydration'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { messagePaintWeight } from '@/lib/render-weight'
import { isCardTool } from '@/lib/tool-render-class'
import { $activeSessionId } from '@/store/session'
import { $toolDisclosureStates } from '@/store/tool-view'
import type { SessionMessage } from '@/types/hermes'

stubThreadEnvironment()
stubThreadViewportSize()

let dispose = () => {}
const launch = vi.fn()

function registerCard(tool = 'spawn_session') {
  dispose = registry.register({
    area: UI_REQUEST_AREA,
    id: 'test/plugin-card',
    data: {
      kind: 'test/offers',
      tool,
      render: () => null,
      renderResult: () => (
        <button onClick={launch} type="button">
          Count plugin tests
        </button>
      )
    }
  })
}

function message(running: boolean): ThreadMessage {
  return {
    id: 'spawn-persistence',
    role: 'assistant',
    createdAt: new Date('2026-10-04T23:43:00Z'),
    status: running ? { type: 'running' } : { type: 'complete', reason: 'stop' },
    content: ['read_file', 'spawn_session', 'search_files'].map((toolName, index) => ({
      type: 'tool-call',
      toolCallId: `call-${index}`,
      toolName,
      args: {},
      argsText: '{}',
      result: toolName === 'spawn_session' ? { status: 'offered', wait: false, tasks: [] } : { content: 'done' }
    })),
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
}

function Harness({ value }: { value: ThreadMessage }) {
  const messages = useMemo(() => [value], [value])

  return (
    <ThreadRuntime messages={messages}>
      <Thread />
    </ThreadRuntime>
  )
}

beforeEach(() => {
  $activeSessionId.set('sess-1')
  $toolDisclosureStates.set({})
  launch.mockClear()
})

afterEach(() => {
  cleanup()
  dispose()
  $activeSessionId.set(null)
})

describe('plugin result cards in the transcript', () => {
  it('keeps non-blocking offers visible and usable after turn completion and remount', async () => {
    registerCard()
    const { rerender, unmount } = render(<Harness value={message(true)} />)
    expect(await screen.findByRole('button', { name: 'Count plugin tests' })).toBeTruthy()
    rerender(<Harness value={message(false)} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Count plugin tests' }))
    expect(launch).toHaveBeenCalledOnce()
    unmount()
    render(<Harness value={message(false)} />)
    expect(await screen.findByRole('button', { name: 'Count plugin tests' })).toBeTruthy()
  })

  it('recognizes any registered result renderer, without hard-coding tool names', () => {
    registerCard('private_followup')
    expect(isCardTool('private_followup')).toBe(true)
    expect(splitRunItems(['read_file', 'private_followup', 'terminal'])).toEqual([
      { kind: 'run', start: 0, end: 0 },
      { kind: 'card', index: 1 },
      { kind: 'run', start: 2, end: 2 }
    ])
    dispose()
    expect(isCardTool('private_followup')).toBe(false)
  })

  it('renders a deferred tool reached through the tool_call bridge after a transcript reload', async () => {
    registerCard()

    const history = toChatMessages([
      { role: 'user', content: 'offer follow-ups', timestamp: 1 },
      {
        role: 'assistant',
        content: '',
        timestamp: 2,
        tool_calls: [
          {
            id: 'bridged-1',
            type: 'function',
            function: {
              name: 'tool_call',
              arguments: JSON.stringify({ calls: [{ name: 'spawn_session', arguments: { wait: false, tasks: [] } }] })
            }
          }
        ]
      },
      {
        role: 'tool',
        tool_call_id: 'bridged-1',
        tool_name: 'spawn_session',
        content: JSON.stringify({ status: 'offered', wait: false, tasks: [] }),
        timestamp: 3
      },
      { role: 'assistant', content: 'Offered.', timestamp: 4 }
    ] as SessionMessage[])

    const assistant = history.find(entry => entry.role === 'assistant')!
    render(<Harness value={toRuntimeMessage(assistant)} />)
    expect(await screen.findByRole('button', { name: 'Count plugin tests' })).toBeTruthy()
  })

  it('prices plugin result cards above collapsed activity rows', () => {
    registerCard()
    const part = (toolName: string) => [{ type: 'tool-call', toolName, args: {}, result: {} }]
    expect(messagePaintWeight(part('spawn_session'))).toBeGreaterThan(messagePaintWeight(part('read_file')))
  })

  it('does not promote request-only or disabled contributions to result cards', () => {
    dispose = registry.registerMany([
      {
        area: UI_REQUEST_AREA,
        id: 'test/request-only',
        data: { kind: 'test/request', tool: 'request_only', render: () => null }
      },
      {
        area: UI_REQUEST_AREA,
        id: 'test/disabled',
        enabled: false,
        data: { kind: 'test/disabled', tool: 'disabled_card', render: () => null, renderResult: () => null }
      }
    ])
    expect(isCardTool('request_only')).toBe(false)
    expect(isCardTool('disabled_card')).toBe(false)
    expect(isCardTool('read_file')).toBe(false)
    expect(isCardTool('patch')).toBe(true)
  })
})
