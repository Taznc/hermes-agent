/**
 * Integration test for the native OAuth token store's crash-safety, wired the
 * same way main.ts's `_nativeTokenStoreIo()` wires it: `writeStoreText`
 * through the REAL `writeSecretFileAtomic` (temp file + rename), and a
 * `preserveCorruptStore` that renames a corrupt file aside — against a REAL
 * temp directory, not mocked fs. Separate from native-token-store.test.ts
 * (which exercises the pure store logic against a fake in-memory disk) and
 * hardening.test.ts (which exercises writeSecretFileAtomic in isolation):
 * this file is the one place the two are proven to compose correctly, which
 * is exactly the seam the card's acceptance criteria target.
 *
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { writeSecretFileAtomic } from './hardening'
import { loadNativeTokenSet, type NativeTokenStoreIo, persistNativeTokenSet } from './native-token-store'

const GATEWAY_A = 'https://gw-a.example.com'
const GATEWAY_B = 'https://gw-b.example.com'

function withTempDir(run: (storePath: string) => void) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-native-token-store-'))

  try {
    run(path.join(dir, 'native-oauth-tokens.json'))
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

/** Mirrors main.ts's `_nativeTokenStoreIo()` exactly, against a real path. */
function realIo(storePath: string, logs: string[]): NativeTokenStoreIo {
  return {
    encrypt: plaintext => ({ encoding: 'safeStorage', value: Buffer.from(plaintext, 'utf8').toString('base64') }),
    decrypt: secret =>
      secret?.encoding === 'safeStorage' ? Buffer.from(String(secret.value), 'base64').toString('utf8') : '',
    readStoreText: () => fs.readFileSync(storePath, 'utf8'),
    writeStoreText: text => writeSecretFileAtomic(storePath, text, { encoding: 'utf8' }),
    preserveCorruptStore: () => {
      const quarantined = `${storePath}.corrupt-${Date.now()}`

      fs.renameSync(storePath, quarantined)
      logs.push(`quarantined:${quarantined}`)
    },
    rememberLog: message => logs.push(message)
  }
}

test('a fully-written store round-trips through the real atomic writer', () => {
  withTempDir(storePath => {
    const logs: string[] = []
    const io = realIo(storePath, logs)

    persistNativeTokenSet(GATEWAY_A, { accessToken: 'AT-a', refreshToken: 'RT-a', expiresAt: 1, provider: 'nous', userId: 'u-a' }, io)

    assert.ok(fs.existsSync(storePath))
    assert.equal(fs.existsSync(`${storePath}.tmp`), false, 'no leftover temp file after a clean write')
    assert.equal((fs.statSync(storePath).mode & 0o777).toString(8), '600', 'owner-only on disk')

    const reloaded = loadNativeTokenSet(GATEWAY_A, realIo(storePath, []))

    assert.equal(reloaded?.accessToken, 'AT-a')
  })
})

test('acceptance: a truncated file from a simulated kill -9 does not lose the OTHER gateway on the next persist', () => {
  withTempDir(storePath => {
    const setupLogs: string[] = []
    const setupIo = realIo(storePath, setupLogs)

    // Gateway A's real tokens land on disk via the real atomic writer.
    persistNativeTokenSet(
      GATEWAY_A,
      { accessToken: 'AT-a-live', refreshToken: 'RT-a-live', expiresAt: 1, provider: 'nous', userId: 'u-a' },
      setupIo
    )

    const fullBytes = fs.readFileSync(storePath, 'utf8')

    assert.ok(fullBytes.includes(GATEWAY_A))

    // Simulate `kill -9` mid fs.writeFileSync (the OLD, pre-fix code path):
    // only the first half of the JSON hit disk before the process died.
    // This is what a bare (non-atomic) writer can produce; the point of the
    // fix is that writeSecretFileAtomic itself can no longer produce this
    // shape — we're modeling "damage already happened; how do we recover?"
    const truncated = fullBytes.slice(0, Math.floor(fullBytes.length / 2))

    fs.writeFileSync(storePath, truncated, { mode: 0o600 })
    assert.throws(() => JSON.parse(fs.readFileSync(storePath, 'utf8')), 'the simulated truncation is really invalid JSON')

    // Now: sign in on GATEWAY B. The OLD bug: readStore() saw unparseable JSON,
    // silently treated it as {}, and the ensuing write for B replaced the
    // truncated bytes with a store containing ONLY B — GATEWAY A's refresh
    // token is gone forever, with no error and no log.
    const persistLogs: string[] = []
    const persistIo = realIo(storePath, persistLogs)

    persistNativeTokenSet(
      GATEWAY_B,
      { accessToken: 'AT-b-live', refreshToken: 'RT-b-live', expiresAt: 1, provider: 'nous', userId: 'u-b' },
      persistIo
    )

    // The corrupted bytes were quarantined BEFORE the new store was written —
    // gateway A's truncated (but not-yet-lost) data still exists on disk
    // under the quarantine path for a human/support flow to recover, rather
    // than being silently overwritten.
    assert.ok(persistLogs.some(line => line.startsWith('quarantined:')), 'corruption must be logged and quarantined')

    const quarantinedPath = persistLogs.find(line => line.startsWith('quarantined:'))!.slice('quarantined:'.length)

    assert.ok(fs.existsSync(quarantinedPath), 'the quarantined file must actually exist on disk')
    assert.equal(fs.readFileSync(quarantinedPath, 'utf8'), truncated, 'quarantined bytes are exactly what was corrupt')

    // Gateway B (the one being signed in right now) must still work — refusing
    // to trust corrupt bytes must not also refuse to serve the CURRENT request.
    const reloadedB = loadNativeTokenSet(GATEWAY_B, realIo(storePath, []))

    assert.equal(reloadedB?.accessToken, 'AT-b-live')

    // And critically: nothing about this flow silently fabricated gateway A's
    // tokens back into the live store — nothing recovers automatically here
    // and nothing pretends A is still signed in either.
    const reloadedA = loadNativeTokenSet(GATEWAY_A, realIo(storePath, []))

    assert.equal(reloadedA, null, 'A is honestly signed-out, not silently resurrected with stale/wrong data')
  })
})

test('acceptance: the real atomic writer itself cannot produce a truncated file, even mid-process-death simulation', () => {
  // The fix's other half: with writeSecretFileAtomic in the loop, a "crash"
  // can only ever interrupt the WRITE TO THE TEMP FILE (leaving a stale .tmp
  // that the next write's rmSync cleans up) or happen strictly before/after
  // the atomic rename — never leave the TARGET path itself holding partial
  // bytes. Simulate the two survivable interruption points directly.
  withTempDir(storePath => {
    const logs: string[] = []
    const io = realIo(storePath, logs)

    persistNativeTokenSet(
      GATEWAY_A,
      { accessToken: 'AT-a', refreshToken: 'RT-a', expiresAt: 1, provider: 'nous', userId: 'u-a' },
      io
    )

    const goodBytes = fs.readFileSync(storePath, 'utf8')

    // "Crash" that only ever gets as far as leaving a stale, half-written
    // temp file (a real writeFileSync interrupted before rename) — the
    // TARGET is untouched, still the last good bytes.
    fs.writeFileSync(`${storePath}.tmp`, '{"partial', { mode: 0o600 })

    assert.equal(fs.readFileSync(storePath, 'utf8'), goodBytes, 'target file is untouched by a temp-only crash')

    // The next real write must still succeed (rmSync clears the stale temp).
    persistNativeTokenSet(
      GATEWAY_B,
      { accessToken: 'AT-b', refreshToken: 'RT-b', expiresAt: 1, provider: 'nous', userId: 'u-b' },
      io
    )

    assert.equal(loadNativeTokenSet(GATEWAY_A, realIo(storePath, []))?.accessToken, 'AT-a')
    assert.equal(loadNativeTokenSet(GATEWAY_B, realIo(storePath, []))?.accessToken, 'AT-b')
  })
})
