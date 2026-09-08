import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ComposerTriggerPopover } from './trigger-popover'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      composer: {
        lookupLoading: 'Loading…',
        lookupNoMatches: 'No matches',
        lookupTry: 'Try',
        lookupOr: 'or'
      }
    }
  })
}))

/** A real bundled skill description; skill frontmatter caps these at 60 chars,
 *  which is already far past what a 20rem row can show beside the name. */
const LONG_DESCRIPTION =
  'Parallel 4-agent cleanup of recent code changes across the tree, ' +
  'reviewing every diff hunk for dead code, duplication and naming drift ' +
  'before it reaches review.'

function slashItem(command: string, meta: string, group = 'Skills') {
  return {
    id: `${command}|0`,
    type: 'slash',
    label: command.slice(1),
    metadata: { command, display: command, meta, group, action: '', rawText: command }
  }
}

const noop = () => {}

function popover(items: ReturnType<typeof slashItem>[], activeIndex = 0) {
  return (
    <ComposerTriggerPopover
      activeIndex={activeIndex}
      items={items}
      kind="/"
      loading={false}
      onHover={noop}
      onPick={noop}
    />
  )
}

function detail(root: HTMLElement) {
  return root.querySelector('[data-slot="composer-completion-detail"]') as HTMLElement | null
}

afterEach(() => {
  cleanup()
})

describe('the highlighted row description is readable in full', () => {
  it('shows the complete description with no truncation marker in the detail area', () => {
    const { container } = render(popover([slashItem('/simplify-code', LONG_DESCRIPTION)]))

    const footer = detail(container)

    expect(footer).toBeTruthy()
    expect(footer?.textContent).toBe(LONG_DESCRIPTION)

    // The row itself still ellipsizes — the footer is what carries the text,
    // so nothing inside it may clip.
    const text = footer?.querySelector('p') as HTMLElement

    expect(text.className).not.toContain('truncate')
    expect(text.className).toContain('break-words')
    expect(footer?.textContent).not.toContain('…')
    expect(footer?.textContent).not.toMatch(/\.\.\.$/)
  })

  it('wraps within the panel instead of overflowing a narrow width', () => {
    const { container } = render(popover([slashItem('/simplify-code', LONG_DESCRIPTION)]))
    const text = detail(container)?.querySelector('p') as HTMLElement

    // Wrapping, not horizontal overflow: the block is width-constrained by the
    // panel, breaks long words, and is capped at three lines rather than
    // growing the panel without bound.
    expect(text.className).toContain('line-clamp-3')
    expect(text.className).toContain('break-words')
    expect(text.className).not.toContain('whitespace-nowrap')
    expect(text.className).not.toContain('overflow-x')

    const footer = detail(container) as HTMLElement

    expect(footer.className).toContain('overflow-hidden')
  })

  it('follows the highlighted index as the user arrows through the list', () => {
    const items = [
      slashItem('/first', 'The first skill, described at length for the footer.'),
      slashItem('/second', 'The second skill, with an entirely different blurb.'),
      slashItem('/third', 'The third skill and its own distinct description.')
    ]

    const { container, rerender } = render(popover(items, 0))

    expect(detail(container)?.textContent).toBe(items[0].metadata.meta)

    rerender(popover(items, 1))
    expect(detail(container)?.textContent).toBe(items[1].metadata.meta)

    rerender(popover(items, 2))
    expect(detail(container)?.textContent).toBe(items[2].metadata.meta)

    // Selection and rows are untouched by the footer.
    expect((container.querySelector('[data-highlighted]') as HTMLElement).textContent).toContain('/third')
    expect(screen.getAllByRole('button')).toHaveLength(3)
  })

  it('reserves the footer height so arrowing does not resize the panel', () => {
    const items = [
      slashItem('/short', 'Short.'),
      slashItem('/long', LONG_DESCRIPTION),
      // A row with no description of its own must not collapse the reserved
      // area — that is exactly the per-keypress jitter the footer avoids.
      slashItem('/blank', '')
    ]

    const { container, rerender } = render(popover(items, 0))
    const heightClass = (detail(container) as HTMLElement).className.match(/h-\[[^\]]+\]/)?.[0]

    expect(heightClass).toBeTruthy()

    for (const index of [1, 2]) {
      rerender(popover(items, index))
      const footer = detail(container) as HTMLElement

      expect(footer).toBeTruthy()
      expect(footer.className).toContain(heightClass as string)
    }

    // The row without a description leaves the reserved block empty rather
    // than rendering a stray separator or blank paragraph.
    expect(detail(container)?.textContent).toBe('')
    expect(detail(container)?.querySelector('p')).toBeNull()
  })

  it('omits the footer entirely when no row in the list has a description', () => {
    const { container } = render(popover([slashItem('/bare', ''), slashItem('/plain', '')]))

    expect(detail(container)).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })

  it('renders without a footer for an empty list and for emoji rows', () => {
    const { container, rerender } = render(popover([], 0))

    expect(detail(container)).toBeNull()

    rerender(
      <ComposerTriggerPopover
        activeIndex={0}
        items={[{ id: ':joy:|0', type: 'emoji', label: '😂  :joy:', metadata: { display: '😂  :joy:' } }]}
        kind=":"
        loading={false}
        onHover={noop}
        onPick={noop}
      />
    )

    // The emoji IS its own label; there is no second field to spill into a
    // footer, so no space is reserved for one.
    expect(detail(container)).toBeNull()
  })

  it('falls back to the item description when metadata carries no meta', () => {
    const { container } = render(
      <ComposerTriggerPopover
        activeIndex={0}
        items={[
          {
            id: '@file:src/main.tsx|0',
            type: 'file',
            label: 'src/main.tsx',
            description: LONG_DESCRIPTION,
            metadata: { display: 'src/main.tsx', rawText: '@file:src/main.tsx' }
          }
        ]}
        kind="@"
        loading={false}
        onHover={noop}
        onPick={noop}
      />
    )

    expect(detail(container)?.textContent).toBe(LONG_DESCRIPTION)
  })

  it('keeps the list scrollable and the footer pinned outside it', () => {
    const { container } = render(popover([slashItem('/simplify-code', LONG_DESCRIPTION)]))
    const panel = container.querySelector('[data-slot="composer-completion-drawer"]') as HTMLElement
    const list = container.querySelector('[data-slot="composer-completion-list"]') as HTMLElement
    const footer = detail(container) as HTMLElement

    // The rows scroll; the description block does not scroll away with them.
    expect(list.className).toContain('overflow-y-auto')
    expect(list.getAttribute('role')).toBe('listbox')
    expect(list.contains(footer)).toBe(false)
    expect(panel.contains(footer)).toBe(true)
    expect(panel.className).toContain('overflow-hidden')
    expect(panel.className).not.toContain('overflow-y-auto')
  })
})
