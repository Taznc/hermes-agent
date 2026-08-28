/**
 * secret-storage-policy.ts
 *
 * Single owner of the "do we use the OS keychain at all?" decision for
 * desktop-stored secrets (remote gateway tokens, CF Access headers, native
 * OAuth token sets).
 *
 * Why this exists: Electron safeStorage on macOS parks a per-app key
 * ("Hermes Key") in the login keychain. On machines with a locked, missing,
 * or corrupted default keychain, ANY safeStorage touch — including
 * isEncryptionAvailable() — makes macOS throw a blocking "Keychain Not
 * Found" / password dialog on every launch. That is an unacceptable default
 * for a chat app, so keychain-backed encryption is OPT-IN:
 *
 *   - Setting OFF (default): secrets are written with encoding 'plain' and
 *     NO safeStorage API is ever called. decryptDesktopSecret already
 *     returns non-safeStorage encodings verbatim, so reads need no change.
 *   - Setting ON: the previous behavior — strict safeStorage encryption,
 *     loud failure when the keychain is unavailable, per-save plain-text
 *     confirm dialog as the escape hatch.
 *
 * Legacy blobs written before the flag existed are safeStorage-encoded on
 * disk. With the setting OFF we attempt ONE migration pass (decrypt →
 * rewrite as plain). The pass is recorded in the same settings file whether
 * or not it succeeds, so a broken keychain costs at most one prompt on the
 * first post-update launch — never one per launch.
 *
 * Kept standalone (no `import 'electron'`) so it unit-tests under the
 * electron vitest project, same pattern as native-token-store.ts. main.ts
 * injects the file path and fs.
 */

export interface SecretStoragePolicy {
  /** Keychain-backed encryption enabled (explicit user opt-in). */
  on: boolean
  /** One-shot legacy-blob migration already attempted. */
  migrated: boolean
}

export const SECRET_STORAGE_POLICY_FILE = 'secure-token-storage.json'

/**
 * A durable marker kept in its OWN file, separate from `SECRET_STORAGE_POLICY_FILE`.
 * Its only job is answering "was encryption ever explicitly turned on?" even
 * after the main policy file itself has vanished — see the "disappearance of
 * a previously-on policy" branch in `readSecretStoragePolicy` for why the two
 * files cannot be merged into one: a single file that disappears takes its own
 * history with it.
 */
export const SECRET_STORAGE_LAST_ON_MARKER_FILE = 'secure-token-storage.last-on'

export interface SecretStoragePolicyIo {
  readText: () => string
  writeText: (text: string) => void
  /**
   * Move a present-but-unparseable policy file aside instead of letting the
   * next write silently replace it. Optional so tests that never hit
   * corruption need not implement it.
   */
  preserveCorruptPolicy?: () => void
  /**
   * Durable "was encryption last explicitly turned ON?" marker, read from a
   * file independent of the main policy file (see
   * `SECRET_STORAGE_LAST_ON_MARKER_FILE`). Returns false when the marker is
   * absent — which covers both "never touched" and "explicitly turned off",
   * since `writeLastKnownOn(false)` removes it. Optional so tests that only
   * exercise the ordinary present/corrupt-file paths need not implement it;
   * when omitted, a genuinely absent policy file always reads as the OFF
   * default (the pre-existing behavior), which is the conservative choice for
   * an IO stub that hasn't opted into the extra durability.
   */
  readLastKnownOn?: () => boolean
  /**
   * Persist the "was encryption last explicitly turned ON?" marker. Called
   * every time `writeSecretStoragePolicy` runs, alongside the main policy
   * write, so the marker always mirrors the last *deliberately written*
   * policy — never the conservative-assumption value synthesized when the
   * main file is merely found corrupt or missing (those paths return a
   * `SecretStoragePolicy` but do not themselves call `writeSecretStoragePolicy`).
   */
  writeLastKnownOn?: (on: boolean) => void
  rememberLog?: (message: string) => void
}

/**
 * Normalize whatever is on disk into a policy.
 *
 *   - File genuinely ABSENT (readText throws, e.g. ENOENT): this is NOT
 *     automatically "never opted in" anymore — it also covers a previously-ON
 *     policy file that some external actor (backup tool, sync client, a stray
 *     `rm`) deleted out from under a machine that HAD explicitly turned
 *     encryption on. Silently reverting that to the OFF default would be the
 *     exact silent-downgrade this whole module exists to prevent — a user who
 *     opted into keychain encryption would find themselves back on plaintext
 *     with no warning. So an absent policy file consults the independent
 *     `readLastKnownOn` marker (see `SECRET_STORAGE_LAST_ON_MARKER_FILE`):
 *       - marker says it was last ON: return the conservative `{ on: true,
 *         migrated: true }`, exactly like the present-but-corrupt case below.
 *       - marker says OFF (or the IO doesn't implement it): the ordinary
 *         "never opted in" default, `{ on: false, migrated: false }`. This is
 *         still the product default for the overwhelming majority of installs
 *         — the marker file is only ever written when the user's own Settings
 *         toggle runs, so a genuinely new install with neither file present
 *         reads as OFF, never ON. This is the case "Do NOT default the policy
 *         ON" protects — a machine that never wrote either file must never
 *         see a keychain dialog.
 *   - File PRESENT but unparseable/hand-mangled/wrong-shaped: this is NOT the
 *     same case as absent. A file that exists only got there by this module's
 *     own atomic writer, so a corrupt-but-present file is a sign of external
 *     tampering (a backup tool, a sync client, a stray `> file` truncation)
 *     rather than "never configured." Silently reading that as the OFF
 *     default would flip an opted-in user back to plaintext storage without
 *     their knowledge — exactly the silent-downgrade this guards against.
 *     So a present-but-corrupt file is read as `{ on: true, migrated: true }`
 *     (assume it WAS on until the user's own Settings toggle confirms
 *     otherwise) and quarantined so the next write doesn't erase the
 *     evidence.
 *   - `on`/`migrated` use strict `=== true` on a well-formed object — a
 *     truthy-but-not-true value must not silently enable keychain prompts
 *     (mirrors the allowPlainText coercion rule in hardening.ts).
 */
export function readSecretStoragePolicy(io: SecretStoragePolicyIo): SecretStoragePolicy {
  let text: string

  try {
    text = io.readText()
  } catch {
    // Genuinely absent. Distinguish "never configured" from "a previously-ON
    // policy file vanished" via the independent last-known-on marker.
    let wasOn = false

    try {
      wasOn = io.readLastKnownOn?.() === true
    } catch {
      // Marker unreadable: fail toward the safe default rather than assuming ON
      // from a read failure on the marker itself — the marker file failing to
      // read is not evidence the policy was ever on.
      wasOn = false
    }

    if (wasOn) {
      io.rememberLog?.(
        '[secret-storage] policy file is missing but was last known to be ON; assuming encryption is still ON until confirmed'
      )

      return { on: true, migrated: true }
    }

    return { on: false, migrated: false }
  }

  try {
    const parsed = JSON.parse(text)

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { on: parsed.on === true, migrated: parsed.migrated === true }
    }
  } catch {
    // fall through to the present-but-corrupt handling below
  }

  try {
    io.preserveCorruptPolicy?.()
  } catch {
    // Best-effort quarantine; still fail toward the conservative assumption.
  }

  io.rememberLog?.(
    '[secret-storage] policy file exists but is corrupt or malformed; assuming encryption was ON until confirmed, and quarantined the file'
  )

  return { on: true, migrated: true }
}

export function writeSecretStoragePolicy(policy: SecretStoragePolicy, io: SecretStoragePolicyIo): void {
  const normalized = { on: policy.on === true, migrated: policy.migrated === true }

  io.writeText(JSON.stringify(normalized))

  // Keep the durable last-known-on marker in lockstep with every deliberate
  // write, so a later disappearance of the main policy file alone can still
  // be told apart from a genuinely fresh install (see readSecretStoragePolicy).
  try {
    io.writeLastKnownOn?.(normalized.on)
  } catch {
    // Best-effort: losing the marker degrades the disappearance-recovery case
    // back to the ordinary absent-file default, not a crash of the actual write.
  }
}

/** One stored secret blob as it appears on disk. */
interface StoredSecret {
  encoding?: string
  value?: string
}

/**
 * Decide what to do with one stored blob under the current policy.
 *
 *   - 'keep'    — blob is fine as-is under this policy.
 *   - 'migrate' — safeStorage blob while encryption is OFF and migration has
 *                 not run: caller should decrypt once and rewrite as plain.
 *   - 'drop'    — safeStorage blob while encryption is OFF and the migration
 *                 pass already ran (i.e. it could not be decrypted last
 *                 time): treat as absent WITHOUT touching safeStorage, so a
 *                 dead keychain never prompts again.
 */
export function classifyStoredSecret(
  secret: StoredSecret | null | undefined,
  policy: SecretStoragePolicy
): 'keep' | 'migrate' | 'drop' {
  if (!secret || typeof secret !== 'object' || secret.encoding !== 'safeStorage') {
    return 'keep'
  }

  if (policy.on) {
    return 'keep'
  }

  return policy.migrated ? 'drop' : 'migrate'
}

/**
 * The renderer-facing truth about whether a secret saved RIGHT NOW would
 * actually be OS-keychain encrypted.
 *
 *   - `policyOn` mirrors the stored policy flag verbatim, so the renderer can
 *     tell "off by choice" (policyOn false) apart from "on but broken"
 *     (policyOn true, available false).
 *   - `available` is the honest answer to "would a save be encrypted?".
 *     With the policy off, encryptDesktopSecret() never touches safeStorage
 *     at all and unconditionally writes plain text — so `available` is false
 *     here WITHOUT probing, both because probing would be a lie-detector on a
 *     question the policy already answered, and because probing itself is a
 *     keychain touch (isEncryptionAvailable() creates/queries the macOS Safe
 *     Storage Keychain item) that this opt-out policy exists to avoid.
 */
export interface SecureTokenStorageState {
  available: boolean
  policyOn: boolean
}

export function resolveSecureTokenStorageState(
  policy: SecretStoragePolicy,
  probeAvailable: () => boolean
): SecureTokenStorageState {
  if (!policy.on) {
    return { available: false, policyOn: false }
  }

  let available = false

  try {
    available = probeAvailable()
  } catch {
    available = false
  }

  return { available, policyOn: true }
}
