import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $gatewayState } from '@/store/session'
import {
  $catchingUpSessionIds,
  $turnLostSessionIds,
  clearAllSessionStates,
  dismissTurnLost
} from '@/store/session-states'

import { ReconnectStatusBanner } from './reconnect-banner'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { close: 'Close' },
      composer: {
        catchingUpNotice: 'Reconnected — catching up…',
        reconnectingBanner: 'Reconnecting to Hermes — you can keep reading and typing.',
        turnLostNotice: 'This turn may not have completed during the disconnect.',
        turnLostRegenerate: 'Regenerate'
      }
    }
  })
}))

// Priority + scoping for the composer's fast, non-modal reconnect surface:
// turn-lost (needs a decision) > catching-up (transient) > plain reconnecting
// (no session-specific claim) > nothing. Only one banner renders at a time,
// and it must never leak another session's story into this composer.
describe('ReconnectStatusBanner', () => {
  beforeEach(() => {
    $gatewayState.set('open')
    clearAllSessionStates()
  })

  afterEach(() => {
    cleanup()
    $gatewayState.set('open')
    clearAllSessionStates()
  })

  it('renders nothing when the gateway is open and the session is not tracked', () => {
    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.queryByRole('status')).toBeNull()
  })

  it('shows the plain reconnecting banner when the gateway is not open and no session-specific state exists', () => {
    $gatewayState.set('closed')

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.getByText('Reconnecting to Hermes — you can keep reading and typing.')).toBeTruthy()
  })

  it('also treats an "error" gateway state as reconnecting', () => {
    $gatewayState.set('error')

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.getByText('Reconnecting to Hermes — you can keep reading and typing.')).toBeTruthy()
  })

  it('shows the catching-up notice for a tracked session even once the gateway is open again', () => {
    $catchingUpSessionIds.set(['s1'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.getByText('Reconnected — catching up…')).toBeTruthy()
  })

  it('scopes catching-up to the composer\'s own stored session id', () => {
    $catchingUpSessionIds.set(['other-session'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.queryByText('Reconnected — catching up…')).toBeNull()
  })

  it('prioritizes turn-lost over catching-up when both are somehow set for the same session', () => {
    $catchingUpSessionIds.set(['s1'])
    $turnLostSessionIds.set(['s1'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.getByText('This turn may not have completed during the disconnect.')).toBeTruthy()
    expect(screen.queryByText('Reconnected — catching up…')).toBeNull()
  })

  it('prioritizes turn-lost over the plain reconnecting banner', () => {
    $gatewayState.set('closed')
    $turnLostSessionIds.set(['s1'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.getByText('This turn may not have completed during the disconnect.')).toBeTruthy()
    expect(screen.queryByText('Reconnecting to Hermes — you can keep reading and typing.')).toBeNull()
  })

  it('wires the Regenerate action to onReload with a null parent id', () => {
    $turnLostSessionIds.set(['s1'])
    const onReload = vi.fn().mockResolvedValue(undefined)

    render(<ReconnectStatusBanner onReload={onReload} storedSessionId="s1" />)

    fireEvent.click(screen.getByText('Regenerate'))

    expect(onReload).toHaveBeenCalledWith(null)
  })

  it('omits the Regenerate button when no onReload handler is supplied', () => {
    $turnLostSessionIds.set(['s1'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.queryByText('Regenerate')).toBeNull()
  })

  it('dismissing the turn-lost banner clears the tracked mark for that session', () => {
    $turnLostSessionIds.set(['s1'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)
    fireEvent.click(screen.getByText('Close'))

    expect($turnLostSessionIds.get()).not.toContain('s1')
  })

  it('does not scope turn-lost to a different session', () => {
    $turnLostSessionIds.set(['other-session'])

    render(<ReconnectStatusBanner storedSessionId="s1" />)

    expect(screen.queryByText('This turn may not have completed during the disconnect.')).toBeNull()
  })

  it('renders nothing without a stored session id even if some other session is catching up or lost', () => {
    $catchingUpSessionIds.set(['s1'])
    $turnLostSessionIds.set(['s2'])

    render(<ReconnectStatusBanner storedSessionId={null} />)

    expect(screen.queryByRole('status')).toBeNull()
  })

  it('exposes dismissTurnLost as a safe no-op for an untracked session', () => {
    expect(() => dismissTurnLost('untracked')).not.toThrow()
  })
})
