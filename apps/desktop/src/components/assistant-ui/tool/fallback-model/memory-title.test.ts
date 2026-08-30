import { describe, expect, it } from 'vitest'

import type { ToolPart } from './types'

import { buildToolView } from './index'

function memoryPart(args: Record<string, unknown>, result?: unknown): ToolPart {
  return { args, result, timestamp: 0, toolCallId: 'memory-1', toolName: 'memory', type: 'tool-call' }
}

// The title used to be hardcoded to "Saving to memory" / "Saved to memory"
// for every `memory` call regardless of its `action` argument, so a read
// (search/probe/related/reason/list) misreported itself as a write while it
// ran. Each action now gets its own present/past title.
describe('buildToolView memory action titles', () => {
  it('reads a search call as searching, not saving', () => {
    const running = buildToolView(memoryPart({ action: 'search', query: 'editor config' }), '')

    expect(running.title).toBe('Searching memory')

    const done = buildToolView(
      memoryPart({ action: 'search', query: 'editor config' }, { results: [] }),
      ''
    )

    expect(done.title).toBe('Searched memory')
  })

  it('reads a probe call as recalling, not saving', () => {
    expect(buildToolView(memoryPart({ action: 'probe', entity: 'user' }), '').title).toBe('Recalling memory')
  })

  it('still reads an add call as saving', () => {
    expect(buildToolView(memoryPart({ action: 'add', content: 'note' }), '').title).toBe('Saving a note')
    expect(
      buildToolView(memoryPart({ action: 'add', content: 'note' }, { success: true }), '').title
    ).toBe('Saved a note')
  })

  it('falls back to the generic memory title for an unrecognized or missing action', () => {
    expect(buildToolView(memoryPart({}), '').title).toBe('Saving to memory')
    expect(buildToolView(memoryPart({ action: 'not_a_real_action' }), '').title).toBe('Saving to memory')
  })
})
