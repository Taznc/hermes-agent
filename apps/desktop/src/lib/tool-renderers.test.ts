import { describe, expect, it } from 'vitest'

import type { Contribution } from '@/contrib/types'

import { resolveToolRenderer, type ToolRendererContribution } from './tool-renderers'

function contribution(id: string, data: ToolRendererContribution): Contribution {
  return { area: 'toolRenderers', data, id }
}

describe('resolveToolRenderer', () => {
  it('returns undefined when nobody claims the tool name', () => {
    const contributions = [contribution('a', { toolName: 'other', render: () => null })]

    expect(resolveToolRenderer(contributions, 'clarify')).toBeUndefined()
  })

  it('returns undefined for an empty registry snapshot', () => {
    expect(resolveToolRenderer([], 'clarify')).toBeUndefined()
  })

  it('resolves the single matching contribution', () => {
    const render = () => null
    const contributions = [contribution('mine', { toolName: 'clarify', render })]

    expect(resolveToolRenderer(contributions, 'clarify')).toEqual({ id: 'mine', render })
  })

  it('is last-match-wins for a duplicate toolName, not first', () => {
    const firstRender = () => null
    const secondRender = () => null

    const contributions = [
      contribution('first', { toolName: 'clarify', render: firstRender }),
      contribution('second', { toolName: 'clarify', render: secondRender })
    ]

    const resolved = resolveToolRenderer(contributions, 'clarify')

    expect(resolved?.id).toBe('second')
    expect(resolved?.render).toBe(secondRender)
  })

  it('ignores a contribution whose data is missing or malformed', () => {
    const contributions: Contribution[] = [
      { area: 'toolRenderers', id: 'no-data' },
      { area: 'toolRenderers', data: { toolName: 'clarify' }, id: 'no-render' } as Contribution
    ]

    expect(resolveToolRenderer(contributions, 'clarify')).toBeUndefined()
  })

  it('only matches contributions targeting the exact tool name', () => {
    const clarifyRender = () => null

    const contributions = [
      contribution('a', { toolName: 'setup_mcp', render: () => null }),
      contribution('b', { toolName: 'clarify', render: clarifyRender }),
      contribution('c', { toolName: 'delegate_task', render: () => null })
    ]

    expect(resolveToolRenderer(contributions, 'clarify')).toEqual({ id: 'b', render: clarifyRender })
  })
})
