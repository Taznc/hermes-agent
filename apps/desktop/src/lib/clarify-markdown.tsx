import { Fragment, type ReactNode } from 'react'

import { cn } from '@/lib/utils'

// Clarify (and clarify.explain "Why?"/"Ask") text renders the bare minimum of
// markdown: **bold**, `code`, paragraphs, and `-`/`1.` lists. Same reasoning
// as UserMessageText — a clarify card mounts many small text fragments at
// once (the question, every choice, notes, help answers), often several per
// screen in a batch, so the full Streamdown pipeline (KaTeX, syntax
// highlighting, artifact detection) is the wrong tool: it's built for one
// long assistant answer, not dozens of short repeated fragments. This covers
// what tools/clarify_tool.py and the explain path actually emit.

interface Block {
  readonly items: readonly string[]
  readonly kind: 'ol' | 'p' | 'ul'
}

const BULLET_RE = /^[-*]\s+(.*)$/
const ORDERED_RE = /^\d+[.)]\s+(.*)$/

// Models frequently write "Option 1 — ... Option 2 — ..." as one run-on
// sentence with no blank line or list marker between the two — readable as
// markdown source, but a wall of text once rendered. Force each subsequent
// occurrence onto its own paragraph (a blank-line break, not just a line
// break — `splitBlocks` only starts a new block on a blank line) so it gets
// the same treatment a blank-line-separated question already gets. Only
// mid-text occurrences break (the lookbehind requires preceding non-newline
// content) — the first "Option 1" stays put.
const INLINE_ENUM_HEADER_RE = /(?<=\S)[ \t]+(?=(?:\*\*)?(?:Option|Choice)\s+\d+(?:\*\*)?\s*[:\-–—])/g

function breakInlineEnumHeaders(text: string): string {
  return text.replace(INLINE_ENUM_HEADER_RE, '\n\n')
}

/** Split text into paragraph/list blocks. A block boundary is a blank line or
 *  a switch into/out of a bulleted or numbered line — so a model that writes
 *  "Option 1 — ...\nOption 2 — ..." (one option per line, no blank line
 *  between) still renders each option on its own line instead of one run-on
 *  paragraph swallowing the newlines. */
function splitBlocks(rawText: string): Block[] {
  const lines = breakInlineEnumHeaders(rawText.replace(/\r\n/g, '\n')).split('\n')
  const blocks: Block[] = []
  let paragraph: string[] = []

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ items: paragraph, kind: 'p' })
      paragraph = []
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trim()

    if (!line) {
      flushParagraph()

      continue
    }

    const bullet = line.match(BULLET_RE)

    if (bullet) {
      flushParagraph()
      const last = blocks.at(-1)

      if (last?.kind === 'ul') {
        blocks[blocks.length - 1] = { items: [...last.items, bullet[1]], kind: 'ul' }
      } else {
        blocks.push({ items: [bullet[1]], kind: 'ul' })
      }

      continue
    }

    const ordered = line.match(ORDERED_RE)

    if (ordered) {
      flushParagraph()
      const last = blocks.at(-1)

      if (last?.kind === 'ol') {
        blocks[blocks.length - 1] = { items: [...last.items, ordered[1]], kind: 'ol' }
      } else {
        blocks.push({ items: [ordered[1]], kind: 'ol' })
      }

      continue
    }

    // A non-list line right after list items starts a new paragraph rather
    // than joining the list's last item.
    if (blocks.at(-1)?.kind === 'ul' || blocks.at(-1)?.kind === 'ol') {
      flushParagraph()
    }

    paragraph.push(line)
  }

  flushParagraph()

  return blocks
}

const INLINE_RE = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g

/** Render `**bold**` and `` `code` `` spans inline; everything else passes
 *  through as plain text. Exported so single-line contexts (a choice label)
 *  can use it without the block/paragraph machinery below. */
export function renderClarifyInline(text: string, keyPrefix = 'inline'): ReactNode[] {
  return text.split(INLINE_RE).map((part, index) => {
    const key = `${keyPrefix}-${index}`

    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return <strong key={key}>{part.slice(2, -2)}</strong>
    }

    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return (
        <code
          className="mx-px rounded bg-[color-mix(in_srgb,currentColor_8%,transparent)] px-1 py-px font-mono text-[0.92em]"
          key={key}
        >
          {part.slice(1, -1)}
        </code>
      )
    }

    return <Fragment key={key}>{part}</Fragment>
  })
}

const LIST_CLASS = 'grid list-outside gap-0.5 ps-4'

/** Render clarify/explain prose: paragraphs and lists, each line running
 *  through `renderClarifyInline`. Multiple paragraphs/list blocks get
 *  visible vertical spacing so "Option 1 / Option 2" reads as distinct
 *  lines instead of one dense run of text. */
export function ClarifyMarkdown({ className, text }: { className?: string; text: string }) {
  const blocks = splitBlocks(text)

  if (blocks.length === 0) {
    return null
  }

  return (
    <div className={cn('grid gap-1.5', className)}>
      {blocks.map((block, blockIndex) => {
        const key = `block-${blockIndex}`

        if (block.kind === 'p') {
          return (
            <p key={key}>
              {block.items.map((line, lineIndex) => (
                <Fragment key={`${key}-${lineIndex}`}>
                  {lineIndex > 0 ? <br /> : null}
                  {renderClarifyInline(line, `${key}-${lineIndex}`)}
                </Fragment>
              ))}
            </p>
          )
        }

        const ListTag = block.kind === 'ul' ? 'ul' : 'ol'

        return (
          <ListTag className={cn(LIST_CLASS, block.kind === 'ul' ? 'list-disc' : 'list-decimal')} key={key}>
            {block.items.map((item, itemIndex) => (
              <li key={`${key}-${itemIndex}`}>{renderClarifyInline(item, `${key}-${itemIndex}`)}</li>
            ))}
          </ListTag>
        )
      })}
    </div>
  )
}
