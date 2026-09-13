/**
 * Trusted-input verification for the Phase 2.7 copy controls on structured
 * tool output (t_f660da56). Unit tests exercise the components with jsdom's
 * synthetic `fireEvent.click`, which cannot prove the button is actually
 * hit-testable in a real compositor. This spec drives the real Electron
 * renderer through Playwright, which dispatches genuine OS-level input
 * (CDP `Input.dispatchMouseEvent`), and asserts the clipboard receives the
 * exact source string — including the long `--disable-pip-version-check`
 * flag the card calls out by name. It also pins the Phase 2.3 pet-overlay
 * non-interference invariant against the new control.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { TERMINAL_COPY_COMMAND, TERMINAL_COPY_TRIGGER } from './mock-server'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('copy button on a terminal tool card copies the exact command via real mouse input, and survives a pet overlay', async () => {
  const page = fixture!.page

  // Let the app read/write the system clipboard without a permission prompt.
  await fixture!.app.context().grantPermissions?.(['clipboard-read', 'clipboard-write']).catch(() => undefined)

  const composer = page.locator('[data-slot="composer-rich-input"]').first()
  await composer.click()
  await composer.pressSequentially(TERMINAL_COPY_TRIGGER)
  await page.keyboard.press('Enter')

  // Wait for the terminal tool card to render, then expand it — the
  // disclosure row starts collapsed, and TerminalTranscript's `<code>` only
  // mounts once `open` is true.
  const disclosure = page.getByRole('button', { name: /^Ran /, exact: false }).filter({ hasText: 'pip install' }).first()
  await disclosure.waitFor({ state: 'visible', timeout: 60_000 })
  await disclosure.click()

  const commandCode = page.locator('code', { hasText: TERMINAL_COPY_COMMAND }).first()
  await commandCode.waitFor({ state: 'visible', timeout: 10_000 })

  // The copy button is hover-revealed (opacity-0 by default) — a real mouse
  // hover, not a programmatic focus, is the trusted-input path the card asks
  // for. Hover the transcript row before locating the button by its
  // accessible name.
  await commandCode.hover()

  const copyButton = page
    .locator('.group\\/terminal-transcript')
    .filter({ has: page.locator('code', { hasText: TERMINAL_COPY_COMMAND }) })
    .getByRole('button', { name: /copy/i })
    .first()

  await copyButton.waitFor({ state: 'visible', timeout: 10_000 })
  await copyButton.click()

  // Read the real system clipboard back through the renderer, byte-for-byte.
  await expect
    .poll(async () => page.evaluate(() => navigator.clipboard.readText()), { timeout: 10_000 })
    .toBe(TERMINAL_COPY_COMMAND)

  // ── Phase 2.3 non-regression: a pet-shaped overlay must not eat the click ──
  //
  // floating-pet.tsx is fixed-position, z-index 60, pointer-events:none by
  // default — it only turns pointer-events:auto while the cursor sits on an
  // alpha-sampled OPAQUE sprite pixel (pet-hit-test.ts), never as a blanket
  // property of its bounding box. Hatching a real pet needs sprite/gallery
  // state this suite doesn't set up, so this reproduces the #95001
  // regression shape directly: drop a fixed, same-z-index, click-through
  // overlay exactly on top of the hover-revealed copy button and prove a
  // real click still lands on the button underneath, not the overlay.
  await page.evaluate(() => navigator.clipboard.writeText('cleared-before-pet-overlay-click'))

  await commandCode.hover()
  await copyButton.waitFor({ state: 'visible', timeout: 10_000 })

  const box = await copyButton.boundingBox()
  expect(box).not.toBeNull()

  await page.evaluate(rect => {
    const overlay = document.createElement('div')
    overlay.id = 'e2e-fake-pet-overlay'
    Object.assign(overlay.style, {
      position: 'fixed',
      left: `${rect!.x - 4}px`,
      top: `${rect!.y - 4}px`,
      width: `${rect!.width + 8}px`,
      height: `${rect!.height + 8}px`,
      zIndex: '60',
      pointerEvents: 'none',
      background: 'rgba(255, 0, 255, 0.01)'
    })
    document.body.appendChild(overlay)
  }, box)

  await copyButton.click()

  await expect
    .poll(async () => page.evaluate(() => navigator.clipboard.readText()), { timeout: 10_000 })
    .toBe(TERMINAL_COPY_COMMAND)

  await page.evaluate(() => document.getElementById('e2e-fake-pet-overlay')?.remove())
})
