import { act, cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import type * as ConnectionsStore from '@/store/connections'
import type * as GatewayStore from '@/store/gateway'
import { $gateway } from '@/store/gateway'
import type * as ProfileStore from '@/store/profile'
import { $activeGatewayProfile } from '@/store/profile'
import type * as SessionStore from '@/store/session'

import { ChatRoutesSurface } from './surfaces'
import type { WiringActions } from './types'

vi.mock('@/contrib/react/use-contributions', () => ({ useContributions: vi.fn() }))
// Spread the real module and override only what this surface reads — see the
// note on the @/store/session mock below.
vi.mock('@/store/connections', async importOriginal => ({
  ...(await importOriginal<typeof ConnectionsStore>()),
  $activeConnectionId: atom('local')
}))
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<typeof GatewayStore>()),
  $gateway: atom<unknown>(null)
}))
vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<typeof ProfileStore>()),
  $activeGatewayProfile: atom('default')
}))
// Only the two stores this surface actually reads are stubbed; everything else
// falls through to the real module. Enumerating the whole export surface here
// meant every upstream store added anywhere in the import graph broke this file
// with "No <X> export is defined on the mock" — three such exports appeared in
// one sync ($sessions, $cronSessions, $messagingSessions), none of them used by
// the code under test.
vi.mock('@/store/session', async importOriginal => ({
  ...(await importOriginal<typeof SessionStore>()),
  $freshDraftReady: atom(false),
  $gatewayState: atom('open')
}))
vi.mock('../chat', () => ({
  ChatView: ({ gateway }: { gateway: { id?: string } | null }) => <div data-testid="gateway">{gateway?.id}</div>
}))
vi.mock('../chat/sidebar', () => ({ ChatSidebar: () => null }))
vi.mock('../right-sidebar/terminal/chrome', () => ({ TerminalPaneChrome: () => null }))
vi.mock('../shell/hooks/use-status-snapshot', () => ({ useStatusSnapshot: () => ({}) }))
vi.mock('../shell/hooks/use-statusbar-items', () => ({
  useStatusbarItems: () => ({ leftStatusbarItems: [], statusbarItems: [] })
}))
vi.mock('../shell/statusbar-controls', () => ({ StatusbarControls: () => null }))
vi.mock('../routes', () => ({
  contributedRoutes: () => [],
  NEW_CHAT_ROUTE: '/new',
  ROUTES_AREA: 'routes',
  sessionRoute: (id: string) => `/${id}`
}))
vi.mock('./latest-actions', () => ({ latestChatActions: () => ({}), latestSidebarActions: () => ({}) }))
vi.mock('./panes', () => ({ setStatusbarItemGroup: vi.fn(), useStatusbarContributions: () => [] }))
vi.mock('../shell/model-menu-panel', () => ({ ModelMenuPanel: () => null }))

afterEach(() => {
  cleanup()
  $gateway.set(null)
  $activeGatewayProfile.set('default')
})

describe('ChatRoutesSurface', () => {
  it('passes the live gateway after an open-to-open profile switch', () => {
    const gatewayA = { id: 'a' } as unknown as HermesGateway
    const gatewayB = { id: 'b' } as unknown as HermesGateway

    $gateway.set(gatewayA)
    const actions = { getGateway: () => $gateway.get() } as unknown as WiringActions

    render(
      <MemoryRouter>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    expect(screen.getByTestId('gateway').textContent).toBe('a')

    act(() => {
      $gateway.set(gatewayB)
      $activeGatewayProfile.set('other')
    })

    expect(screen.getByTestId('gateway').textContent).toBe('b')
  })
})
