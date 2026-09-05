import { describe, expect, it } from 'vitest'

import { FORK_LOCAL_SCOPE, forkBackendScopeKey, forkScopeChangedBackend, forkScopeConnection } from './profile-scope'

// These pin the distinction store/profile.ts's routing subscription depends on:
// a backend switch (different machine — must wipe the session list) versus a
// profile switch within one backend (must NOT wipe; the rows are still the
// user's). Getting it wrong either strands the previous machine's sessions on
// screen or throws away rows the user is still looking at.

describe('forkBackendScopeKey', () => {
  it('scopes a null connection to the local pool', () => {
    expect(forkBackendScopeKey(null, 'default')).toBe(`${FORK_LOCAL_SCOPE}::default`)
  })

  it('distinguishes the same profile name on different sources', () => {
    // The whole reason the key is a pair: these must not compare equal.
    expect(forkBackendScopeKey(null, 'default')).not.toBe(forkBackendScopeKey('remote-box', 'default'))
  })

  it('round-trips the connection half', () => {
    expect(forkScopeConnection(forkBackendScopeKey('remote-box', 'default'))).toBe('remote-box')
    expect(forkScopeConnection(forkBackendScopeKey(null, 'default'))).toBe(FORK_LOCAL_SCOPE)
  })
})

describe('forkScopeChangedBackend', () => {
  it('is true when the machine changed', () => {
    expect(forkScopeChangedBackend(forkBackendScopeKey(null, 'default'), forkBackendScopeKey('remote', 'default'))).toBe(
      true
    )
    expect(
      forkScopeChangedBackend(forkBackendScopeKey('remote', 'default'), forkBackendScopeKey('other', 'default'))
    ).toBe(true)
    expect(forkScopeChangedBackend(forkBackendScopeKey('remote', 'default'), forkBackendScopeKey(null, 'default'))).toBe(
      true
    )
  })

  it('is false when only the profile changed on the same machine', () => {
    expect(forkScopeChangedBackend(forkBackendScopeKey(null, 'default'), forkBackendScopeKey(null, 'work'))).toBe(false)
    expect(
      forkScopeChangedBackend(forkBackendScopeKey('remote', 'default'), forkBackendScopeKey('remote', 'work'))
    ).toBe(false)
  })

  it('treats a profile name containing the separator as one profile, not a source change', () => {
    const previous = forkBackendScopeKey(null, 'a::b')
    const next = forkBackendScopeKey(null, 'c::d')

    expect(forkScopeConnection(previous)).toBe(FORK_LOCAL_SCOPE)
    expect(forkScopeChangedBackend(previous, next)).toBe(false)
  })
})
