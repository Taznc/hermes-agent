import { act, cleanup, render, renderHook } from '@testing-library/react'
import type * as ReactModule from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useStatusbarItems } from '@/app/shell/hooks/use-statusbar-items'
import { StatusbarControls } from '@/app/shell/statusbar-controls'
import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { I18nProvider, useI18n } from '@/i18n'
import { en } from '@/i18n/en'
import { statusBarGatewayHealth } from '@/lib/gateway-health-pill'
import type { GatewayHealthPillInput } from '@/lib/gateway-health-pill'
import type * as HealthModule from '@/lib/gateway-health-pill'
import { $currentUsage } from '@/store/session'
import { $statusbarHiddenIds } from '@/store/statusbar-prefs'
import { $gatewayRestarting } from '@/store/system-actions'

import { useMemoizedStatusBarGatewayHealth } from './statusbar-gateway-health'

const counts = vi.hoisted(() => ({ left: 0, hook: 0 }))
const leftIds = vi.hoisted(() => new Set(['command-center', 'gateway-health', 'agents', 'cron', 'webhooks']))
vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof ReactModule>()

  return {
    ...actual,
    memo: (component: Parameters<typeof actual.memo>[0], compare: Parameters<typeof actual.memo>[1]) => {
      if (component.name !== 'StatusbarItemView') {
        return actual.memo(component, compare)
      }

      return actual.memo(function CountedItem(props: { item: { id: string } }) {
        if (leftIds.has(props.item.id)) {
          counts.left++
        }

        return (component as (props: unknown) => ReturnType<typeof actual.createElement>)(props)
      }, compare)
    }
  }
})
vi.mock('@/lib/gateway-health-pill', async importOriginal => {
  const actual = await importOriginal<typeof HealthModule>()

  return { ...actual, statusBarGatewayHealth: vi.fn(actual.statusBarGatewayHealth) }
})
vi.mock('@/app/shell/hooks/use-context-breakdown', () => ({
  useContextBreakdown: () => ({ breakdown: null, loading: false })
}))
vi.mock('@/app/shell/approval-mode-menu', () => ({ useApprovalModeStatusbarItem: () => null }))
vi.mock('@/app/shell/system-resources-statusbar', () => ({ useSystemResourcesStatusbarItem: () => null }))

const options = {
  agentsOpen: false,
  chatOpen: true,
  commandCenterOpen: false,
  extraLeftItems: [],
  extraRightItems: [],
  freshDraftReady: false,
  gatewayState: 'open',
  inferenceStatus: null,
  openAgents: () => {},
  openCommandCenterSection: () => {},
  requestGateway: async () => undefined as never,
  statusSnapshot: null,
  toggleCommandCenter: () => {}
}

beforeEach(() => {
  $statusbarHiddenIds.set([])
  $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'health-perf' }))
  $gatewayRestarting.set(false)

  $currentUsage.set({ calls: 0, input: 0, output: 0, total: 0 })
  vi.mocked(statusBarGatewayHealth).mockClear()
  counts.left = 0
})
afterEach(() => {
  cleanup()
  $gatewayRestarting.set(false)
})

describe('production statusbar health memo seam', () => {
  it('20 usage updates avoid health computations and five actual memoized left-child renders', () => {
    function Harness() {
      counts.hook++
      const { leftStatusbarItems, statusbarItems } = useStatusbarItems(options)

      return <StatusbarControls items={statusbarItems.filter(Boolean)} leftItems={leftStatusbarItems} />
    }

    const view = render(
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    )

    expect(counts.left).toBe(5)
    vi.mocked(statusBarGatewayHealth).mockClear()
    counts.left = 0
    counts.hook = 0

    for (let n = 1; n <= 20; n++) {
      act(() => $currentUsage.set({ calls: n, input: n, output: n, total: n * 2 }))
    }

    process.stdout.write(
      `usage-operation-counts ${JSON.stringify({ hook: counts.hook, health: vi.mocked(statusBarGatewayHealth).mock.calls.length, left: counts.left })}\n`
    )
    expect(vi.mocked(statusBarGatewayHealth)).toHaveBeenCalledTimes(0)
    expect(counts.left).toBe(0)
    expect(counts.hook).toBe(20)
    expect(view.container.querySelector('[data-slot="statusbar"]')).not.toBeNull()
  })

  it('keeps left identity on unrelated renders but invalidates connection, language and restart', async () => {
    const { result, rerender } = renderHook(props => ({ ...useStatusbarItems(props), i18n: useI18n() }), {
      initialProps: options,
      wrapper: ({ children }) => <I18nProvider configClient={null}>{children}</I18nProvider>
    })

    const left = result.current.leftStatusbarItems
    rerender({ ...options })
    expect(result.current.leftStatusbarItems).toBe(left)
    rerender({ ...options, gatewayState: 'connecting' })
    expect(result.current.leftStatusbarItems.find(item => item.id === 'gateway-health')?.detail).toBe('connecting')
    await act(() => result.current.i18n.setLocale('ja'))
    expect(result.current.leftStatusbarItems.find(item => item.id === 'gateway-health')?.label).toBe('バックエンド')
    act(() => $gatewayRestarting.set(true))
    expect(result.current.leftStatusbarItems.find(item => item.id === 'gateway-health')?.detail).not.toBe('connecting')
  })
})

const copy = {
  backend: en.shell.statusbar.backend,
  checking: en.shell.statusbar.gatewayChecking,
  connecting: en.shell.statusbar.gatewayConnecting,
  messagingDegraded: en.shell.statusbar.messagingDegraded,
  messagingStopped: en.shell.statusbar.messagingStopped,
  needsSetup: en.shell.statusbar.gatewayNeedsSetup,
  offline: en.shell.statusbar.gatewayOffline,
  ready: en.shell.statusbar.gatewayReady,
  restarting: en.shell.statusbar.gatewayRestarting,
  unavailable: en.shell.statusbar.gatewayUnavailable
}

const input: GatewayHealthPillInput = { connectionState: 'open', copy, inferenceStatus: null }

describe('health helper semantic dependencies', () => {
  it('reuses fresh wrapper objects but invalidates each copy field/function', () => {
    const { result, rerender } = renderHook(useMemoizedStatusBarGatewayHealth, { initialProps: input })
    const initial = result.current
    vi.mocked(statusBarGatewayHealth).mockClear()
    rerender({ ...input, copy: { ...copy } })
    expect(result.current).toBe(initial)
    expect(statusBarGatewayHealth).not.toHaveBeenCalled()

    for (const key of Object.keys(copy) as (keyof typeof copy)[]) {
      const changed = { ...copy, [key]: key === 'messagingDegraded' ? (name: string) => `new ${name}` : `new ${key}` }
      vi.mocked(statusBarGatewayHealth).mockClear()
      rerender({ ...input, copy: changed })
      expect(statusBarGatewayHealth).toHaveBeenCalledTimes(1)
      expect(result.current).toEqual(statusBarGatewayHealth({ ...input, copy: changed }))
    }
  })

  it.each<Partial<GatewayHealthPillInput>>([
    { connectionState: 'closed' },
    { connectionState: 'connecting' },
    { inferenceStatus: { ready: true, reason: null, checksDisagree: false, source: 'runtime_check' } },
    { messagingRunning: false, messagingState: 'stopped' },
    { messagingState: 'startup_failed' },
    { messagingRunning: true },
    { platforms: { discord: { state: 'fatal' } } },
    { platforms: { discord: { state: 'connecting' } } },
    { platforms: { discord: { state: 'connected' } } },
    { restarting: true }
  ])('matches the unchanged upstream derivation for %j', change => {
    const { result, rerender } = renderHook(useMemoizedStatusBarGatewayHealth, { initialProps: input })
    const previous = result.current
    vi.mocked(statusBarGatewayHealth).mockClear()
    const next = { ...input, ...change }
    rerender(next)
    expect(statusBarGatewayHealth).toHaveBeenCalledTimes(1)
    expect(result.current).not.toBe(previous)
    expect(result.current).toEqual(statusBarGatewayHealth(next))
  })
})
