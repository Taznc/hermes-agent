/**
 * Tests for electron/native-token-store.ts — the encrypted-at-rest persistence
 * seam main.ts uses for RFC 8252 native OAuth tokens.
 *
 * The regression this file exists for (#73271): tokens are persisted as a
 * normalized camelCase NativeTokenSet, but the reload path fed the freshly
 * decrypted object to parseTokenResponse(), which only understands the
 * gateway's snake_case response. It threw on every launch, so a signed-in user
 * came back signed out. The parser boundary now lives inside
 * loadNativeTokenSet(), so these tests fail if it is ever crossed again.
 *
 * "Fresh load" here means what it means after a restart: nothing survives but
 * the bytes of the store file, so every assertion below is served by
 * deserializing and decrypting that text — never by an in-memory object.
 *
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type NativeTokenSet, parseStoredTokenSet, parseTokenResponse } from './native-oauth'
import { loadNativeTokenSet, type NativeTokenStoreIo, persistNativeTokenSet } from './native-token-store'

const GATEWAY = 'https://gw.example.com'

const TOKENS: NativeTokenSet = {
  accessToken: 'AT-live-abc123',
  refreshToken: 'RT-live-xyz789',
  expiresAt: 1_893_456_000,
  provider: 'nous',
  userId: 'u-42'
}

interface FakeDisk {
  io: NativeTokenStoreIo
  logs: string[]
  /** The store-file text as it would sit on disk; null when the file is absent. */
  fileText: () => string | null
}

/**
 * A stand-in for the userData store file plus safeStorage. Encryption is
 * base64 rather than the OS keychain — opaque-blob-in, same-plaintext-out is
 * the only property this seam depends on, and it keeps the round trip
 * observable. `initialText` models a process restart: the new "process" starts
 * with nothing but the bytes the previous one wrote.
 */
function createFakeDisk(initialText: string | null = null, overrides: Partial<NativeTokenStoreIo> = {}): FakeDisk {
  let text = initialText
  const logs: string[] = []

  const io: NativeTokenStoreIo = {
    encrypt: plaintext => ({ encoding: 'safeStorage', value: Buffer.from(plaintext, 'utf8').toString('base64') }),
    decrypt: secret =>
      secret?.encoding === 'safeStorage' ? Buffer.from(String(secret.value), 'base64').toString('utf8') : '',
    readStoreText: () => {
      if (text === null) {
        // Matches fs.readFileSync on a missing file: throws, not empty string.
        throw Object.assign(new Error('ENOENT: no such file or directory'), { code: 'ENOENT' })
      }

      return text
    },
    writeStoreText: next => {
      text = next
    },
    rememberLog: message => logs.push(message),
    ...overrides
  }

  return { io, logs, fileText: () => text }
}

// --- the restart round trip ---

test('a camelCase token set survives store then a fresh load', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, first.io)

  const onDisk = first.fileText()

  assert.ok(onDisk, 'persisting must write the store file')

  // Nothing may survive the "restart" except those bytes.
  const restarted = createFakeDisk(onDisk)
  const loaded = loadNativeTokenSet(GATEWAY, restarted.io)

  assert.ok(loaded, 'a stored token set must reload after a restart')
  // Reconstructed from the payload, not handed back the object we stored.
  assert.notEqual(loaded, TOKENS)
  assert.deepEqual(loaded, TOKENS)
  assert.deepEqual(restarted.logs, [])
})

test('a fresh load restores both tokens and preserves expiry, provider and user', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, first.io)

  const loaded = loadNativeTokenSet(GATEWAY, createFakeDisk(first.fileText()).io)!

  assert.equal(loaded.accessToken, 'AT-live-abc123')
  assert.equal(loaded.refreshToken, 'RT-live-xyz789')
  // Still a number after the JSON round trip, not "1893456000".
  assert.equal(loaded.expiresAt, 1_893_456_000)
  assert.equal(typeof loaded.expiresAt, 'number')
  assert.equal(loaded.provider, 'nous')
  assert.equal(loaded.userId, 'u-42')
})

test('the loaded set is accepted by the stored-token parsing boundary', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, first.io)

  const loaded = loadNativeTokenSet(GATEWAY, createFakeDisk(first.fileText()).io)!

  // What comes back out of the store is itself a valid stored set — re-parsing
  // it is a no-op, so callers can hand it straight to the refresh path.
  assert.deepEqual(parseStoredTokenSet(loaded), loaded)
})

test('the persisted payload is what broke the old reload path (#73271)', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, first.io)

  const restarted = createFakeDisk(first.fileText())
  const secret = JSON.parse(restarted.fileText()!)[GATEWAY]
  const decrypted = JSON.parse(restarted.io.decrypt(secret))

  // The old code passed exactly this object to parseTokenResponse(). A
  // normalized set has no snake_case access_token, so every launch threw and
  // the user was shown as signed out...
  assert.throws(() => parseTokenResponse(decrypted), /missing access_token/i)
  // ...while the real load path reads the same bytes successfully.
  assert.deepEqual(loadNativeTokenSet(GATEWAY, restarted.io), TOKENS)
})

test('the full login-to-restart sequence keeps the two parser boundaries apart', () => {
  // Login: the gateway answers /auth/native/token in snake_case, and only
  // parseTokenResponse() understands that shape.
  const fromGateway = parseTokenResponse({
    access_token: 'AT-fresh',
    refresh_token: 'RT-fresh',
    expires_at: 1_893_456_789,
    provider: 'nous',
    user_id: 'u-77'
  })

  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, fromGateway, first.io)

  // Restart: what was stored is normalized, so the store's own boundary reads
  // it back unchanged.
  assert.deepEqual(loadNativeTokenSet(GATEWAY, createFakeDisk(first.fileText()).io), fromGateway)
})

// --- storage hygiene ---

test('tokens are encrypted at rest, never plaintext in the store file', () => {
  const disk = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, disk.io)

  const onDisk = disk.fileText()!

  assert.doesNotMatch(onDisk, /AT-live-abc123/)
  assert.doesNotMatch(onDisk, /RT-live-xyz789/)
  assert.equal(JSON.parse(onDisk)[GATEWAY].encoding, 'safeStorage')
})

test('persisting one gateway leaves other gateways intact', () => {
  const other = 'https://other.example.com'
  const disk = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, disk.io)
  persistNativeTokenSet(other, { ...TOKENS, accessToken: 'AT-other', userId: 'u-99' }, disk.io)

  const restarted = createFakeDisk(disk.fileText())

  assert.equal(loadNativeTokenSet(GATEWAY, restarted.io)!.accessToken, 'AT-live-abc123')
  assert.equal(loadNativeTokenSet(other, restarted.io)!.accessToken, 'AT-other')
})

test('clearing removes only that gateway and reloads as signed out', () => {
  const other = 'https://other.example.com'
  const disk = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, disk.io)
  persistNativeTokenSet(other, TOKENS, disk.io)
  persistNativeTokenSet(GATEWAY, null, disk.io)

  const restarted = createFakeDisk(disk.fileText())

  assert.equal(loadNativeTokenSet(GATEWAY, restarted.io), null)
  assert.ok(loadNativeTokenSet(other, restarted.io))
})

test('an absent store file loads as signed out without logging a failure', () => {
  const disk = createFakeDisk()

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  assert.deepEqual(disk.logs, [])
})

// --- failure paths (unchanged by the extraction) ---

test('a locked keychain keeps the stored entry for a later retry', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, first.io)

  // safeStorage unavailable at load time ⇒ decryptDesktopSecret returns ''.
  const locked = createFakeDisk(first.fileText(), { decrypt: () => '' })

  assert.equal(loadNativeTokenSet(GATEWAY, locked.io), null)
  assert.match(locked.logs[0], /failed to decrypt stored tokens for https:\/\/gw\.example\.com/)
  assert.match(locked.logs[0], /keeping stored entry for retry/)
  // The refresh token must NOT be dropped just because the keychain was locked.
  assert.deepEqual(locked.fileText(), first.fileText())
})

test('a corrupt store file loads as signed out and logs the corruption instead of throwing', () => {
  const disk = createFakeDisk('{not json')

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  // Unlike a merely-absent store (logs stay empty), a PRESENT-but-corrupt
  // store is worth a log line: it's likely a truncated write, not "never
  // used" — see readStore()'s doc comment in native-token-store.ts.
  assert.equal(disk.logs.length, 1)
  assert.match(disk.logs[0], /corrupt/)
})

test('an array store file loads as signed out instead of throwing', () => {
  const disk = createFakeDisk('[]')

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  assert.deepEqual(disk.logs, [])
})

test('an array store file is replaced by a real map rather than swallowing the write', () => {
  const disk = createFakeDisk('[]')

  persistNativeTokenSet(GATEWAY, TOKENS, disk.io)

  const written = JSON.parse(disk.fileText()!)

  // Assigning store[baseUrl] on an array sets a non-index property, which
  // JSON.stringify drops — the write would report success and the tokens would
  // be gone on the next launch.
  assert.equal(Array.isArray(written), false)
  assert.ok(written[GATEWAY], 'the gateway entry must survive serialization')
  // And it really does come back after a restart.
  assert.deepEqual(loadNativeTokenSet(GATEWAY, createFakeDisk(disk.fileText()).io), TOKENS)
})

test('a corrupt decrypted blob is reported and loads as signed out', () => {
  const disk = createFakeDisk(JSON.stringify({ [GATEWAY]: { encoding: 'safeStorage', value: 'bm90LWpzb24=' } }))

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  assert.match(disk.logs[0], /failed to load stored tokens for https:\/\/gw\.example\.com/)
})

test('a decrypted blob missing accessToken is rejected, not half-restored', () => {
  const plaintext = JSON.stringify({ refreshToken: 'RT-only', provider: 'nous' })

  const disk = createFakeDisk(
    JSON.stringify({ [GATEWAY]: { encoding: 'safeStorage', value: Buffer.from(plaintext).toString('base64') } })
  )

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  assert.match(disk.logs[0], /missing accessToken/i)
})

test('a non-Error decryption failure keeps its detail in the log', () => {
  const disk = createFakeDisk(JSON.stringify({ [GATEWAY]: { encoding: 'safeStorage', value: 'AAAA' } }), {
    decrypt: () => {
      throw 'keychain exploded'
    }
  })

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
  assert.match(disk.logs[0], /keychain exploded/)
})

test('an unwritable store file is logged rather than thrown', () => {
  const disk = createFakeDisk(null, {
    writeStoreText: () => {
      throw new Error('EACCES: permission denied')
    }
  })

  assert.doesNotThrow(() => persistNativeTokenSet(GATEWAY, TOKENS, disk.io))
  assert.match(disk.logs[0], /failed to persist tokens: EACCES/)
})

test('a non-Error write failure keeps its detail in the log', () => {
  const disk = createFakeDisk(null, {
    writeStoreText: () => {
      throw 'disk went away'
    }
  })

  // `(error as Error).message` on a thrown string reads as undefined and loses
  // the only diagnostic there was.
  assert.doesNotThrow(() => persistNativeTokenSet(GATEWAY, TOKENS, disk.io))
  assert.equal(disk.logs[0], '[native-oauth] failed to persist tokens: disk went away')
})

// --- crash-safety: corrupt-store quarantine must not destroy other gateways ---
//
// The bug this guards against: a truncated write (kill -9 mid-persist with a
// non-atomic writer) leaves the JSON unparseable. readStore() used to treat
// that identically to "file never existed" — empty object, no signal — so the
// very next persist() for ANY gateway would silently drop every other
// gateway's stored refresh token by writing a fresh store containing only the
// current entry. Quarantining the corrupt bytes instead of reading them as
// empty-and-then-overwriting is what makes that no longer possible: the
// corrupt file is moved aside before it can be clobbered, and a caller that
// still wants to write is writing a genuinely fresh store, not overwriting
// evidence.

test('preserveCorruptStore is invoked exactly once when the store fails to parse', () => {
  let quarantines = 0

  const disk = createFakeDisk('{not valid json at all', {
    preserveCorruptStore: () => {
      quarantines += 1
    }
  })

  loadNativeTokenSet(GATEWAY, disk.io)

  assert.equal(quarantines, 1)
})

test('a quarantine failure does not prevent the corrupt store from still being read as empty', () => {
  const disk = createFakeDisk('{not valid json at all', {
    preserveCorruptStore: () => {
      throw new Error('rename failed: EACCES')
    }
  })

  // The quarantine attempt itself failing must not throw out of loadNativeTokenSet —
  // "refuse to trust corrupt bytes" must hold even when quarantining is impossible.
  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
})

test('an io without preserveCorruptStore (older/simpler callers) still refuses to trust corrupt bytes', () => {
  // preserveCorruptStore is optional on the interface; omitting it must not
  // change the safety property — corruption still reads as empty, not as a
  // thrown error that could crash the caller.
  const disk = createFakeDisk('{not valid json at all')

  delete (disk.io as any).preserveCorruptStore

  assert.equal(loadNativeTokenSet(GATEWAY, disk.io), null)
})

test('array store and truly-corrupt-JSON store are told apart: only the latter is quarantined', () => {
  // An array is a deliberate "start fresh" case (see readStore's doc comment)
  // — it's valid JSON, just the wrong shape, so there's nothing to preserve.
  let arrayQuarantines = 0
  const arrayDisk = createFakeDisk('[]', { preserveCorruptStore: () => (arrayQuarantines += 1) })

  loadNativeTokenSet(GATEWAY, arrayDisk.io)
  assert.equal(arrayQuarantines, 0, 'a syntactically valid array must not be treated as corruption')

  let jsonQuarantines = 0
  const corruptDisk = createFakeDisk('{not json', { preserveCorruptStore: () => (jsonQuarantines += 1) })

  loadNativeTokenSet(GATEWAY, corruptDisk.io)
  assert.equal(jsonQuarantines, 1, 'unparseable JSON must be quarantined')
})

test('persisting against a corrupt store aborts BEFORE writing anything — refuses the destructive rewrite', () => {
  // The exact contract gap from review round 1: quarantining the corrupt
  // bytes is necessary but not sufficient. If persistNativeTokenSet went on
  // to write a fresh canonical store after quarantining, the write for
  // gateway B would still be the only entry in the live store — identical
  // end state to the original bug, just with better evidence preservation.
  // The write MUST NOT happen at all; the caller (main.ts) is expected to
  // surface the throw and let the user retry.
  let writes = 0
  let quarantines = 0

  const disk = createFakeDisk('{not valid json at all', {
    preserveCorruptStore: () => {
      quarantines += 1
    },
    writeStoreText: () => {
      writes += 1
    }
  })

  assert.throws(
    () => persistNativeTokenSet(GATEWAY, TOKENS, disk.io),
    /corrupt/i,
    'persisting against a corrupt store must throw, not silently succeed'
  )

  assert.equal(writes, 0, 'writeStoreText must never be called when the existing store is corrupt')
  assert.equal(quarantines, 1, 'the corrupt bytes are still quarantined as part of the abort')
})

test('an array store (not corrupt, just wrong-shaped) still allows a normal persist', () => {
  // Contrast case for the test above: an array is NOT corruption (see
  // readStore's doc comment), so persisting over one must keep working exactly
  // as before — only genuinely unparseable JSON triggers the write-abort.
  const disk = createFakeDisk('[]')

  assert.doesNotThrow(() => persistNativeTokenSet(GATEWAY, TOKENS, disk.io))
  assert.deepEqual(loadNativeTokenSet(GATEWAY, createFakeDisk(disk.fileText()).io), TOKENS)
})

test('clearing (tokens=null) against a corrupt store also aborts rather than writing an empty store', () => {
  // A logout/clear is a write too — it must not get a free pass around the
  // corruption guard just because its intent is deletion rather than
  // insertion. Writing `{}` over the quarantined bytes would be exactly as
  // destructive to any OTHER gateway's entry the corrupt bytes might still
  // have held.
  let writes = 0

  const disk = createFakeDisk('{not valid json at all', {
    writeStoreText: () => {
      writes += 1
    }
  })

  assert.throws(() => persistNativeTokenSet(GATEWAY, null, disk.io), /corrupt/i)
  assert.equal(writes, 0)
})

test('an unusable keychain fails the write loudly and writes nothing', () => {
  const existing = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, existing.io)

  const before = existing.fileText()

  const broken = createFakeDisk(before, {
    encrypt: () => {
      throw new Error('Secure token storage is unavailable')
    }
  })

  // Storing must not pretend to succeed when the token cannot be encrypted...
  assert.throws(() => persistNativeTokenSet(GATEWAY, { ...TOKENS, accessToken: 'AT-new' }, broken.io), /unavailable/)
  // ...and must not clobber the tokens already on disk.
  assert.equal(broken.fileText(), before)
})

test('an encrypt that returns null is refused rather than blanking the stored entry', () => {
  const existing = createFakeDisk()

  persistNativeTokenSet(GATEWAY, TOKENS, existing.io)

  const before = existing.fileText()
  const nulled = createFakeDisk(before, { encrypt: () => null })
  let writes = 0

  // Spy that still delegates, so a stray write would show up in BOTH the
  // counter and the file text.
  const io = {
    ...nulled.io,
    writeStoreText: (text: string) => {
      writes += 1
      nulled.io.writeStoreText(text)
    }
  }

  // A quiet null is the same failure as a throw and must be just as loud.
  assert.throws(
    () => persistNativeTokenSet(GATEWAY, { ...TOKENS, accessToken: 'AT-new' }, io),
    /refusing to overwrite stored native tokens/
  )
  assert.equal(writes, 0, 'the store file must not be written at all')
  // Byte-for-byte unchanged...
  assert.equal(nulled.fileText(), before)
  // ...and the original token set still loads, refresh token intact.
  assert.deepEqual(loadNativeTokenSet(GATEWAY, createFakeDisk(before).io), TOKENS)
})

// --- credential redaction in logs ---
//
// normalizeRemoteBaseUrl() strips query/fragment/trailing slashes but not
// userinfo, so a configured gateway URL can carry `user:password@` into this
// store. It must stay intact as the store KEY and never reach a log line.

const CRED_GATEWAY = 'https://alice:supersecret@gw.example.com/hermes'

test('a decryption failure logs the gateway host and path but not its credentials', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(CRED_GATEWAY, TOKENS, first.io)

  const before = first.fileText()
  const locked = createFakeDisk(before, { decrypt: () => '' })

  assert.equal(loadNativeTokenSet(CRED_GATEWAY, locked.io), null)
  // Still identifies which gateway failed...
  assert.match(locked.logs[0], /failed to decrypt stored tokens for https:\/\/gw\.example\.com\/hermes/)
  assert.match(locked.logs[0], /keeping stored entry for retry/)
  // ...without the userinfo.
  assert.doesNotMatch(locked.logs[0], /alice/)
  assert.doesNotMatch(locked.logs[0], /supersecret/)
  // Redaction is log-only: the entry stays under the credential-bearing key.
  assert.ok(JSON.parse(locked.fileText()!)[CRED_GATEWAY])
  assert.equal(locked.fileText(), before)
})

test('a parsing failure logs the gateway host and path but not its credentials', () => {
  const disk = createFakeDisk(JSON.stringify({ [CRED_GATEWAY]: { encoding: 'safeStorage', value: 'bm90LWpzb24=' } }))

  assert.equal(loadNativeTokenSet(CRED_GATEWAY, disk.io), null)
  assert.match(disk.logs[0], /failed to load stored tokens for https:\/\/gw\.example\.com\/hermes/)
  assert.doesNotMatch(disk.logs[0], /alice/)
  assert.doesNotMatch(disk.logs[0], /supersecret/)
})

test('the credential-bearing base URL stays the exact store key', () => {
  const first = createFakeDisk()

  persistNativeTokenSet(CRED_GATEWAY, TOKENS, first.io)

  assert.deepEqual(Object.keys(JSON.parse(first.fileText()!)), [CRED_GATEWAY])
  // The original key still round-trips a full set after a restart.
  assert.deepEqual(loadNativeTokenSet(CRED_GATEWAY, createFakeDisk(first.fileText()).io), TOKENS)
  // The redacted form is a log string, never a lookup key.
  assert.equal(loadNativeTokenSet('https://gw.example.com/hermes', createFakeDisk(first.fileText()).io), null)
})

test('an unparseable gateway URL logs a fixed placeholder rather than the raw value', () => {
  // A space makes this unparseable by URL, so redaction cannot fall back to
  // echoing the input — that would leak the very credentials it guards.
  const invalid = 'ht tp://alice:supersecret@gw.example.com'

  const disk = createFakeDisk(JSON.stringify({ [invalid]: { encoding: 'safeStorage', value: 'AAAA' } }), {
    decrypt: () => ''
  })

  assert.equal(loadNativeTokenSet(invalid, disk.io), null)
  assert.match(disk.logs[0], /<invalid gateway URL>/)
  assert.doesNotMatch(disk.logs[0], /alice/)
  assert.doesNotMatch(disk.logs[0], /supersecret/)
})
