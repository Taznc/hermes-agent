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
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { updateChecksDisabled } from './update-checks-gate'

test('checks are enabled by default (no env set)', () => {
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
