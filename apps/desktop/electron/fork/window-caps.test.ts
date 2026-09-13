/**
 * Behavior contract for the fork window-caps integration: the
 * `--hermes-window-caps=<json>` argument preload parses off `process.argv`
 * (see preload.ts parseWindowCaps), and the wrapper every preloaded window's
 * webPreferences must pass through.
 */

import { expect, test } from 'vitest'

import { hudWindowingView, resolveHudWindowing } from '../hud-windowing'

import { createWindowCapsIntegration } from './window-caps'

function integration(overrides: Partial<Parameters<typeof createWindowCapsIntegration>[0]> = {}) {
  return createWindowCapsIntegration({
    glassSupported: true,
    translucencySupported: true,
    platform: 'linux',
    env: {},
    argv: [],
    ...overrides
  })
}

function parseCapsArgument(argument: string) {
  expect(argument.startsWith('--hermes-window-caps=')).toBe(true)

  return JSON.parse(decodeURIComponent(argument.slice('--hermes-window-caps='.length)))
}

test('WINDOW_CAPS_ARGUMENT carries glass/translucency verbatim and the resolved HUD windowing view', () => {
  const caps = parseCapsArgument(integration({ glassSupported: true, translucencySupported: false }).WINDOW_CAPS_ARGUMENT)

  expect(caps.glass).toBe(true)
  expect(caps.translucency).toBe(false)
  // The HUD view must be exactly what the shared resolver answers for the
  // same inputs — the renderer's HUD windowing must not drift from main's.
  expect(caps.hud).toEqual(hudWindowingView(resolveHudWindowing('linux', {}, [])))
})

test('withWindowCapsArgument appends the caps argument without dropping existing additionalArguments', () => {
  const { WINDOW_CAPS_ARGUMENT, withWindowCapsArgument } = integration()

  const wrapped = withWindowCapsArgument({
    preload: '/tmp/preload.js',
    additionalArguments: ['--existing-flag']
  })

  expect(wrapped.preload).toBe('/tmp/preload.js')
  expect(wrapped.additionalArguments).toEqual(['--existing-flag', WINDOW_CAPS_ARGUMENT])
})

test('withWindowCapsArgument tolerates webPreferences with no additionalArguments', () => {
  const { WINDOW_CAPS_ARGUMENT, withWindowCapsArgument } = integration()

  const wrapped = withWindowCapsArgument({ preload: '/tmp/preload.js' })

  expect(wrapped.additionalArguments).toEqual([WINDOW_CAPS_ARGUMENT])
})

test('withWindowCapsArgument does not mutate its input', () => {
  const { withWindowCapsArgument } = integration()
  const input = { preload: '/tmp/preload.js', additionalArguments: ['--a'] }

  withWindowCapsArgument(input)

  expect(input.additionalArguments).toEqual(['--a'])
})
