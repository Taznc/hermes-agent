import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { $subagentsBySession } from '@/store/subagents'

import { SpawnTreeView } from './spawn-tree'

afterEach(() => {
  cleanup()
  $subagentsBySession.set({})
})

// Acceptance criterion 6: exactly one empty-state rendering for the Spawn
// tree tab. SubagentTree used to carry its own divergent "No live subagents"
// block for the exact case SpawnTreeView already handles via PanelEmpty.
it('renders exactly one empty state when there are no live subagents', () => {
  $subagentsBySession.set({})
  render(<SpawnTreeView />)
  expect(screen.getAllByText('No live subagents')).toHaveLength(1)
  expect(screen.getAllByText('When a turn delegates work, child agents stream their progress here.')).toHaveLength(1)
})
