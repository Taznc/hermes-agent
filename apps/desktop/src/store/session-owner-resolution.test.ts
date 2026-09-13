import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connectionsRegistry } from './connections'
import { $profiles } from './profile'
import {
  ambientGatewayOwnsEverySession,
  assertSessionOwnerResolved,
  sessionOwnerIsKnown
} from './session-owner-resolution'

const registry = (...ids: string[]) =>
  ({
    connections: ids.map(id => ({ id })),
    lastUsed: ids[0] ?? null,
    launchMode: 'primary',
    primary: ids[0] ?? null
  }) as never

// A host that owns a connection registry (real Electron, any version): the
// bridge always exposes getConnectionConfig, the sentinel connectionsManagedByHost
// gates on. Tests below that model Electron topology (with or without a live
// registry) call this so "no registry" and "no host capability" stay distinct.
function setElectronHost(extra: Record<string, unknown> = {}): void {
  ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
    getConnectionConfig: vi.fn(async () => ({})),
    ...extra
  }
}

beforeEach(() => {
  $connectionsRegistry.set(null)
  $profiles.set([])
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('session owner topology', () => {
  it('fails closed while the modern registry bridge is present but its async cache is not loaded', () => {
    setElectronHost({
      connections: { list: vi.fn(async () => Promise.reject(new Error('ipc unavailable'))) }
    })
    $connectionsRegistry.set(null)
    $profiles.set([{ name: 'default' }] as never)

    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() =>
      assertSessionOwnerResolved('default', { method: 'session.resume', sessionId: 'registry-loading' })
    ).not.toThrow()
    expect(() => assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'registry-loading' })).toThrow(
      /could not be resolved/i
    )
  })

  it('fails closed on an unknown owner in registry topology while preserving legacy profile routes', () => {
    // A connection registry means the ambient gateway is never provably the
    // sole backend, even with one profile listed: an unknown owner fails
    // closed. A bare profile still names a backend — the legacy profile door
    // (a pick on the primary / explicit `local` source) mints sessions owned
    // by that profile's pool socket in every topology.
    setElectronHost()
    $connectionsRegistry.set(registry('local'))
    $profiles.set([{ name: 'default' }] as never)

    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() =>
      assertSessionOwnerResolved('default', { method: 'session.resume', sessionId: 'registry-profile' })
    ).not.toThrow()
    expect(() => assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'unknown-owner' })).toThrow(
      /could not be resolved/i
    )

    $connectionsRegistry.set(registry('local', 'homelab'))
    expect(sessionOwnerIsKnown(null)).toBe(false)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() => assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'unknown-owner' })).toThrow(
      /could not be resolved/i
    )

    $connectionsRegistry.set(null)
    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(true)
    expect(() =>
      assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'legacy-single-profile' })
    ).not.toThrow()

    $profiles.set([{ name: 'default' }, { name: 'loki' }] as never)
    expect(sessionOwnerIsKnown('loki')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() =>
      assertSessionOwnerResolved('loki', { method: 'session.resume', sessionId: 'legacy-profile-owner' })
    ).not.toThrow()
  })

  it('treats the ambient gateway as sole owner on a host with no connection registry, regardless of profile count', () => {
    // The web-bridge shim (browser-served Desktop, no Electron main process)
    // deliberately omits getConnectionConfig — see host-connections.ts. It
    // serves every profile through the SAME physical backend process, scoped
    // only by a `profile` request param, never a distinct socket per
    // profile. Profile count is therefore not evidence of multiple backends
    // here the way it is for Electron's pool, and the six-profile dev-VM
    // deployment that regressed this (session-control RPCs throwing
    // SessionOwnerResolutionError) must resolve to "ambient owns it".
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    $connectionsRegistry.set(null)
    $profiles.set([
      { name: 'default' },
      { name: 'claudecode' },
      { name: 'claudeprimary' },
      { name: 'debugger' },
      { name: 'orchestrator' },
      { name: 'reviewer' }
    ] as never)

    expect(ambientGatewayOwnsEverySession()).toBe(true)
    expect(() =>
      assertSessionOwnerResolved(null, { method: 'session.control.read', sessionId: 'web-bridge-multi-profile' })
    ).not.toThrow()
  })
})
