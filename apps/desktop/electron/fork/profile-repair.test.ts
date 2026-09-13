/**
 * Behavior contract for the fork profile-repair module: a stored desktop
 * profile preference is only honored when the name is well-formed AND the
 * profile still exists on this machine; a dead preference is logged and
 * cleared (self-heal) so the app falls back to the default profile instead
 * of routing every profile-scoped REST call at a 404 forever.
 */

import { expect, test } from 'vitest'

import { repairStoredProfile, type StoredProfileRepairDeps } from './profile-repair'

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

function deps(overrides: Partial<StoredProfileRepairDeps> = {}) {
  const state = {
    cleared: 0,
    logs: [] as string[]
  }

  const d: StoredProfileRepairDeps = {
    readStoredProfile: () => '',
    isValidProfileName: name => NAME_RE.test(name),
    profileDirectoryExists: () => false,
    clearStoredProfile: () => {
      state.cleared += 1
    },
    rememberLog: message => state.logs.push(message),
    ...overrides
  }

  return { d, state }
}

test('an existing well-formed profile resolves and nothing is cleared', () => {
  const { d, state } = deps({
    readStoredProfile: () => 'work',
    profileDirectoryExists: name => name === 'work'
  })

  expect(repairStoredProfile(d)).toBe('work')
  expect(state.cleared).toBe(0)
  expect(state.logs).toEqual([])
})

test('an absent/malformed preference resolves to null silently', () => {
  const { d, state } = deps({ readStoredProfile: () => '' })

  expect(repairStoredProfile(d)).toBeNull()
  expect(state.cleared).toBe(0)
  expect(state.logs).toEqual([])
})

test('a well-formed name whose profile is gone is logged and cleared (self-heal)', () => {
  const { d, state } = deps({
    readStoredProfile: () => 'claudeprimary',
    profileDirectoryExists: () => false
  })

  expect(repairStoredProfile(d)).toBeNull()
  expect(state.cleared).toBe(1)
  expect(state.logs.some(line => line.includes('"claudeprimary" no longer exists'))).toBe(true)
})

test('a failing clear (read-only userData) still falls back to null', () => {
  const { d } = deps({
    readStoredProfile: () => 'gone',
    clearStoredProfile: () => {
      throw new Error('EROFS')
    }
  })

  expect(repairStoredProfile(d)).toBeNull()
})

test('"default" pins the root HERMES_HOME without requiring a profiles/ directory', () => {
  const { d, state } = deps({
    readStoredProfile: () => 'default',
    profileDirectoryExists: () => false
  })

  expect(repairStoredProfile(d)).toBe('default')
  expect(state.cleared).toBe(0)
})
