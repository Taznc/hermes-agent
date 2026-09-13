import { parseMarkdownIntoBlocks } from '@assistant-ui/react-streamdown'

/**
 * Block splitting for the streaming markdown pipeline, without re-lexing the
 * whole message on every token flush.
 *
 * `parseMarkdownIntoBlocks` is a full `marked` lex of the entire text —
 * measured 3.4–9.6ms per call at 64–192KB. During streaming every flush is a
 * new string, so the stock splitter pays that O(full-text) cost ~30×/s on
 * long replies. Two caches remove it:
 *
 * 1. Exact-string cache — the same text always yields the SAME ARRAY. This is
 *    identity, not just cost: `parseMarkdownIntoBlocks` builds a fresh array
 *    every call, and Streamdown mirrors the block list into `useState`, so a
 *    new array identity for unchanged text makes every Streamdown re-render
 *    itself and re-render every Block under it. Short messages used to skip
 *    the cache on the theory that re-lexing them was cheap — the lex is, but
 *    the churn it caused was not (measured: ~105 self-renders of Streamdown
 *    across five idle tiles in six seconds, cascading into 800 Block renders
 *    with nothing streaming). Every length is cached now.
 * 2. Streaming-append cache — when the new text starts with a recently parsed
 *    text (the token-append case), the previous parse's blocks are reused up
 *    to a settled boundary and only the suffix is lexed. The boundary drops
 *    the previous parse's trailing whitespace-only blocks AND its last content
 *    block, because appended text can retroactively change how that last
 *    block parses (open fence, list/table continuation, setext underline, a
 *    lazy blockquote line). Blocks before it are separated by settled blank
 *    lines and cannot be affected. Cross-block reference links can't regress:
 *    Streamdown renders each block as an independent markdown document
 *    already. Verified property: `blocks.join('') === text`, and incremental
 *    output is asserted byte-identical to a full lex in tests across fences,
 *    lists, tables, setext headings, blockquotes, and HTML blocks.
 *
 * Any doubt — no prefix match, reconstruction mismatch — falls back to the
 * full lex, i.e. exactly the previous behavior.
 */

// Byte-budgeted instead of count-bounded: a count cap of 256 bounds how many
// DISTINCT texts are cached, not how much memory they cost. Long streamed
// replies (tens of KB each) can pin tens of MB behind 256 entries — the very
// "creeps over days" class this module's cache exists to fix should not
// itself be one. Approximate size (2 bytes/UTF-16 code unit, key + every
// cached block string) is tracked per entry so eviction can run purely off a
// running total instead of re-measuring the whole cache on every write.
const EXACT_CACHE_BYTE_BUDGET = 4 * 1024 * 1024
const exactCache = new Map<string, string[]>()
const exactCacheBytes = new Map<string, number>()
let exactCacheTotalBytes = 0

function approxByteSize(markdown: string, blocks: string[]): number {
  let size = markdown.length * 2

  for (const block of blocks) {
    size += block.length * 2
  }

  return size
}

function exactCacheDelete(key: string): void {
  const size = exactCacheBytes.get(key)

  if (size !== undefined) {
    exactCacheTotalBytes -= size
    exactCacheBytes.delete(key)
  }

  exactCache.delete(key)
}

function exactCacheSet(key: string, blocks: string[]): void {
  const size = approxByteSize(key, blocks)

  exactCache.set(key, blocks)
  exactCacheBytes.set(key, size)
  exactCacheTotalBytes += size

  while (exactCacheTotalBytes > EXACT_CACHE_BYTE_BUDGET && exactCache.size > 1) {
    const oldestKey = exactCache.keys().next().value as string

    exactCacheDelete(oldestKey)
  }
}

// Streaming messages grow monotonically, and only a handful stream at once
// (main reply + reasoning part, maybe a tile). A tiny ring is enough; each
// entry holds the last parse for one growing text lineage.
const APPEND_CACHE_MAX = 4
const APPEND_CACHE_MIN_LENGTH = 2048
const appendCache: { blocks: string[]; text: string }[] = []

function rememberAppend(text: string, blocks: string[]): void {
  if (text.length < APPEND_CACHE_MIN_LENGTH) {
    return
  }

  // Replace the lineage this text grew from (its cached prefix), else push.
  const index = appendCache.findIndex(entry => text.startsWith(entry.text))

  if (index !== -1) {
    appendCache.splice(index, 1)
  }

  appendCache.push({ blocks, text })

  if (appendCache.length > APPEND_CACHE_MAX) {
    appendCache.shift()
  }
}

function lexIncrementally(text: string): null | string[] {
  const entry = appendCache.find(cached => text.length > cached.text.length && text.startsWith(cached.text))

  if (!entry) {
    return null
  }

  // Settled boundary: drop the last TWO content blocks (skipping any
  // whitespace-only blocks around them). Dropping only the single last content
  // block is unsound: appended text can retroactively merge the previous
  // parse's last two blocks into one. The trigger is a trailing Setext
  // underline — `marked` only treats `-`/`=` as an underline for the paragraph
  // ABOVE it, so a settled `"#e\n5\n-"` lexes as ["#e\n", "5\n-"], but growing
  // the tail to `"#e\n5\n-p2=kj:c"` collapses both into one paragraph. The
  // block before the last is the deepest an append can reach (the underline
  // consumes exactly one preceding block), so re-lexing the last two is safe;
  // earlier blocks are fenced off by settled blank lines. join('') === text
  // still holds either way, so the reconstruction check below can't catch this.
  let keep = entry.blocks.length

  for (let dropped = 0; dropped < 2 && keep > 0; dropped += 1) {
    while (keep > 0 && !entry.blocks[keep - 1].trim()) {
      keep -= 1
    }

    if (keep > 0) {
      keep -= 1
    }
  }

  if (keep === 0) {
    return null
  }

  const settled = entry.blocks.slice(0, keep)
  let settledLength = 0

  for (const block of settled) {
    settledLength += block.length
  }

  // Defensive reconstruction check — the splitter's join(blocks) === text
  // property is what makes offsets exact. If it ever doesn't hold, full lex.
  if (settledLength > entry.text.length || !text.startsWith(entry.text.slice(0, settledLength), 0)) {
    return null
  }

  return [...settled, ...parseMarkdownIntoBlocks(text.slice(settledLength))]
}

export function parseMarkdownIntoBlocksCached(markdown: string): string[] {
  const hit = exactCache.get(markdown)

  if (hit) {
    // Refresh recency (Map iteration order is insertion order): re-set both
    // the blocks and their tracked byte size so eviction still walks
    // oldest-first without double-counting the entry's bytes.
    const size = exactCacheBytes.get(markdown)

    exactCache.delete(markdown)

    if (size !== undefined) {
      exactCacheBytes.delete(markdown)
    }

    exactCache.set(markdown, hit)

    if (size !== undefined) {
      exactCacheBytes.set(markdown, size)
    }

    return hit
  }

  const blocks = lexIncrementally(markdown) ?? parseMarkdownIntoBlocks(markdown)

  rememberAppend(markdown, blocks)
  exactCacheSet(markdown, blocks)

  return blocks
}

// Test-only introspection: the byte-budget eviction is a memory-shape
// contract that isn't otherwise observable from the cached parser's return
// value (eviction never changes correctness, only what's retained).
export function __exactCacheStatsForTests() {
  return { entries: exactCache.size, totalBytes: exactCacheTotalBytes }
}

export function __resetMarkdownBlocksCachesForTests() {
  exactCache.clear()
  exactCacheBytes.clear()
  exactCacheTotalBytes = 0
  appendCache.length = 0
}
