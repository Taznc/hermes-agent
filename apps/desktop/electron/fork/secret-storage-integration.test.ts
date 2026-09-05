/**
 * Behavior contract for the fork secret-storage integration: the
 * Electron-coupled IO bodies (atomic store writes, corruption quarantine,
 * the last-on marker, and the gated vs honest keychain probes), exercised
 * against a REAL temp directory the way main.ts wires them. The policy/store
 * decision logic itself is covered by ../secret-storage-policy.test.ts and
 * ../native-token-store*.test.ts.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, expect, test } from 'vitest'

import { createSecretStorageIntegration, type SecretStorageIntegrationDeps } from './secret-storage-integration'

let dir: string
let logs: string[]

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-secret-storage-integration-'))
  logs = []
})

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true })
})

function integration(overrides: Partial<SecretStorageIntegrationDeps> = {}) {
  return createSecretStorageIntegration({
    nativeTokenStorePath: () => path.join(dir, 'store', 'native-oauth-tokens.json'),
    policyPath: path.join(dir, 'secure-token-storage.json'),
    lastOnMarkerPath: path.join(dir, 'secure-token-storage.last-on'),
    encrypt: plaintext => ({ encoding: 'plain', value: plaintext }),
    decrypt: secret => String(secret?.value || ''),
    isEncryptionAvailable: () => true,
    secretStoragePolicy: () => ({ on: false, migrated: true }),
    rememberLog: message => logs.push(message),
    ...overrides
  })
}

test('native token store writeStoreText creates parent dirs and round-trips through readStoreText', () => {
  const io = integration().nativeTokenStoreIo()

  io.writeStoreText('{"a":1}')
  expect(io.readStoreText()).toBe('{"a":1}')
})

test('preserveCorruptStore renames the store aside so the next read throws ENOENT', () => {
  const io = integration().nativeTokenStoreIo()

  io.writeStoreText('not json')
  io.preserveCorruptStore!()

  expect(() => io.readStoreText()).toThrow()

  const quarantined = fs.readdirSync(path.join(dir, 'store')).filter(name => name.includes('.corrupt-'))

  expect(quarantined).toHaveLength(1)
  expect(logs.some(line => line.includes('quarantined corrupt token store'))).toBe(true)
})

test('preserveCorruptStore on an absent store logs the failure instead of throwing', () => {
  const io = integration().nativeTokenStoreIo()

  io.preserveCorruptStore!()

  expect(logs.some(line => line.includes('failed to quarantine'))).toBe(true)
})

test('policy IO round-trips and quarantines a corrupt policy file', () => {
  const { secretStoragePolicyIo } = integration()

  secretStoragePolicyIo.writeText('{"on":true}')
  expect(secretStoragePolicyIo.readText()).toBe('{"on":true}')

  secretStoragePolicyIo.preserveCorruptPolicy!()
  expect(() => secretStoragePolicyIo.readText()).toThrow()
  expect(logs.some(line => line.includes('quarantined corrupt policy file'))).toBe(true)
})

test('the last-on marker answers by presence: write ON creates it, write OFF removes it', () => {
  const { secretStoragePolicyIo } = integration()

  expect(secretStoragePolicyIo.readLastKnownOn!()).toBe(false)

  secretStoragePolicyIo.writeLastKnownOn!(true)
  expect(secretStoragePolicyIo.readLastKnownOn!()).toBe(true)

  secretStoragePolicyIo.writeLastKnownOn!(false)
  expect(secretStoragePolicyIo.readLastKnownOn!()).toBe(false)

  // Removing an already-absent marker must not throw.
  secretStoragePolicyIo.writeLastKnownOn!(false)
})

test('probeSecureTokenStorage reads true with the policy OFF without touching safeStorage', () => {
  let probed = 0

  const it = integration({
    secretStoragePolicy: () => ({ on: false, migrated: true }),
    isEncryptionAvailable: () => {
      probed += 1

      return false
    }
  })

  // Gated probe: policy off → true, and the keychain must NOT be touched
  // (isEncryptionAvailable raises the macOS dialog this feature avoids).
  expect(it.probeSecureTokenStorage()).toBe(true)
  expect(probed).toBe(0)

  // Honest state still reports policyOn: false.
  expect(it.probeSecureTokenStorageState()).toEqual({ available: false, policyOn: false })
})

test('probeSecureTokenStorage reports the real availability with the policy ON', () => {
  const healthy = integration({
    secretStoragePolicy: () => ({ on: true, migrated: true }),
    isEncryptionAvailable: () => true
  })

  expect(healthy.probeSecureTokenStorage()).toBe(true)
  expect(healthy.probeSecureTokenStorageState()).toEqual({ available: true, policyOn: true })

  const broken = integration({
    secretStoragePolicy: () => ({ on: true, migrated: true }),
    isEncryptionAvailable: () => {
      throw new Error('keychain locked')
    }
  })

  expect(broken.probeSecureTokenStorage()).toBe(false)
  expect(broken.probeSecureTokenStorageState()).toEqual({ available: false, policyOn: true })
})
