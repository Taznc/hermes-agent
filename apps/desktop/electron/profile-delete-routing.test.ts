import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  assertLocalProfileCanStart,
  decideProfileDeleteAction,
  localProfilePoolKeys,
  ProfileDeletionGate,
  profileNameFromDeleteRequest,
  resolveRouteProfile,
  resolveStoredDesktopProfile
} from './profile-delete-routing'

// ---------------------------------------------------------------------------
// profileNameFromDeleteRequest
// ---------------------------------------------------------------------------

test('profileNameFromDeleteRequest parses a DELETE /api/profiles/<name> path', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/worker' }), 'worker')
})

test('profileNameFromDeleteRequest lowercases the profile name', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/Worker' }), 'worker')
})

test('profileNameFromDeleteRequest returns null for non-DELETE methods', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'GET', path: '/api/profiles/worker' }), null)
})

test('profileNameFromDeleteRequest returns null when the path does not match', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/sessions' }), null)
})

test('profileNameFromDeleteRequest returns null for an empty/whitespace name', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/%20' }), null)
})

test('profileNameFromDeleteRequest returns null for an undecodable path segment', () => {
  assert.equal(profileNameFromDeleteRequest({ method: 'DELETE', path: '/api/profiles/%E0%A4%A' }), null)
})

// ---------------------------------------------------------------------------
// decideProfileDeleteAction
// ---------------------------------------------------------------------------

const deps = {
  isDefaultProfile: p => p === 'default',
  isValidProfileName: p => /^[a-z0-9][a-z0-9_-]{0,63}$/.test(p),
  primaryProfileKey: () => 'primary-profile'
}

test('decideProfileDeleteAction is a noop for the default profile', () => {
  assert.deepEqual(decideProfileDeleteAction('default', deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction is a noop for null (no profile parsed)', () => {
  assert.deepEqual(decideProfileDeleteAction(null, deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction is a noop for an invalid profile name', () => {
  assert.deepEqual(decideProfileDeleteAction('Not Valid!', deps), { action: 'noop', profile: null })
})

test('decideProfileDeleteAction tears down the primary backend for the primary profile', () => {
  assert.deepEqual(decideProfileDeleteAction('primary-profile', deps), {
    action: 'teardown-primary',
    profile: 'primary-profile'
  })
})

test('decideProfileDeleteAction tears down the pool backend for any other valid profile', () => {
  assert.deepEqual(decideProfileDeleteAction('worker', deps), { action: 'teardown-pool', profile: 'worker' })
})

// ---------------------------------------------------------------------------
// resolveRouteProfile
// ---------------------------------------------------------------------------

test('resolveRouteProfile routes to the primary backend (null) when a profile was torn down', () => {
  assert.equal(resolveRouteProfile('worker', 'other-profile'), null)
})

test('resolveRouteProfile passes the requested profile through when nothing was torn down', () => {
  assert.equal(resolveRouteProfile(null, 'other-profile'), 'other-profile')
})

test('resolveRouteProfile passes through undefined when nothing was torn down and no profile was requested', () => {
  assert.equal(resolveRouteProfile(null, undefined), undefined)
})

// ---------------------------------------------------------------------------
// ProfileDeletionGate / localProfilePoolKeys
// ---------------------------------------------------------------------------

test('ProfileDeletionGate blocks concurrent starts until deletion releases', () => {
  const gate = new ProfileDeletionGate()
  const release = gate.acquire('Selena')

  assert.equal(gate.blocks('selena'), true)
  assert.equal(gate.blocks('trina'), false)

  release()
  assert.equal(gate.blocks('selena'), false)
})

test('ProfileDeletionGate keeps overlapping deletion leases blocked', () => {
  const gate = new ProfileDeletionGate()
  const releaseFirst = gate.acquire('selena')
  const releaseSecond = gate.acquire('selena')

  releaseFirst()
  assert.equal(gate.blocks('selena'), true)

  releaseSecond()
  assert.equal(gate.blocks('selena'), false)
})

test('ProfileDeletionGate rejects a deferred start when deletion begins while it waits', async () => {
  const gate = new ProfileDeletionGate()
  let continueStart = () => undefined

  const waiting = new Promise<void>(resolve => {
    continueStart = resolve
  })

  const start = (async () => {
    await waiting
    gate.assertCanStart('selena')
  })()

  const release = gate.acquire('selena')

  continueStart()
  await assert.rejects(start, /Profile "selena" is being deleted/)
  release()
})

test('assertLocalProfileCanStart rejects a delayed retry after the profile directory is removed', () => {
  const gate = new ProfileDeletionGate()

  assert.throws(() => assertLocalProfileCanStart('selena', gate, () => false), /Profile "selena" no longer exists/)
  assert.doesNotThrow(() => assertLocalProfileCanStart('default', gate, () => false))
  assert.doesNotThrow(() => assertLocalProfileCanStart('selena', gate, profile => profile === 'selena'))
})

test('localProfilePoolKeys returns every local process scope for one profile', () => {
  assert.deepEqual(localProfilePoolKeys('Selena'), ['selena', 'conn:local::selena'])
  assert.deepEqual(localProfilePoolKeys(''), [])
})

test('resolveRouteProfile preserves a primary-backend route from another routing policy', () => {
  assert.equal(resolveRouteProfile(null, null), null)
})

// ---------------------------------------------------------------------------
// resolveStoredDesktopProfile
//
// Regression: the desktop's stored profile preference was validated for NAME
// FORMAT but never for EXISTENCE. A preference naming a profile that isn't
// installed on this machine (deleted elsewhere, config synced between
// machines, restored backup) routed every profile-scoped REST call at a
// profile the backend will never have — config, env, model info, schema and
// sessions each answering `404 Profile 'x' does not exist.` in a loop that
// nothing self-healed.
// ---------------------------------------------------------------------------

const validName = (profile: string) => /^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile)
const noProfilesOnDisk = () => false
const allProfilesOnDisk = () => true

test('resolveStoredDesktopProfile keeps a stored profile that exists on disk', () => {
  assert.equal(
    resolveStoredDesktopProfile('worker', validName, profile => profile === 'worker'),
    'worker'
  )
})

// The core fix: well-formed, but not installed here → no preference.
test('resolveStoredDesktopProfile drops a well-formed profile that is not installed', () => {
  assert.equal(resolveStoredDesktopProfile('claudeprimary', validName, noProfilesOnDisk), null)
})

// "default" is the root HERMES_HOME — it has no profiles/<name> directory, so
// an existence check must never reject it or the app self-heals into nothing.
test('resolveStoredDesktopProfile always accepts default without a directory check', () => {
  assert.equal(resolveStoredDesktopProfile('default', validName, noProfilesOnDisk), 'default')
})

test('resolveStoredDesktopProfile treats an absent or blank preference as unset', () => {
  assert.equal(resolveStoredDesktopProfile('', validName, allProfilesOnDisk), null)
  assert.equal(resolveStoredDesktopProfile('   ', validName, allProfilesOnDisk), null)
  assert.equal(resolveStoredDesktopProfile(null, validName, allProfilesOnDisk), null)
  assert.equal(resolveStoredDesktopProfile(undefined, validName, allProfilesOnDisk), null)
})

// Format validation still applies: a malformed name must not reach the
// filesystem probe as a path segment.
test('resolveStoredDesktopProfile rejects a malformed name before probing disk', () => {
  let probed = false

  const result = resolveStoredDesktopProfile('../escape', validName, () => {
    probed = true

    return true
  })

  assert.equal(result, null)
  assert.equal(probed, false)
})

test('resolveStoredDesktopProfile trims surrounding whitespace before resolving', () => {
  assert.equal(
    resolveStoredDesktopProfile('  worker  ', validName, profile => profile === 'worker'),
    'worker'
  )
})
