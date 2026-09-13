import { describe, expect, it, vi } from 'vitest'

import { buildActionDraft, getHermesActions, openHermesAction, resolveActionCwd } from './session-actions'
import type { BoardMeta, KanbanRun, KanbanTaskFull } from './types'

const task = (status: string, extra: Partial<KanbanTaskFull> = {}): KanbanTaskFull => ({
  id: 't_exact123',
  status,
  title: 'Seeded action card',
  ...extra
})

const board: BoardMeta = {
  default_workdir: '/board/project',
  name: 'Shipping Board',
  project_id: 'project-42',
  project_name: 'Hermes Desktop',
  slug: 'shipping'
}

const run = (status: string, id: number): KanbanRun => ({ id, status })

describe('seeded Hermes action catalog', () => {
  it.each([
    ['idea', ['Explain this card', 'Rough this out']],
    ['roadmap', ['Explain this card', 'Scope this']],
    ['blocked', ['Explain this card', 'Help me unblock it']],
    ['ready', ['Explain this card']]
  ])('shows the actions for %s', (status, expected) => {
    expect(getHermesActions(task(status), [])).toEqual(expected.map(action => expect.objectContaining({ label: action })))
  })

  it('adds failure investigation for a consecutive failure or errored latest run', () => {
    expect(getHermesActions(task('todo', { consecutive_failures: 2 }), []).map(action => action.label)).toEqual([
      'Explain this card',
      'Investigate the failure'
    ])
    expect(getHermesActions(task('done'), [run('completed', 1), run('errored', 2)]).map(action => action.label)).toEqual([
      'Explain this card',
      'Investigate the failure'
    ])
    expect(getHermesActions(task('done'), [run('errored', 1), run('completed', 2)]).map(action => action.label)).toEqual([
      'Explain this card'
    ])
  })

  it('resolves a non-empty workspace override before the board default', () => {
    expect(resolveActionCwd(task('todo', { workspace_path: ' /card/worktree ' }), board)).toBe('/card/worktree')
    expect(resolveActionCwd(task('todo', { workspace_path: '   ' }), board)).toBe('/board/project')
    expect(resolveActionCwd(task('todo'), { ...board, default_workdir: '' })).toBeUndefined()
  })

  it.each(['explain', 'rough', 'scope', 'unblock', 'failure'] as const)(
    'builds the %s intent around the exact card and board context',
    id => {
      const action = [
        ...getHermesActions(task('idea', { consecutive_failures: 1 }), [run('errored', 9)]),
        ...getHermesActions(task('roadmap'), []),
        ...getHermesActions(task('blocked'), [])
      ].find(candidate => candidate.id === id)!

      const draft = buildActionDraft(action, {
        board,
        commentsCount: 3,
        runs: [run('errored', 9)],
        task: task('blocked')
      })

      expect(draft).toContain('t_exact123')
      expect(draft).toContain('board shipping')
      expect(draft).toContain('Hermes Desktop')
      expect(draft).toContain(action.intent)
    }
  )

  it('opens only a fresh editable draft and performs no operation when cwd is unavailable', () => {
    const open = vi.fn()
    const action = getHermesActions(task('ready'), [])[0]
    const context = { board, commentsCount: 0, runs: [], task: task('ready') }

    expect(openHermesAction(action, context, open)).toBe(true)
    expect(open).toHaveBeenCalledWith({
      cwd: '/board/project',
      draft: expect.stringContaining('t_exact123'),
      openTab: true
    })
    expect(open.mock.calls[0][0]).not.toHaveProperty('send')
    expect(open.mock.calls[0][0]).not.toHaveProperty('submit')

    open.mockClear()
    expect(openHermesAction(action, { ...context, board: { ...board, default_workdir: null } }, open)).toBe(false)
    expect(open).not.toHaveBeenCalled()

    expect(openHermesAction(action, { ...context, board: { ...board, slug: '' } }, open)).toBe(false)
    expect(open).not.toHaveBeenCalled()
  })
})
