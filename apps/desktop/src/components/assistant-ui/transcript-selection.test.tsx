import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MarkdownTextContent } from './markdown-text'
import { hasTextSelection } from './thread/user-message'
import { UserMessageText } from './thread/user-message-text'

afterEach(() => {
  cleanup()
  window.getSelection()?.removeAllRanges()
  vi.restoreAllMocks()
})

/**
 * jsdom does not implement the browser's native "double-click selects the
 * word under the cursor" or "drag paints a Range" behaviour (there is no
 * layout engine to hit-test against), so these tests build the exact Range
 * a real double-click / drag-select produces and drive it through the same
 * Selection API + `hasTextSelection()` gate the app itself uses
 * (user-message.tsx's context-menu/expand guards, the composer drag guard in
 * hud/composer-drag.ts). What we CAN and do assert end-to-end here: the
 * rendered markdown DOM accepts a cross-node Range without throwing, the
 * app's own selection-detection helper agrees a selection is live, and no
 * console error/warning fires anywhere in the process — the two symptoms
 * from the bug report ("requires pixel-perfect precision" / "pops up with an
 * error"). The literal click-swallowing root cause (the floating pet mascot)
 * has its own DOM-level regression coverage in
 * pet/floating-pet-click-through.test.tsx, verified to fail against the
 * pre-fix source and pass against the fix.
 */

/** Find the first text node under `root` whose content includes `needle`. */
function findTextNode(root: Node, needle: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)

  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.textContent?.includes(needle)) {
      return node as Text
    }
  }

  throw new Error(`no text node containing "${needle}"`)
}

/** Select exactly `word` inside `root`, matching what a double-click on that
 *  word does in a real browser (word-boundary selection, collapsed at both
 *  ends of the word). */
function selectWord(root: Node, word: string): Selection {
  const node = findTextNode(root, word)
  const start = node.textContent!.indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)

  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)

  return selection
}

/** Select from the start of `fromWord` to the end of `toWord`, matching a
 *  click-drag range that spans everything in between — including any inline
 *  formatting boundaries the drag crosses. */
function selectRange(root: Node, fromWord: string, toWord: string): Selection {
  const startNode = findTextNode(root, fromWord)
  const endNode = findTextNode(root, toWord)
  const range = document.createRange()
  range.setStart(startNode, startNode.textContent!.indexOf(fromWord))
  range.setEnd(endNode, endNode.textContent!.indexOf(toWord) + toWord.length)

  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)

  return selection
}

/** Fire the real event sequence a browser emits for a double-click, so any
 *  listener wired to it (tapback, context-menu guard, drag-guard) runs —
 *  the console-error assertions below cover that path, not just the Range API. */
function fireDoubleClickSequence(target: Element) {
  fireEvent.pointerDown(target, { detail: 1 })
  fireEvent.mouseDown(target, { detail: 1 })
  fireEvent.pointerUp(target, { detail: 1 })
  fireEvent.mouseUp(target, { detail: 1 })
  fireEvent.click(target, { detail: 1 })
  fireEvent.pointerDown(target, { detail: 2 })
  fireEvent.mouseDown(target, { detail: 2 })
  fireEvent.pointerUp(target, { detail: 2 })
  fireEvent.mouseUp(target, { detail: 2 })
  fireEvent.doubleClick(target, { detail: 2 })
}

/** Fire the real event sequence a browser emits for a click-drag. */
function fireDragSequence(from: Element, to: Element) {
  fireEvent.pointerDown(from, { buttons: 1 })
  fireEvent.mouseDown(from, { buttons: 1 })
  fireEvent.pointerMove(to, { buttons: 1 })
  fireEvent.mouseMove(to, { buttons: 1 })
  fireEvent.pointerUp(to, {})
  fireEvent.mouseUp(to, {})
}

describe('double-click selects a single word without error', () => {
  it('selects exactly one word out of a plain assistant sentence', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const { container } = render(
      <MarkdownTextContent isRunning={false} text="The quick brown fox jumps over the lazy dog" />
    )

    const paragraph = container.querySelector('p')!
    fireDoubleClickSequence(paragraph)

    const selection = selectWord(container, 'jumps')

    expect(selection.toString()).toBe('jumps')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('selects a word sitting inside bold emphasis without leaking sibling text', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(<MarkdownTextContent isRunning={false} text="Please **carefully** review this" />)

    const bold = container.querySelector('[data-streamdown="strong"]')!
    fireDoubleClickSequence(bold)

    const selection = selectWord(container, 'carefully')

    expect(selection.toString()).toBe('carefully')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('selects a word inside inline code without error', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(<MarkdownTextContent isRunning={false} text="run `npm test` before pushing" />)

    const code = container.querySelector('code')!
    fireDoubleClickSequence(code)

    const selection = selectWord(container, 'npm')

    expect(selection.toString()).toBe('npm')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('double-clicking a plain user bubble word does not throw', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(<UserMessageText text="remember to update the changelog" />)

    fireDoubleClickSequence(container)

    const selection = selectWord(container, 'changelog')

    expect(selection.toString()).toBe('changelog')
    expect(errorSpy).not.toHaveBeenCalled()
  })
})

describe('click-drag selects a multi-word / multi-line range', () => {
  it('selects a multi-word range within one paragraph', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(
      <MarkdownTextContent isRunning={false} text="The quick brown fox jumps over the lazy dog" />
    )

    const paragraph = container.querySelector('p')!
    fireDragSequence(paragraph, paragraph)

    const selection = selectRange(container, 'quick', 'jumps')

    expect(selection.toString()).toBe('quick brown fox jumps')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('selects across two separate paragraphs (multi-line drag)', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(
      <MarkdownTextContent isRunning={false} text={'First paragraph opening line\n\nSecond paragraph closing line'} />
    )

    const paragraphs = container.querySelectorAll('p')
    expect(paragraphs).toHaveLength(2)

    fireDragSequence(paragraphs[0]!, paragraphs[1]!)

    const selection = selectRange(container, 'opening', 'closing')

    expect(selection.toString()).toContain('opening')
    expect(selection.toString()).toContain('closing')
    expect(selection.isCollapsed).toBe(false)
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('a collapsed (zero-length) selection is correctly reported as no selection', () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text="single line of text" />)

    const node = findTextNode(container, 'single')
    const range = document.createRange()
    range.setStart(node, 0)
    range.setEnd(node, 0)

    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)

    // Guards like the user bubble's context-menu/expand-clamp handlers rely on
    // this returning false for a mere caret placement (a click that didn't
    // drag), so they don't mistake "cursor parked" for "text highlighted".
    expect(hasTextSelection()).toBe(false)
  })
})

describe('selection works across mixed inline formatting (bold, links, inline code)', () => {
  const MIXED_TEXT = 'Please **check** the [docs](https://example.com/guide) and run `npm test` today'

  it('selects a range spanning plain text, bold, a link, and inline code with no error', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const { container } = render(<MarkdownTextContent isRunning={false} text={MIXED_TEXT} />)

    // Sanity: the fixture actually rendered the three inline kinds under test.
    expect(container.querySelector('[data-streamdown="strong"]')).not.toBeNull()
    expect(container.querySelector('a[href]')).not.toBeNull()
    expect(container.querySelector('code')).not.toBeNull()

    const paragraph = container.querySelector('p')!
    fireDragSequence(paragraph, paragraph)

    // Drag from inside the bold run, across the link, into the inline code —
    // exactly the node-boundary-crossing shape a real drag-select produces.
    const selection = selectRange(container, 'check', 'npm')

    expect(selection.isCollapsed).toBe(false)
    expect(selection.toString()).toContain('check')
    expect(selection.toString()).toContain('docs')
    expect(selection.toString()).toContain('npm')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('selects a word that is entirely inside a link without error', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(<MarkdownTextContent isRunning={false} text={MIXED_TEXT} />)

    const link = container.querySelector('a[href]')!
    fireDoubleClickSequence(link)

    const selection = selectWord(container, 'docs')

    expect(selection.toString()).toBe('docs')
    expect(hasTextSelection()).toBe(true)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('right-clicking a live selection does not throw (context-menu guard path)', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const { container } = render(<MarkdownTextContent isRunning={false} text={MIXED_TEXT} />)

    selectRange(container, 'check', 'npm')

    const paragraph = container.querySelector('p')!

    expect(() => fireEvent.contextMenu(paragraph)).not.toThrow()
    expect(errorSpy).not.toHaveBeenCalled()
  })
})
