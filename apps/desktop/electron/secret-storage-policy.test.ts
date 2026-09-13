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

/**
 * A fakeIo augmented with the durable last-known-on marker as its own
 * independent piece of state — mirrors main.ts's real wiring, where the
 * marker lives in a SEPARATE file from the policy file so deleting one
 * doesn't take out the other.
 */
function fakeIoWithMarker(
  initial: string | null = null,
  markerInitiallyOn = false
): SecretStoragePolicyIo & { fileText: () => string | null; logs: string[]; deletePolicyFile: () => void } {
  const base = fakeIo(initial)
  let markerOn = markerInitiallyOn

  return {
    ...base,
    readLastKnownOn: () => markerOn,
    writeLastKnownOn: (on: boolean) => {
      markerOn = on
    },
    // Simulates an external actor (backup tool, sync client, stray `rm`)
    // deleting JUST the policy file, leaving the independent marker intact.
    deletePolicyFile: () => {
      // Re-derive an ENOENT-throwing readText without touching the marker —
      // fakeIo's own `text` closure isn't reachable from here, so route
      // through the same preserveCorruptPolicy hook the real quarantine path
      // uses (it already sets text to null).
      base.preserveCorruptPolicy!()
    }
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

// ── durable last-known-on marker (review round 2) ──────────────────────────
//
// A deleted previously-ON policy file must NOT silently downgrade to the OFF
// default on restart — that is exactly the silent-downgrade this whole module
// exists to prevent, and it was previously missed for the "file plain
// vanished" case (only "file present but corrupt" was covered). The
// independent last-known-on marker (its own file, written every time
// writeSecretStoragePolicy runs) is what lets readSecretStoragePolicy tell
// "genuinely never configured" apart from "was on, and only the policy file
// itself disappeared".

test('a genuinely new install (marker never written) still defaults OFF when the policy file is absent', () => {
  const io = fakeIoWithMarker(null, false)

  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: false })
})

test('restart after the policy file is deleted, with the marker recording it was last ON, resolves sticky-on', () => {
  const io = fakeIoWithMarker(null, true)

  const policy = readSecretStoragePolicy(io)

  assert.deepEqual(policy, { on: true, migrated: true })
  assert.equal(io.logs.length, 1)
  assert.match(io.logs[0], /missing but was last known to be ON/)
})

test('the marker is written every time the policy is deliberately turned ON', () => {
  const io = fakeIoWithMarker(null, false)

  writeSecretStoragePolicy({ on: true, migrated: true }, io)

  // Simulate the policy file vanishing after the deliberate ON write — the
  // marker (a separate file) must have survived and still says ON.
  io.deletePolicyFile()

  assert.deepEqual(readSecretStoragePolicy(io), { on: true, migrated: true })
})

test('the marker is cleared when the policy is deliberately turned back OFF', () => {
  const io = fakeIoWithMarker(null, false)

  writeSecretStoragePolicy({ on: true, migrated: true }, io)
  writeSecretStoragePolicy({ on: false, migrated: true }, io)

  // Deliberately turned off, then the policy file vanishes — must NOT come
  // back as sticky-on, because the LAST deliberate write was off.
  io.deletePolicyFile()

  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: false })
})

test('a present-but-corrupt policy file still wins over the marker (corruption handling is unchanged)', () => {
  // Corruption-while-present already has its own conservative sticky-on
  // handling (tested above); the marker must not change or duplicate that —
  // it only matters on the ABSENT-file path.
  const io = fakeIoWithMarker('not-json', false)

  assert.deepEqual(readSecretStoragePolicy(io), { on: true, migrated: true })
  assert.equal(io.fileText(), null, 'still quarantined via the normal corrupt-file path')
})

test('an IO that does not implement the marker (older/simpler caller) keeps the pre-existing absent-defaults-off behavior', () => {
  const io = fakeIo(null)

  delete (io as any).readLastKnownOn
  delete (io as any).writeLastKnownOn

  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: false })

  // And writing through an IO without writeLastKnownOn must not throw.
  assert.doesNotThrow(() => writeSecretStoragePolicy({ on: true, migrated: true }, io))
})

test('a throwing readLastKnownOn fails toward the safe (OFF) default rather than propagating', () => {
  const io: SecretStoragePolicyIo = {
    ...fakeIo(null),
    readLastKnownOn: () => {
      throw new Error('marker file unreadable for an unrelated reason')
    }
  }

  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: false })
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
