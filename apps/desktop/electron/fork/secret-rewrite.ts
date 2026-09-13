// Fork-owned: bulk re-encoding of every stored desktop secret — the shared
// rewrite walker plus its two consumers, the one-shot legacy migration and
// the Settings → Gateway encryption toggle. The stores it walks (v1
// connection.json, v2 connections.json registry, native OAuth token store)
// and the policy live behind injected deps so the logic is testable without
// Electron and the store formats stay owned by their upstream-shaped
// modules.

import type { NativeTokenStoreIo } from '../native-token-store'
import { classifyStoredSecret, type SecretStoragePolicy } from '../secret-storage-policy'

export interface SecretRewriteDeps {
  readDesktopConnectionConfig(): any
  writeDesktopConnectionConfig(config: any): void
  readDesktopConnectionsRegistry(): any
  writeDesktopConnectionsRegistry(registry: any): void
  nativeTokenStoreIo(): NativeTokenStoreIo
  secretStoragePolicy(): SecretStoragePolicy
  setSecretStoragePolicy(next: SecretStoragePolicy): void
  decryptDesktopSecret(secret: any): string
  /** Strict safeStorage encryption — THROWS when the keychain is unusable. */
  encryptSecretStrict(value: string): any
  /** Raw safeStorage availability probe (may throw). */
  isEncryptionAvailable(): boolean
  rememberLog(message: string): void
}

/**
 * Rewrite every stored desktop secret (v1 connection.json token/headers +
 * per-profile overrides, v2 registry connections, native OAuth token store)
 * through `reencode`. Returns true when any store was rewritten. Shared by
 * the one-shot legacy migration and the Settings encryption toggle.
 */
export function rewriteAllStoredSecrets(
  deps: SecretRewriteDeps,
  shouldRewrite: (secret: any) => boolean,
  reencode: (secret: any) => any
): boolean {
  let touched = false

  const rewriteBlock = (block: any) => {
    if (!block || typeof block !== 'object') {
      return block
    }

    const next = { ...block, ...(block.token ? { token: reencode(block.token) } : {}) }

    if (block.headers && typeof block.headers === 'object') {
      next.headers = Object.fromEntries(Object.entries(block.headers).map(([k, v]) => [k, reencode(v)]))
    }

    return next
  }

  const blockNeedsRewrite = (o: any) =>
    shouldRewrite(o?.token) ||
    Object.values(o?.headers && typeof o.headers === 'object' ? o.headers : {}).some(shouldRewrite)

  // v1 connection.json.
  const config = deps.readDesktopConnectionConfig()

  if (blockNeedsRewrite(config.remote) || Object.values(config.profiles || {}).some(blockNeedsRewrite)) {
    touched = true
    deps.writeDesktopConnectionConfig({
      ...config,
      remote: rewriteBlock(config.remote),
      profiles: Object.fromEntries(Object.entries(config.profiles || {}).map(([k, v]) => [k, rewriteBlock(v)]))
    })
  }

  // v2 connections.json registry.
  const registry = deps.readDesktopConnectionsRegistry()

  if (registry.connections?.some(blockNeedsRewrite)) {
    touched = true
    deps.writeDesktopConnectionsRegistry({ ...registry, connections: registry.connections.map(rewriteBlock) })
  }

  // Native OAuth token store: baseUrl → blob.
  const io = deps.nativeTokenStoreIo()

  try {
    const store = JSON.parse(io.readStoreText())

    if (store && typeof store === 'object' && !Array.isArray(store)) {
      const entries = Object.entries(store)

      if (entries.some(([, v]) => shouldRewrite(v))) {
        touched = true
        io.writeStoreText(JSON.stringify(Object.fromEntries(entries.map(([k, v]) => [k, reencode(v)]))))
      }
    }
  } catch {
    // Missing/corrupt native token store: nothing to rewrite.
  }

  return touched
}

/**
 * One-shot legacy migration: builds before the opt-in policy wrote every
 * secret as a safeStorage blob. With encryption now defaulting OFF, decrypt
 * each stored blob once and rewrite it as plain so no future launch touches
 * the keychain. Marked `migrated` whether or not every blob decrypts — a
 * broken keychain costs at most ONE prompt (this pass), never one per
 * launch; blobs that would not decrypt are left in place and simply read as
 * absent from then on (classifyStoredSecret → 'drop'), so opting encryption
 * back ON later can still recover them on a healthy keychain.
 *
 * Runs before createWindow() so every later read sees the final encodings.
 */
export function migrateLegacyEncryptedSecretsOnce(deps: SecretRewriteDeps): void {
  const policy = deps.secretStoragePolicy()

  if (policy.on || policy.migrated) {
    return
  }

  const needsMigration = (secret: any) => classifyStoredSecret(secret, policy) === 'migrate'

  const reencode = (secret: any) => {
    if (!needsMigration(secret)) {
      return secret
    }

    const plaintext = deps.decryptDesktopSecret(secret)

    // Undecryptable now (locked/absent keychain): keep the blob for a
    // potential future opt-in, but post-migration reads treat it as unset.
    return plaintext ? { encoding: 'plain', value: plaintext } : secret
  }

  let touchedKeychain = false

  try {
    touchedKeychain = rewriteAllStoredSecrets(deps, needsMigration, reencode)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)

    deps.rememberLog(`[secret-storage] legacy migration pass failed: ${detail}`)
  }

  deps.setSecretStoragePolicy({ on: false, migrated: true })

  if (touchedKeychain) {
    deps.rememberLog('[secret-storage] migrated legacy keychain-encrypted secrets to opt-out storage (one-shot pass)')
  }
}

/**
 * Settings → Gateway toggle: flip keychain-backed encryption and re-encode
 * every stored secret to match. Turning ON encrypts plain blobs through
 * strict safeStorage (throws loudly when the keychain is unusable — the
 * toggle stays off and the renderer shows the error). Turning OFF decrypts
 * back to plain; this is user-initiated, so a keychain prompt here is
 * expected and acceptable.
 */
export function applySecretStorageEncryption(
  deps: SecretRewriteDeps,
  on: boolean,
  safeStorageEncoding: string
): { on: boolean } {
  const enable = on === true

  if (deps.secretStoragePolicy().on === enable) {
    return { on: enable }
  }

  if (enable) {
    const needsEncrypt = (secret: any) => secret?.encoding === 'plain' && Boolean(secret.value)

    // Probe FIRST so an unusable keychain fails before any store is touched.
    if (
      !(() => {
        try {
          return Boolean(deps.isEncryptionAvailable())
        } catch {
          return false
        }
      })()
    ) {
      throw new Error(
        'OS keychain encryption is unavailable on this machine, so stored gateway secrets cannot be encrypted.'
      )
    }

    deps.setSecretStoragePolicy({ on: true, migrated: true })

    try {
      rewriteAllStoredSecrets(deps, needsEncrypt, secret =>
        needsEncrypt(secret) ? deps.encryptSecretStrict(String(secret.value)) : secret
      )
    } catch (error) {
      // Encryption failed midway: revert the policy so reads keep working
      // against whatever encodings are on disk (mixed stores read fine —
      // decryptDesktopSecret handles both encodings under either policy).
      deps.setSecretStoragePolicy({ on: false, migrated: true })
      throw error
    }

    return { on: true }
  }

  // Turning OFF: decrypt everything back to plain while the keychain is
  // still readable, then flip the policy.
  const needsDecrypt = (secret: any) => secret?.encoding === safeStorageEncoding

  rewriteAllStoredSecrets(deps, needsDecrypt, (secret: any) => {
    if (!needsDecrypt(secret)) {
      return secret
    }

    const plaintext = deps.decryptDesktopSecret(secret)

    return plaintext ? { encoding: 'plain', value: plaintext } : secret
  })

  deps.setSecretStoragePolicy({ on: false, migrated: true })

  return { on: false }
}
