/**
 * Tests for electron/secret-storage-policy.ts — the "is OS-keychain
 * encryption enabled at all?" decision seam.
 *
 * The behavior this file pins: keychain-backed encryption is OPT-IN
 * (default OFF), and once the one-shot legacy migration has run, a
 * safeStorage blob under an opted-out policy reads as 'drop' — i.e. the
 * caller must treat it as absent WITHOUT touching safeStorage, so a broken
 * macOS login keychain can never raise its password dialog on launch.
 *
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  classifyStoredSecret,
  readSecretStoragePolicy,
  type SecretStoragePolicyIo,
  writeSecretStoragePolicy
} from './secret-storage-policy'

function fakeIo(
  initial: string | null = null
): SecretStoragePolicyIo & { fileText: () => string | null; logs: string[] } {
  let text = initial
  const logs: string[] = []

  return {
    readText: () => {
      if (text === null) {
        throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      }

      return text
    },
    writeText: (next: string) => {
      text = next
    },
    preserveCorruptPolicy: () => {
      text = null
    },
    rememberLog: message => logs.push(message),
    fileText: () => text,
    logs
  }
}

// ── defaults ────────────────────────────────────────────────────────────────

test('missing policy file defaults to encryption OFF, not migrated', () => {
  const policy = readSecretStoragePolicy(fakeIo())

  assert.deepEqual(policy, { on: false, migrated: false })
})

test('corrupt or non-object policy file — while PRESENT — is read as the conservative sticky-on default, not silently defaulted off', () => {
  // A file that exists only got there via this module's own atomic writer, so
  // corruption while present is a sign of external tampering (garbling), not
  // "never configured." Reading it as the OFF default would silently flip an
  // opted-in user back to plaintext storage — exactly what this guards against.
  for (const bad of ['not-json', '[]', '"on"', 'null', '123']) {
    const io = fakeIo(bad)

    assert.deepEqual(readSecretStoragePolicy(io), { on: true, migrated: true })
    // The corrupt bytes are quarantined so a later write doesn't erase them.
    assert.equal(io.fileText(), null)
  }
})

test('a corrupt policy file logs once and is quarantined via preserveCorruptPolicy', () => {
  const io = fakeIo('{not json')

  readSecretStoragePolicy(io)

  assert.equal(io.logs.length, 1)
  assert.match(io.logs[0], /corrupt or malformed/)
})

test('truthy-but-not-true values do NOT enable encryption', () => {
  // Strict === true coercion: a hand-edited "on": 1 or "yes" must not turn
  // keychain prompts back on.
  for (const bad of ['{"on":1}', '{"on":"yes"}', '{"on":"true"}']) {
    assert.equal(readSecretStoragePolicy(fakeIo(bad)).on, false)
  }
})

test('round trip preserves both fields', () => {
  const io = fakeIo()

  writeSecretStoragePolicy({ on: true, migrated: true }, io)
  assert.deepEqual(readSecretStoragePolicy(io), { on: true, migrated: true })

  writeSecretStoragePolicy({ on: false, migrated: true }, io)
  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: true })
})

// ── classification ──────────────────────────────────────────────────────────

const SAFE_BLOB = { encoding: 'safeStorage', value: 'AAAA' }
const PLAIN_BLOB = { encoding: 'plain', value: 'tok' }

test('non-safeStorage blobs are always keep, under every policy', () => {
  for (const policy of [
    { on: false, migrated: false },
    { on: false, migrated: true },
    { on: true, migrated: true }
  ]) {
    assert.equal(classifyStoredSecret(PLAIN_BLOB, policy), 'keep')
    assert.equal(classifyStoredSecret(null, policy), 'keep')
    assert.equal(classifyStoredSecret(undefined, policy), 'keep')
    assert.equal(classifyStoredSecret({} as any, policy), 'keep')
  }
})

test('safeStorage blob with encryption ON is keep', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: true, migrated: true }), 'keep')
})

test('safeStorage blob, encryption OFF, pre-migration is migrate', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: false, migrated: false }), 'migrate')
})

test('safeStorage blob, encryption OFF, post-migration is drop — never touch the keychain again', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: false, migrated: true }), 'drop')
})
