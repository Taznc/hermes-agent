/**
 * Behavior contract for the fork secret-rewrite module: the store walker
 * rewrites v1 config, v2 registry, and the native token store; the one-shot
 * legacy migration decrypts safeStorage blobs to plain exactly once; the
 * Settings toggle encrypts/decrypts with a probe-first failure mode and a
 * rollback on mid-flight errors. Everything runs against in-memory stores.
 */

import { expect, test } from 'vitest'

import { SAFE_STORAGE_ENCODING } from '../hardening'

import {
  applySecretStorageEncryption,
  migrateLegacyEncryptedSecretsOnce,
  rewriteAllStoredSecrets,
  type SecretRewriteDeps
} from './secret-rewrite'

const enc = (value: string) => ({ encoding: SAFE_STORAGE_ENCODING, value: Buffer.from(value).toString('base64') })
const plain = (value: string) => ({ encoding: 'plain', value })

interface Harness {
  deps: SecretRewriteDeps
  state: {
    config: any
    registry: any
    nativeStore: string
    policy: { on: boolean; migrated: boolean }
    logs: string[]
  }
}

function harness(
  overrides: Partial<{
    config: any
    registry: any
    nativeStore: string
    policy: { on: boolean; migrated: boolean }
    isEncryptionAvailable: () => boolean
  }> = {}
): Harness {
  const state = {
    config: overrides.config ?? { remote: null, profiles: {} },
    registry: overrides.registry ?? { version: 2, connections: [] },
    nativeStore: overrides.nativeStore ?? '{}',
    policy: overrides.policy ?? { on: false, migrated: false },
    logs: [] as string[]
  }

  const deps: SecretRewriteDeps = {
    readDesktopConnectionConfig: () => state.config,
    writeDesktopConnectionConfig: config => {
      state.config = config
    },
    readDesktopConnectionsRegistry: () => state.registry,
    writeDesktopConnectionsRegistry: registry => {
      state.registry = registry
    },
    nativeTokenStoreIo: () =>
      ({
        encrypt: (value: string) => plain(value),
        decrypt: () => '',
        readStoreText: () => state.nativeStore,
        writeStoreText: (text: string) => {
          state.nativeStore = text
        }
      }) as any,
    secretStoragePolicy: () => state.policy,
    setSecretStoragePolicy: next => {
      state.policy = { on: next.on === true, migrated: next.migrated === true }
    },
    decryptDesktopSecret: secret =>
      secret?.encoding === SAFE_STORAGE_ENCODING
        ? Buffer.from(String(secret.value), 'base64').toString('utf8')
        : String(secret?.value || ''),
    encryptSecretStrict: value => enc(value),
    isEncryptionAvailable: overrides.isEncryptionAvailable ?? (() => true),
    rememberLog: message => state.logs.push(message)
  }

  return { deps, state }
}

test('rewriteAllStoredSecrets touches all three stores and reports whether anything changed', () => {
  const { deps, state } = harness({
    config: {
      remote: { token: plain('a'), headers: { 'X-H': plain('b') } },
      profiles: { work: { token: plain('c') } }
    },
    registry: { version: 2, connections: [{ id: 'r1', token: plain('d') }] },
    nativeStore: JSON.stringify({ 'https://gw': plain('e') })
  })

  const touched = rewriteAllStoredSecrets(
    deps,
    secret => secret?.encoding === 'plain',
    secret => (secret?.encoding === 'plain' ? { ...secret, value: secret.value.toUpperCase() } : secret)
  )

  expect(touched).toBe(true)
  expect(state.config.remote.token.value).toBe('A')
  expect(state.config.remote.headers['X-H'].value).toBe('B')
  expect(state.config.profiles.work.token.value).toBe('C')
  expect(state.registry.connections[0].token.value).toBe('D')
  expect(JSON.parse(state.nativeStore)['https://gw'].value).toBe('E')

  // A second pass with nothing left to rewrite reports untouched.
  expect(rewriteAllStoredSecrets(deps, () => false, s => s)).toBe(false)
})

test('migrateLegacyEncryptedSecretsOnce decrypts legacy blobs to plain and marks migrated', () => {
  const { deps, state } = harness({
    config: { remote: { token: enc('secret-token') }, profiles: {} },
    policy: { on: false, migrated: false }
  })

  migrateLegacyEncryptedSecretsOnce(deps)

  expect(state.config.remote.token).toEqual(plain('secret-token'))
  expect(state.policy).toEqual({ on: false, migrated: true })

  // Second call is a no-op (one-shot).
  state.config.remote.token = enc('another')
  migrateLegacyEncryptedSecretsOnce(deps)
  expect(state.config.remote.token).toEqual(enc('another'))
})

test('migrateLegacyEncryptedSecretsOnce does nothing while the policy is ON', () => {
  const { deps, state } = harness({
    config: { remote: { token: enc('keep-me') }, profiles: {} },
    policy: { on: true, migrated: false }
  })

  migrateLegacyEncryptedSecretsOnce(deps)

  expect(state.config.remote.token).toEqual(enc('keep-me'))
  expect(state.policy.on).toBe(true)
})

test('enabling encryption probes first: an unusable keychain throws before any store is touched', () => {
  const { deps, state } = harness({
    config: { remote: { token: plain('secret') }, profiles: {} },
    policy: { on: false, migrated: true },
    isEncryptionAvailable: () => {
      throw new Error('locked')
    }
  })

  expect(() => applySecretStorageEncryption(deps, true, SAFE_STORAGE_ENCODING)).toThrow(
    'OS keychain encryption is unavailable'
  )
  expect(state.config.remote.token).toEqual(plain('secret'))
  expect(state.policy.on).toBe(false)
})

test('enabling encryption re-encodes plain secrets and flips the policy on', () => {
  const { deps, state } = harness({
    config: { remote: { token: plain('secret') }, profiles: {} },
    policy: { on: false, migrated: true }
  })

  expect(applySecretStorageEncryption(deps, true, SAFE_STORAGE_ENCODING)).toEqual({ on: true })
  expect(state.config.remote.token).toEqual(enc('secret'))
  expect(state.policy).toEqual({ on: true, migrated: true })
})

test('a mid-flight encryption failure rolls the policy back off and rethrows', () => {
  const { deps, state } = harness({
    config: { remote: { token: plain('secret') }, profiles: {} },
    policy: { on: false, migrated: true }
  })

  deps.encryptSecretStrict = () => {
    throw new Error('keychain died mid-write')
  }

  expect(() => applySecretStorageEncryption(deps, true, SAFE_STORAGE_ENCODING)).toThrow('keychain died mid-write')
  expect(state.policy).toEqual({ on: false, migrated: true })
})

test('disabling encryption decrypts back to plain and flips the policy off', () => {
  const { deps, state } = harness({
    config: { remote: { token: enc('secret') }, profiles: {} },
    policy: { on: true, migrated: true }
  })

  expect(applySecretStorageEncryption(deps, false, SAFE_STORAGE_ENCODING)).toEqual({ on: false })
  expect(state.config.remote.token).toEqual(plain('secret'))
  expect(state.policy).toEqual({ on: false, migrated: true })
})

test('toggling to the current state is a no-op', () => {
  const { deps, state } = harness({
    config: { remote: { token: plain('secret') }, profiles: {} },
    policy: { on: false, migrated: true }
  })

  expect(applySecretStorageEncryption(deps, false, SAFE_STORAGE_ENCODING)).toEqual({ on: false })
  expect(state.config.remote.token).toEqual(plain('secret'))
})
