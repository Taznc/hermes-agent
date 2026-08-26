import assert from 'node:assert/strict'

import { beforeEach, test } from 'vitest'

import {
  __resetMissingProfiles,
  isMissingProfileError,
  isProfileKnownMissing,
  markProfileMissing,
  noteProfileError
} from './profile-liveness'

beforeEach(() => {
  __resetMissingProfiles()
})

// ---------------------------------------------------------------------------
// isMissingProfileError — the two messages assertLocalProfileCanStart throws
// ---------------------------------------------------------------------------

test('isMissingProfileError matches the deleted-profile rejection', () => {
  assert.equal(isMissingProfileError(new Error('Profile "claudeprimary" no longer exists.')), true)
})

test('isMissingProfileError matches the delete-in-flight rejection', () => {
  assert.equal(isMissingProfileError(new Error('Profile "worker" is being deleted.')), true)
})

// A plain probe miss must stay probeable: treating a 404 as "profile gone"
// would blacklist a live profile that simply didn't hold the session id, and
// every later lookup would skip the profile that actually owns the session.
test('isMissingProfileError ignores a session-not-found 404', () => {
  assert.equal(isMissingProfileError(new Error('404: {"detail":"Session not found"}')), false)
})

// This 404 comes from a backend that IS running, answering about some other
// profile — a different condition from the spawn guard refusing to start one.
test('isMissingProfileError ignores a backend "does not exist" 404', () => {
  assert.equal(isMissingProfileError(new Error('404: {"detail":"Profile \'x\' does not exist."}')), false)
})

test('isMissingProfileError handles non-Error values without throwing', () => {
  assert.equal(isMissingProfileError('Profile "w" no longer exists.'), true)
  assert.equal(isMissingProfileError(null), false)
  assert.equal(isMissingProfileError(undefined), false)
})

// ---------------------------------------------------------------------------
// The dead-profile set
// ---------------------------------------------------------------------------

test('a profile is not known missing until it is marked', () => {
  assert.equal(isProfileKnownMissing('worker'), false)
  markProfileMissing('worker')
  assert.equal(isProfileKnownMissing('worker'), true)
})

test('profile keys are compared case- and whitespace-insensitively', () => {
  markProfileMissing('  Worker  ')

  assert.equal(isProfileKnownMissing('worker'), true)
  assert.equal(isProfileKnownMissing('WORKER'), true)
})

test('marking an empty name records nothing', () => {
  markProfileMissing('   ')

  assert.equal(isProfileKnownMissing(''), false)
  assert.equal(isProfileKnownMissing('   '), false)
})

// ---------------------------------------------------------------------------
// noteProfileError — classify + remember in one step
// ---------------------------------------------------------------------------

test('noteProfileError records a permanently-missing profile and reports true', () => {
  assert.equal(noteProfileError('claudecode', new Error('Profile "claudecode" no longer exists.')), true)
  assert.equal(isProfileKnownMissing('claudecode'), true)
})

// The regression this whole module exists for: a 404 must NOT poison the set,
// or the next lookup skips a live profile and the session becomes unfindable.
test('noteProfileError leaves a profile probeable after a plain 404', () => {
  assert.equal(noteProfileError('worker', new Error('404: {"detail":"Session not found"}')), false)
  assert.equal(isProfileKnownMissing('worker'), false)
})

test('__resetMissingProfiles clears recorded profiles', () => {
  markProfileMissing('worker')
  __resetMissingProfiles()

  assert.equal(isProfileKnownMissing('worker'), false)
})
