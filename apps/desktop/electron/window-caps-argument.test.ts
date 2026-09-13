/**
 * Regression guard for the window-caps argument (see WINDOW_CAPS_ARGUMENT /
 * withWindowCapsArgument() in main.ts).
 *
 * preload.ts no longer answers translucency/HUD-windowing questions via
 * `ipcRenderer.sendSync` -- it parses `--hermes-window-caps=<json>` off
 * `process.argv` instead. That means every `BrowserWindow` whose
 * `webPreferences.preload` is `PRELOAD_PATH` MUST have its `webPreferences`
 * wrapped in `withWindowCapsArgument(...)`, or that window's renderer
 * silently gets `glassSupported`/`translucencySupported` forced `false` and
 * `hudWindowing` `undefined` -- with no runtime error, since `parseWindowCaps()`
 * in preload.ts just falls back to `{}` when the argument is absent.
 *
 * main.ts pulls in the full Electron runtime at module scope (app, session,
 * BrowserWindow, ...), so it can't be `import`ed directly in a Vitest node
 * environment. This test instead parses the source text directly: a cheap,
 * reliable static check that every `webPreferences: chatWindowWebPreferences(PRELOAD_PATH)`
 * call site is wrapped in `withWindowCapsArgument(...)`, and every raw
 * `preload: PRELOAD_PATH` object literal either is inside such a wrap or is
 * an explicitly acknowledged exception.
 *
 * If a future window kind is added and forgets the wrap, this test fails
 * with the offending line instead of the regression only surfacing as "glass
 * silently doesn't work in window X" days later (see round-1 review finding
 * on t_331b1a5e: spawnSecondaryWindow() shipped without the wrap).
 */

import fs from 'node:fs'
import path from 'node:path'

import { expect, test } from 'vitest'

const MAIN_TS_PATH = path.join(__dirname, 'main.ts')
const source = fs.readFileSync(MAIN_TS_PATH, 'utf8')

test('every chatWindowWebPreferences(PRELOAD_PATH) call site is wrapped in withWindowCapsArgument', () => {
  // Every bare (unwrapped) use of `chatWindowWebPreferences(PRELOAD_PATH)` as
  // a webPreferences value is exactly the class of bug this guards against:
  // that helper builds a full chat-window webPreferences object with no
  // caps argument of its own, so unwrapped means the window loses
  // glass/translucency/HUD windowing capability at first paint.
  const bareCalls = source.match(/webPreferences:\s*chatWindowWebPreferences\(PRELOAD_PATH\)/g) || []

  expect(bareCalls, 'found an unwrapped `webPreferences: chatWindowWebPreferences(PRELOAD_PATH)` — wrap it in withWindowCapsArgument(...) so the window receives WINDOW_CAPS_ARGUMENT').toHaveLength(0)

  // Sanity: the wrapped form must actually exist at least once, or this test
  // would trivially pass if someone renamed/removed the pattern entirely.
  const wrappedCalls = source.match(/withWindowCapsArgument\(chatWindowWebPreferences\(PRELOAD_PATH\)\)/g) || []

  expect(wrappedCalls.length).toBeGreaterThan(0)
})

test('every `preload: PRELOAD_PATH` webPreferences object literal is reachable through withWindowCapsArgument', () => {
  // Some windows (pet overlay, quick entry, the HUD's own inline
  // webPreferences) build their webPreferences object inline instead of via
  // chatWindowWebPreferences(). For those, `preload: PRELOAD_PATH` must
  // appear as part of an object passed to withWindowCapsArgument(...), or as
  // the `preloadPath` field fed into createWakeIndicatorWindowController's
  // own additionalArguments wiring (which injects WINDOW_CAPS_ARGUMENT
  // itself -- see the `additionalArguments: [WINDOW_CAPS_ARGUMENT]` call
  // site next to it).
  const lines = source.split('\n')
  const offenders: number[] = []

  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].includes('preload: PRELOAD_PATH')) {
      continue
    }

    // Look backwards a small window for the opening `withWindowCapsArgument({`
    // that this `preload: PRELOAD_PATH` line should be nested inside.
    const context = lines.slice(Math.max(0, i - 5), i + 1).join('\n')

    if (!context.includes('withWindowCapsArgument({')) {
      offenders.push(i + 1)
    }
  }

  expect(offenders, `preload: PRELOAD_PATH used outside withWindowCapsArgument({...}) at line(s) ${offenders.join(', ')} — every preloaded window must receive WINDOW_CAPS_ARGUMENT`).toEqual([])
})
