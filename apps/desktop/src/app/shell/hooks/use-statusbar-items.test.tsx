import { act, cleanup, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $currentUsage } from '@/store/session'

import { useStatusbarItems } from './use-statusbar-items'

function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

const baseProps = {
  agentsOpen: false,
  chatOpen: true,
  commandCenterOpen: false,
  extraLeftItems: [],
  extraRightItems: [],
  gatewayState: 'open',
  inferenceStatus: { checksDisagree: false, ready: true, reason: null, source: 'runtime_check' as const },
  openAgents: () => undefined,
  openCommandCenterSection: () => undefined,
  freshDraftReady: false,
  requestGateway: async () => ({}) as never,
  statusSnapshot: null,
  toggleCommandCenter: () => undefined
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  $currentUsage.set({ calls: 0, input: 0, output: 0, total: 0 })
})

describe('useStatusbarItems render identity', () => {
  // Regression guard for the perf fix in commit 41dd895e12: StatusbarItemView
  // memoizes on item identity, so an unrelated store tick (a streamed usage
  // delta) must NOT rebuild the item array — otherwise every streaming token
  // repaints the whole bar.
  it('keeps the left item array reference stable across an unrelated store tick', () => {
    const { result, rerender } = renderHook(() => useStatusbarItems(baseProps), { wrapper })

    const before = result.current.leftStatusbarItems

    act(() => {
      $currentUsage.set({ calls: 1, input: 10, output: 5, total: 15 })
    })
    rerender()

    expect(result.current.leftStatusbarItems).toBe(before)
  })

  it('hides the interactive terminal affordance when the PTY bridge is absent', () => {
    vi.stubGlobal('hermesDesktop', undefined)

    const { result } = renderHook(() => useStatusbarItems(baseProps), { wrapper })

    expect(result.current.statusbarItems.find(item => item.id === 'terminal')?.hidden).toBe(true)
  })

  it('keeps the gateway-health item identity stable when props are unchanged', () => {
    const { result, rerender } = renderHook(() => useStatusbarItems(baseProps), { wrapper })

    const beforeItem = result.current.leftStatusbarItems.find(item => item.id === 'gateway-health')
    rerender()
    const afterItem = result.current.leftStatusbarItems.find(item => item.id === 'gateway-health')

    expect(afterItem).toBe(beforeItem)
  })
})
