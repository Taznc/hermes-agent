/**
 * E2E batch clarify test — the multi-question clarify card must mount ONCE.
 *
 * Regression coverage for the duplicated-card bug: `tool.start` carries the
 * model's tool_call_id while `clarify.request` carries a gateway-generated
 * request_id. A batch payload has no top-level `question`, so the two rows
 * only merge when the correlation key comes from the question list
 * (`batchClarifyMatchValue` in lib/chat-messages/tool-parts.ts). Before that
 * fix this exact flow rendered two identical interactive cards.
 *
 * The flow runs the real chain: composer → gateway → agent → clarify tool →
 * clarify.request event → renderer, against the mock inference server.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { BATCH_CLARIFY_QUESTIONS, BATCH_CLARIFY_TRIGGER } from './mock-server'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture!, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('batch clarify card', () => {
  test('renders exactly one card and completes via per-question locks', async () => {
    const page = fixture!.page
    const composer = page.locator('[contenteditable="true"]').first()
    await composer.waitFor({ state: 'visible', timeout: 10_000 })

    await composer.click()
    await composer.type(BATCH_CLARIFY_TRIGGER, { delay: 20 })
    await page.keyboard.press('Enter')

    // The live batch form marks itself with data-clarify-batch=<count>.
    const batchCard = page.locator('form[data-clarify-batch]')
    await batchCard.first().waitFor({ state: 'visible', timeout: 60_000 })

    // THE regression assertion: one card, not two.
    await expect(batchCard).toHaveCount(1)
    await expect(batchCard).toHaveAttribute('data-clarify-batch', String(BATCH_CLARIFY_QUESTIONS.length))

    // Both questions render inside the single card.
    for (const entry of BATCH_CLARIFY_QUESTIONS) {
      await expect(batchCard.getByText(entry.question)).toHaveCount(1)
    }

    // Each question text also appears exactly once in the whole transcript —
    // catches a duplicate that mounts outside a form[data-clarify-batch].
    for (const entry of BATCH_CLARIFY_QUESTIONS) {
      await expect(page.getByText(entry.question)).toHaveCount(1)
    }

    // Choice help stays anchored inside the card at a narrow window and larger
    // text. This is the real renderer layout contract: no descendant may cross
    // the form bounds and the page itself must not gain horizontal overflow.
    const firstQuestion = batchCard.locator('[data-clarify-batch-question]').first()
    await firstQuestion.getByRole('button', { name: /Ask about Coffee/ }).click()
    const followUp = firstQuestion.locator('[data-clarify-follow-up] textarea')
    await expect(followUp).toBeVisible()

    // Textareas are transparent, so measure the effective placeholder colour
    // after alpha-compositing it over every painted ancestor. The previous
    // tertiary token measured 3.55:1 on this light surface; secondary must
    // clear WCAG AA without making the whole card permanently bright.
    const placeholderContrast = await followUp.evaluate(field => {
      type Rgba = [number, number, number, number]

      // Computed colours arrive in whatever space the token was mixed in
      // (rgb, color(srgb …), oklab(…) from Tailwind's color-mix). Let the
      // canvas resolve them to sRGB bytes instead of parsing each syntax.
      const scratch = document.createElement('canvas').getContext('2d', { willReadFrequently: true })

      if (!scratch) {
        throw new Error('2D canvas unavailable for colour normalisation')
      }

      const parse = (value: string): Rgba => {
        scratch.clearRect(0, 0, 1, 1)
        scratch.fillStyle = value
        scratch.fillRect(0, 0, 1, 1)

        const [red, green, blue, alpha] = scratch.getImageData(0, 0, 1, 1).data

        // A translucent fill un-premultiplies on read, so channels stay in
        // straight (non-premultiplied) form for the compositor below.
        return [red / 255, green / 255, blue / 255, alpha / 255]
      }

      const composite = (foreground: Rgba, background: Rgba): Rgba => {
        const alpha = foreground[3] + background[3] * (1 - foreground[3])

        return [
          (foreground[0] * foreground[3] + background[0] * background[3] * (1 - foreground[3])) / alpha,
          (foreground[1] * foreground[3] + background[1] * background[3] * (1 - foreground[3])) / alpha,
          (foreground[2] * foreground[3] + background[2] * background[3] * (1 - foreground[3])) / alpha,
          alpha
        ]
      }

      const ancestors: Element[] = []

      for (let current: Element | null = field; current; current = current.parentElement) {
        ancestors.unshift(current)
      }

      const background = ancestors.reduce<Rgba>(
        (painted, element) => composite(parse(getComputedStyle(element).backgroundColor), painted),
        [1, 1, 1, 1]
      )

      const placeholder = composite(parse(getComputedStyle(field, '::placeholder').color), background)

      const luminance = (color: Rgba) =>
        [color[0], color[1], color[2]]
          .map(channel => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4))
          .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0)

      const foregroundLuminance = luminance(placeholder)
      const backgroundLuminance = luminance(background)

      return (
        (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
        (Math.min(foregroundLuminance, backgroundLuminance) + 0.05)
      )
    })

    expect(placeholderContrast).toBeGreaterThanOrEqual(4.5)

    await fixture!.app.evaluate(({ BrowserWindow }) => {
      const win = BrowserWindow.getAllWindows()[0]
      win.setMinimumSize(0, 0)
      win.setSize(720, 900)
    })
    await page.evaluate(() => {
      document.documentElement.style.fontSize = '24px'
    })
    await expect
      .poll(() =>
        batchCard.evaluate(form => {
          const bounds = form.getBoundingClientRect()

          const overflow = [...form.querySelectorAll('*')].filter(element => {
            const rect = element.getBoundingClientRect()

            return rect.width > 0 && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1)
          })

          return overflow.length === 0 && document.documentElement.scrollWidth <= window.innerWidth + 1
        })
      )
      .toBe(true)
    await fixture!.app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0].setSize(720, 1400)
    })
    await expect
      .poll(() =>
        batchCard.evaluate(form => {
          const bounds = form.getBoundingClientRect()

          return bounds.top >= -1 && bounds.bottom <= window.innerHeight + 1
        })
      )
      .toBe(true)
    await expect(firstQuestion.getByText(BATCH_CLARIFY_QUESTIONS[0].question)).toBeVisible()
    await expect(followUp).toBeVisible()
    await test.info().attach('clarify-card-narrow-open-ask', {
      body: await page.screenshot(),
      contentType: 'image/png'
    })
    await fixture!.app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0].setSize(1220, 900)
    })
    await page.evaluate(() => {
      document.documentElement.style.fontSize = ''
    })

    // Answer both questions: stage picks locally (no server traffic yet).
    const confirmButton = batchCard.locator('button[type="submit"]')
    await expect(confirmButton).toContainText('Confirm and continue')
    await expect(confirmButton).toBeDisabled()

    await batchCard
      .locator('[data-choice]')
      .filter({ hasText: /Coffee/ })
      .click()
    await expect(confirmButton).toBeDisabled()

    await batchCard
      .locator('[data-choice]')
      .filter({ hasText: /Morning/ })
      .click()
    await expect(confirmButton).toBeEnabled()

    // ONE confirm submits the whole batch.
    await confirmButton.click()

    // The settled card lists both questions with their locked answers.
    const settled = page.locator('[data-clarify-settled]')
    await settled.waitFor({ state: 'visible', timeout: 30_000 })
    await expect(settled.getByText(BATCH_CLARIFY_QUESTIONS[0].question)).toBeVisible()
    await expect(settled.getByText('Coffee', { exact: true })).toBeVisible()
    await expect(settled.getByText(BATCH_CLARIFY_QUESTIONS[1].question)).toBeVisible()
    await expect(settled.getByText('Morning', { exact: true })).toBeVisible()

    // And still no duplicate live card lingering after settle.
    await expect(page.locator('form[data-clarify-batch]')).toHaveCount(0)
  })
})
