// Fork-owned: the Electron-coupled IO glue for the opt-in secret-storage
// policy and the native OAuth token store — file quarantine, atomic writes,
// the "last deliberate write was ON" durability marker, and the honest
// keychain probe. The policy/store decision logic itself stays in the
// upstream-shaped modules (../secret-storage-policy.ts,
// ../native-token-store.ts); this module owns only the side-effect bodies
// main.ts used to carry inline.

import fs from 'node:fs'
import path from 'node:path'

import { writeSecretFileAtomic } from '../hardening'
import type { NativeTokenStoreIo } from '../native-token-store'
import {
  resolveSecureTokenStorageState,
  type SecretStoragePolicy,
  type SecretStoragePolicyIo,
  type SecureTokenStorageState
} from '../secret-storage-policy'

export interface SecretStorageIntegrationDeps {
  /** Lazy so the store can follow a userData path resolved after app-ready. */
  nativeTokenStorePath(): string
  policyPath: string
  lastOnMarkerPath: string
  encrypt: NativeTokenStoreIo['encrypt']
  decrypt: NativeTokenStoreIo['decrypt']
  /** Raw safeStorage probe — only ever called while the policy is ON. */
  isEncryptionAvailable(): boolean
  secretStoragePolicy(): SecretStoragePolicy
  rememberLog(message: string): void
}

export interface SecretStorageIntegration {
  nativeTokenStoreIo(): NativeTokenStoreIo
  secretStoragePolicyIo: SecretStoragePolicyIo
  /**
   * Keychain availability as the renderer should see it. With encryption
   * opted out this must NOT probe safeStorage — isEncryptionAvailable() is
   * itself a keychain touch that raises the macOS dialog this feature exists
   * to avoid. We report `true` so the plain-text CONFIRMATION dialog (the
   * "your keychain is broken, opt into plaintext to continue" flow) never
   * fires: storing plaintext with the policy off is the user's chosen
   * (default) mode, not a degraded state that needs a gate.
   *
   * This does NOT mean the renderer is blind to the real state — see
   * probeSecureTokenStorageState(), which callers of this function also read
   * and forward honestly as `secretStorageState`.
   */
  probeSecureTokenStorage(): boolean
  /**
   * The full, honest answer to "is what I save right now actually OS-keychain
   * encrypted?" — `{ available, policyOn }`. Unlike probeSecureTokenStorage()
   * (which exists only to gate the plaintext-opt-in CONFIRM dialog and
   * intentionally reads as "fine" while the policy is off), this is what the
   * renderer uses to show an honest, non-blocking "stored without OS keychain
   * encryption" hint instead of asserting security it cannot back up.
   */
  probeSecureTokenStorageState(): SecureTokenStorageState
}

export function createSecretStorageIntegration(deps: SecretStorageIntegrationDeps): SecretStorageIntegration {
  // The electron-coupled half of the token store: safeStorage encryption plus
  // the userData file. native-token-store.ts owns the serialization/parse
  // round trip so it can be tested without an Electron runtime.
  function nativeTokenStoreIo(): NativeTokenStoreIo {
    return {
      encrypt: deps.encrypt,
      decrypt: deps.decrypt,
      readStoreText: () => fs.readFileSync(deps.nativeTokenStorePath(), 'utf8'),
      // Atomic (temp file + rename, owner-only mode) via the same helper the
      // adjacent secret-storage-policy.json write uses — a crash/power-loss/
      // full-disk failure mid-write can now only ever leave the OLD file intact
      // or the NEW file complete, never a truncated hybrid that reads as an
      // empty store and silently drops every other gateway's tokens on the
      // next persist.
      writeStoreText: (text: string) => {
        fs.mkdirSync(path.dirname(deps.nativeTokenStorePath()), { recursive: true })
        writeSecretFileAtomic(deps.nativeTokenStorePath(), text, { encoding: 'utf8' })
      },
      // A store that fails to JSON.parse is quarantined instead of being
      // silently treated as empty and then overwritten on the next persist —
      // rename it aside so the corrupt bytes aren't lost to a "recoverable"
      // read that isn't actually recoverable once the next write lands.
      preserveCorruptStore: () => {
        const target = deps.nativeTokenStorePath()
        const quarantined = `${target}.corrupt-${Date.now()}`

        try {
          fs.renameSync(target, quarantined)
          deps.rememberLog(`[native-oauth] quarantined corrupt token store at ${quarantined}`)
        } catch (error) {
          deps.rememberLog(
            `[native-oauth] failed to quarantine corrupt token store: ${
              error instanceof Error ? error.message : String(error)
            }`
          )
        }
      },
      rememberLog: deps.rememberLog
    }
  }

  const secretStoragePolicyIo: SecretStoragePolicyIo = {
    readText: () => fs.readFileSync(deps.policyPath, 'utf8'),
    writeText: (text: string) => writeSecretFileAtomic(deps.policyPath, text, { encoding: 'utf8' }),
    // A present-but-corrupt policy file is quarantined rather than silently
    // treated as the OFF default — see readSecretStoragePolicy's doc comment
    // for why "corrupt while present" and "absent" must not read the same.
    preserveCorruptPolicy: () => {
      const quarantined = `${deps.policyPath}.corrupt-${Date.now()}`

      try {
        fs.renameSync(deps.policyPath, quarantined)
        deps.rememberLog(`[secret-storage] quarantined corrupt policy file at ${quarantined}`)
      } catch (error) {
        deps.rememberLog(
          `[secret-storage] failed to quarantine corrupt policy file: ${
            error instanceof Error ? error.message : String(error)
          }`
        )
      }
    },
    // The marker's mere PRESENCE means "last deliberate write was ON" — its
    // content is irrelevant, only existence is checked, so a truncated/corrupt
    // marker file still correctly answers "yes, it existed" rather than needing
    // its own corruption-handling ladder for a one-bit signal.
    readLastKnownOn: () => fs.existsSync(deps.lastOnMarkerPath),
    writeLastKnownOn: (on: boolean) => {
      if (on) {
        writeSecretFileAtomic(deps.lastOnMarkerPath, String(Date.now()), { encoding: 'utf8' })
      } else {
        try {
          fs.rmSync(deps.lastOnMarkerPath, { force: true })
        } catch {
          // Best-effort removal; a stale marker after an explicit turn-OFF just
          // means a future disappearance of the main policy file conservatively
          // reads as ON again, which is the safe direction to fail in.
        }
      }
    },
    rememberLog: deps.rememberLog
  }

  function probeSecureTokenStorageState(): SecureTokenStorageState {
    return resolveSecureTokenStorageState(deps.secretStoragePolicy(), () => Boolean(deps.isEncryptionAvailable()))
  }

  function probeSecureTokenStorage(): boolean {
    const state = probeSecureTokenStorageState()

    return state.policyOn ? state.available : true
  }

  return { nativeTokenStoreIo, secretStoragePolicyIo, probeSecureTokenStorage, probeSecureTokenStorageState }
}
