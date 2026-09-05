/**
 * Tests for electron/update-checks-gate.ts — the desktop self-update
 * upstream-contact gate.
 *
 * Regression coverage: the real local dev workflow is `npm run dev` ->
 * `electron .` directly (apps/desktop/package.json's dev/dev:electron
 * scripts), which never goes through the `hermes desktop` CLI wrapper that
 * bridges `desktop.auto_update_checks_enabled` from config.yaml into
 * HERMES_DESKTOP_DISABLE_UPDATE_CHECKS. Before this gate also checked
 * HERMES_DEV directly, that dev workflow silently contacted upstream (git
 * ls-remote/fetch + the GitHub compare API) on every launch regardless of
 * config.yaml, because the env-bridging wrapper was never in the loop.
 *
 * Second regression covered here: a packaged Hermes.app launched from the
 * Dock, Finder, or Spotlight (never through the `hermes desktop` CLI at all)
 * gets NONE of the bridged env vars — HERMES_DESKTOP_DISABLE_UPDATE_CHECKS is
 * simply absent from process.env, same as a fresh install that never set
 * config.yaml's `desktop.auto_update_checks_enabled` to false. Without a
 * fallback rung that reads config.yaml directly, that user's opt-out has no
 * effect on the launch surface they actually use.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { readConfigAutoUpdateChecksEnabled, updateChecksDisabled } from './update-checks-gate'

function tmpConfig(yamlBody: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-gate-'))
  const file = path.join(dir, 'config.yaml')
  fs.writeFileSync(file, yamlBody, 'utf8')

  return file
}

test('checks are enabled by default (no env set, no config path)', () => {
  assert.equal(updateChecksDisabled({}), false)
})

test('HERMES_DEV=1 disables checks even with no other env set', () => {
  assert.equal(updateChecksDisabled({ HERMES_DEV: '1' }), true)
})

test('HERMES_DEV set to anything other than "1" does not disable checks', () => {
  assert.equal(updateChecksDisabled({ HERMES_DEV: 'true' }), false)
  assert.equal(updateChecksDisabled({ HERMES_DEV: '0' }), false)
})

for (const truthy of ['1', 'true', 'yes', 'on', 'TRUE', 'On']) {
  test(`HERMES_DESKTOP_DISABLE_UPDATE_CHECKS=${truthy} disables checks`, () => {
    assert.equal(updateChecksDisabled({ HERMES_DESKTOP_DISABLE_UPDATE_CHECKS: truthy }), true)
  })
}

for (const falsy of ['0', 'false', 'no', 'off', '']) {
  test(`HERMES_DESKTOP_DISABLE_UPDATE_CHECKS=${JSON.stringify(falsy)} leaves checks enabled`, () => {
    assert.equal(updateChecksDisabled({ HERMES_DESKTOP_DISABLE_UPDATE_CHECKS: falsy }), false)
  })
}

test('either guard alone is sufficient — HERMES_DEV wins even if the env var says enabled', () => {
  assert.equal(
    updateChecksDisabled({ HERMES_DEV: '1', HERMES_DESKTOP_DISABLE_UPDATE_CHECKS: '0' }),
    true
  )
})

// ─── config.yaml fallback rung (Dock/Finder/Spotlight launches) ────────────

test('readConfigAutoUpdateChecksEnabled returns null for a missing file', () => {
  assert.equal(readConfigAutoUpdateChecksEnabled('/nonexistent/config.yaml'), null)
})

test('readConfigAutoUpdateChecksEnabled parses desktop.auto_update_checks_enabled: false', () => {
  const configPath = tmpConfig('model:\n  default: foo\ndesktop:\n  auto_update_checks_enabled: false\nterminal:\n  cwd: .\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), false)
})

test('readConfigAutoUpdateChecksEnabled parses desktop.auto_update_checks_enabled: true', () => {
  const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: true\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), true)
})

test('readConfigAutoUpdateChecksEnabled returns null when desktop section is absent', () => {
  const configPath = tmpConfig('model:\n  default: foo\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), null)
})

test('readConfigAutoUpdateChecksEnabled returns null when the key is absent from desktop section', () => {
  const configPath = tmpConfig('desktop:\n  repo_scan_enabled: true\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), null)
})

test('readConfigAutoUpdateChecksEnabled handles a trailing comment on the value line', () => {
  const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: false  # set by the user\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), false)
})

test('readConfigAutoUpdateChecksEnabled stops at the next top-level key', () => {
  const configPath = tmpConfig('desktop:\n  repo_scan_enabled: true\nterminal:\n  auto_update_checks_enabled: false\n')
  assert.equal(readConfigAutoUpdateChecksEnabled(configPath), null)
})

test(
  'updateChecksDisabled: no env vars + config.yaml auto_update_checks_enabled: false ' +
    '=> disabled (Dock/Finder/Spotlight launch simulation)',
  () => {
    const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: false\n')
    assert.equal(updateChecksDisabled({}, { configPath }), true)
  }
)

test('updateChecksDisabled: no env vars + config.yaml with no desktop section => enabled', () => {
  const configPath = tmpConfig('model:\n  default: foo\n')
  assert.equal(updateChecksDisabled({}, { configPath }), false)
})

test('updateChecksDisabled: no env vars + missing config.yaml => enabled (safe default)', () => {
  assert.equal(updateChecksDisabled({}, { configPath: '/nonexistent/config.yaml' }), false)
})

test('updateChecksDisabled: an explicit env var wins over config.yaml, even when set to falsy', () => {
  const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: false\n')
  assert.equal(
    updateChecksDisabled({ HERMES_DESKTOP_DISABLE_UPDATE_CHECKS: '0' }, { configPath }),
    false
  )
})

test('updateChecksDisabled: an explicit env var wins over config.yaml when set to truthy', () => {
  const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: true\n')
  assert.equal(
    updateChecksDisabled({ HERMES_DESKTOP_DISABLE_UPDATE_CHECKS: '1' }, { configPath }),
    true
  )
})

test('updateChecksDisabled: HERMES_DEV=1 wins over config.yaml saying checks are enabled', () => {
  const configPath = tmpConfig('desktop:\n  auto_update_checks_enabled: true\n')
  assert.equal(updateChecksDisabled({ HERMES_DEV: '1' }, { configPath }), true)
})
