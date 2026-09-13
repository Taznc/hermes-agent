/**
 * native-token-store.ts
 *
 * The encrypted-at-rest persistence seam for RFC 8252 native OAuth tokens:
 * NativeTokenSet → JSON → safeStorage blob → store file, and back again on the
 * next launch.
 *
 * Kept standalone (no `import 'electron'`) so the whole restart path unit-tests
 * with the `electron` vitest project — the same pattern as native-oauth.ts.
 * main.ts owns the electron-coupled halves and injects them: the safeStorage
 * encrypt/decrypt pair and the userData store-file read/write.
 *
 * The parser direction is the load-bearing detail. What lands on disk is the
 * *normalized* camelCase NativeTokenSet, so the reload boundary is
 * parseStoredTokenSet(). Gateway `/auth/native/token` responses are snake_case
 * and stay with parseTokenResponse(); crossing the two made the decrypted set
 * throw on every launch, which surfaced as "signed out after restart" (#73271).
 */

import { type NativeTokenSet, parseStoredTokenSet } from './native-oauth'

/** One encrypted blob as written per gateway base URL. */
export interface StoredTokenSecret {
  encoding?: string
  value?: string
}

/**
 * The narrow set of side effects main.ts owns. Everything here is injected so
 * the store/load round trip can be exercised without an Electron runtime, and
 * so production keeps using safeStorage unchanged.
 */
export interface NativeTokenStoreIo {
  /**
   * Encrypt one plaintext blob. main.ts passes the strict safeStorage helper,
   * which THROWS when the OS keychain is unavailable — that must stay loud.
   * A `null` return is treated as the same authoritative failure: the caller
   * throws rather than persisting an empty entry over good tokens.
   */
  encrypt: (plaintext: string) => StoredTokenSecret | null
  /** Decrypt a stored payload; returns '' when it cannot be read. */
  decrypt: (secret: any) => string
  /** Raw store-file text. Throws when the file is absent — treated as empty. */
  readStoreText: () => string
  /**
   * Persist the store-file text. main.ts writes this atomically (temp file +
   * rename, owner-only mode) via writeSecretFileAtomic, so a crash/power-loss/
   * full-disk failure mid-write can only ever leave the OLD file intact or the
   * NEW file complete — never a truncated hybrid that would silently look
   * like an empty store on the next read.
   */
  writeStoreText: (text: string) => void
  /**
   * Move an unparseable store file aside so a later write does not silently
   * clobber bytes that might still be worth a manual recovery attempt.
   * Called at most once per detected corruption (once quarantined, the next
   * readStoreText() throws ENOENT and reads as a normal absent store).
   * Optional so tests that never hit corruption need not implement it.
   */
  preserveCorruptStore?: () => void
  rememberLog?: (message: string) => void
}

/**
 * The result of reading the store file, with corruption called out as its own
 * dimension rather than collapsed into "empty". `corrupted: true` is the
 * signal `persistNativeTokenSet` uses to refuse a destructive rewrite — see
 * that function's doc comment.
 */
interface ReadStoreResult {
  store: Record<string, any>
  /**
   * True only when the file was PRESENT but failed to parse as a JSON object
   * (truncated write, hand-mangled bytes). False for a genuinely absent file
   * and for a syntactically-valid-but-wrong-shaped value (e.g. `[]`) — those
   * are "nothing to preserve" cases, not corruption.
   */
  corrupted: boolean
}

/**
 * baseUrl → encrypted payload. A missing, unreadable, or hand-mangled store
 * reads as empty rather than throwing: a failed *read* falls to the next rung.
 *
 * Arrays (and every other syntactically-valid-but-wrong-shaped JSON value)
 * are rejected alongside a missing file: assigning store[baseUrl] on an array
 * would set a non-index property, which JSON.stringify drops on the way back
 * out — the write would look like it succeeded and the tokens would be gone
 * on the next launch. This is a deliberate, recoverable "start fresh" case,
 * not corruption — the bytes were never a valid store to begin with, so
 * there's nothing to preserve.
 *
 * Invalid JSON is different: it is usually the signature of a truncated
 * write (kill -9 / power loss mid-save with a pre-atomic writer, or a
 * genuinely damaged disk). Silently treating that the same as "empty" would
 * let the very next persist for any OTHER gateway overwrite those bytes for
 * good, destroying whatever forensic value they had — so this path quarantines
 * the file first (via `preserveCorruptStore`), logs once, and tells the caller
 * `corrupted: true` so a WRITE can refuse to proceed (a READ still degrades to
 * an empty store — `loadNativeTokenSet` has nothing to write, so "signed out,
 * logged, evidence preserved" is the safe answer there).
 */
function readStore(io: NativeTokenStoreIo): ReadStoreResult {
  let text: string

  try {
    text = io.readStoreText()
  } catch {
    return { store: {}, corrupted: false }
  }

  try {
    const parsed = JSON.parse(text)

    return { store: parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}, corrupted: false }
  } catch {
    try {
      io.preserveCorruptStore?.()
    } catch {
      // Best-effort quarantine; even if it fails, still refuse to trust the bytes.
    }

    io.rememberLog?.(
      '[native-oauth] token store file is corrupt (unparseable JSON); quarantined the file and continuing as if empty'
    )

    return { store: {}, corrupted: true }
  }
}

/**
 * A gateway URL safe to write into a log line.
 *
 * normalizeRemoteBaseUrl() strips query, fragment, and trailing slashes but
 * NOT userinfo, so a configured gateway can legitimately carry
 * `user:password@` all the way down to this store. Interpolating that into a
 * failure log would spill the credentials into the desktop log file, so drop
 * the userinfo and keep only what makes the line useful — scheme, host, port,
 * path. A value URL cannot parse never falls back to the raw input: echoing it
 * is the exact leak this guards against.
 */
function redactGatewayUrl(baseUrl: string): string {
  try {
    const parsed = new URL(baseUrl)

    parsed.username = ''
    parsed.password = ''

    // `host` already carries a non-default port.
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`
  } catch {
    return '<invalid gateway URL>'
  }
}

/**
 * Write (or, with `tokens === null`, drop) one gateway's token set, merging
 * into whatever other gateways are already stored.
 *
 * A corrupt-but-present store file is a hard stop for this function, not a
 * "read as empty and carry on": the card's acceptance criteria is that a
 * kill-9/truncated write can never lose another gateway's tokens, and writing
 * a fresh canonical store over a quarantined-but-still-live corruption would
 * do exactly that for whatever entries the corrupt bytes still held that
 * `preserveCorruptStore` couldn't restore automatically. So a corrupted read
 * aborts the persist entirely — no store write, no cache mutation upstream —
 * and reports the failure the same way an unusable/null-returning keychain
 * already does: thrown synchronously, straight through to the caller (main.ts's
 * `_persistNativeTokens` does not swallow this — see its own comment). This is
 * DIFFERENT from an unwritable disk (`io.writeStoreText` throwing), which is
 * caught and merely logged below — that failure happens AFTER the in-memory
 * store object has already been safely updated with the caller's intent, so
 * there is nothing left to protect by throwing. A corrupted read is caught
 * before any mutation, and the whole point is to stop before mutating. The
 * bytes are already quarantined by `readStore` for a human/support recovery
 * flow; the CURRENT request (the gateway actually being persisted right now)
 * simply fails loudly instead of silently discarding its siblings.
 */
export function persistNativeTokenSet(baseUrl: string, tokens: NativeTokenSet | null, io: NativeTokenStoreIo): void {
  const { store, corrupted } = readStore(io)

  if (corrupted) {
    throw new Error(
      'Token store file is corrupt; quarantined for recovery and refusing to overwrite it with a partial store. Retry once the quarantined file has been reviewed.'
    )
  }

  if (tokens) {
    // Encrypt the whole set as one blob so the refresh token never lands in
    // plaintext on disk. Deliberately outside the try below: an unusable
    // keychain is an authoritative write failure and must surface to the
    // caller, not be logged away as if the tokens were saved.
    const secret = io.encrypt(JSON.stringify(tokens))

    if (!secret) {
      // A null blob is the same failure as a throw, only quieter. Storing it
      // would replace a good entry with nothing: the write would report
      // success, the next launch would show signed out, and the refresh token
      // would be unrecoverable. Fail before touching the store.
      throw new Error('Secure token storage returned no encrypted payload; refusing to overwrite stored native tokens.')
    }

    store[baseUrl] = secret
  } else {
    delete store[baseUrl]
  }

  try {
    io.writeStoreText(JSON.stringify(store))
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)

    io.rememberLog?.(`[native-oauth] failed to persist tokens: ${detail}`)
  }
}

/**
 * Reconstruct a gateway's token set from the stored encrypted payload. Returns
 * null when nothing is stored, when the blob cannot be decrypted, or when it
 * does not parse — never a partially-populated set.
 *
 * Unlike `persistNativeTokenSet`, a corrupted store here is NOT a hard stop:
 * there is nothing to write, so the only honest answer is "signed out" —
 * the corruption is already logged and the bytes quarantined by `readStore`.
 */
export function loadNativeTokenSet(baseUrl: string, io: NativeTokenStoreIo): NativeTokenSet | null {
  // The UNREDACTED url is the store key — redaction is for logs only.
  const secret = readStore(io).store[baseUrl]

  if (!secret) {
    return null
  }

  try {
    const plaintext = io.decrypt(secret)

    if (!plaintext) {
      // A keychain that is merely locked/unavailable right now must not cost
      // the user their refresh token — leave the entry for the next attempt.
      io.rememberLog?.(
        `[native-oauth] failed to decrypt stored tokens for ${redactGatewayUrl(baseUrl)}; keeping stored entry for retry`
      )

      return null
    }

    // Stored blobs are normalized camelCase sets, never raw gateway responses.
    return parseStoredTokenSet(JSON.parse(plaintext))
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)

    io.rememberLog?.(`[native-oauth] failed to load stored tokens for ${redactGatewayUrl(baseUrl)}: ${detail}`)

    return null
  }
}
