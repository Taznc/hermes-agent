import { describe, expect, it } from 'vitest'

import { toolPartFromStoredCall } from '@/lib/chat-messages/tool-parts'
import { latestSessionTodoSnapshot } from '@/lib/todos'

const stored = (name: string, args: unknown) => ({
  id: 'call-1',
  type: 'function',
  function: { name, arguments: JSON.stringify(args) }
})

const spawnArgs = { wait: false, tasks: [{ title: 'Count plugin tests', prompt: 'p' }] }

describe('stored tool_call bridge rows', () => {
  it('hydrate as the tool they ran, like the live stream does', () => {
    const part = toolPartFromStoredCall(
      stored('tool_call', { calls: [{ name: 'spawn_session', arguments: spawnArgs }] }),
      0
    ) as { toolName: string; args: Record<string, unknown>; toolCallId: string }

    expect(part.toolName).toBe('spawn_session')
    expect(part.args).toEqual(spawnArgs)
    expect(part.toolCallId).toBe('call-1')
  })

  it('accept the legacy single shape and JSON-string arguments', () => {
    const legacy = toolPartFromStoredCall(
      stored('tool_call', { name: 'spawn_session', arguments: JSON.stringify(spawnArgs) }),
      0
    ) as { toolName: string; args: unknown }

    expect(legacy.toolName).toBe('spawn_session')
    expect(legacy.args).toEqual(spawnArgs)
  })

  it('keep the gateway labels on the unwrapped row', () => {
    const label = { app: 'Hermes', text: 'Offered 1 follow-up', kind: 'tool' }

    const part = toolPartFromStoredCall(
      stored('tool_call', { calls: [{ name: 'spawn_session', arguments: spawnArgs }] }),
      0,
      undefined,
      { 'call-1': [label] } as never
    ) as { args: Record<string, unknown> }

    expect(part.args.hermes_tool_labels).toEqual([label])
  })

  it('leave connector batches, multi-call batches and malformed calls as tool_call', () => {
    const cases = [
      { calls: [{ name: 'connectors__github__create_issue', arguments: {} }] },
      {
        calls: [
          { name: 'connectors__github__a', arguments: {} },
          { name: 'connectors__github__b', arguments: {} }
        ]
      },
      { calls: [] },
      { calls: [{ arguments: {} }] },
      { calls: [{ name: 'tool_search', arguments: {} }] },
      {}
    ]

    for (const args of cases) {
      expect((toolPartFromStoredCall(stored('tool_call', args), 0) as { toolName: string }).toolName).toBe('tool_call')
    }
  })

  it('leave ordinary tools untouched', () => {
    const part = toolPartFromStoredCall(stored('read_file', { path: 'a' }), 0) as { toolName: string; args: unknown }

    expect(part.toolName).toBe('read_file')
    expect(part.args).toEqual({ path: 'a' })
  })

  it('keep a bridged todo result confirmed', () => {
    const todos = [{ id: '1', content: 'x', status: 'pending' }]

    const part = toolPartFromStoredCall(stored('tool_call', { calls: [{ name: 'todo', arguments: { todos } }] }), 0)

    const snapshot = latestSessionTodoSnapshot([
      { parts: [{ ...part, storedResultToolName: 'todo', result: { todos, revision: 1 } }] }
    ])

    expect(snapshot?.todos.map(todo => todo.id)).toEqual(['1'])
  })
})
