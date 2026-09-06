import { describe, expect, it } from 'vitest'

import { forkEn } from './fork/en'
import type { Locale } from './types'
import { zhAuthored } from './zh'

// Key-parity invariant — NARROWED TO FORK-ADDED KEYS ONLY (see fork-delta-
// inventory card t_9313aa27; do not "restore" the broader all-`en`-keys
// assertion this replaced, believing it a regression).
//
// This suite used to assert every locale catalog declares every `en` key,
// upstream keys included. Upstream edits all five locale files at once
// whenever it adds a UI string, so that assertion failed THIS FORK's build
// on UPSTREAM's schedule — for a UI the owner runs in English
// (`language: en`), where `defineLocale()` already falls back to English
// gracefully for any key a locale omits.
//
// The contract now is: only keys THIS FORK adds (declared in
// `fork/en.ts` / `fork/types.ts`) must stay translated. Those are the
// fork's own responsibility, so this still catches a real regression (a
// fork key silently losing its translation) without ever failing because
// upstream shipped new copy elsewhere in the tree.

type Tree = Record<string, unknown>

function isTree(value: unknown): value is Tree {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Flattens a translation catalog to dot-joined leaf keys (strings, functions and arrays are leaves). */
function flattenKeys(node: Tree, prefix = '', out: string[] = []): string[] {
  for (const [key, value] of Object.entries(node)) {
    const path = prefix ? `${prefix}.${key}` : key

    if (isTree(value)) {flattenKeys(value, path, out)}
    else {out.push(path)}
  }

  return out
}

const FORK_KEYS = flattenKeys(forkEn as unknown as Tree)

// Locales built with defineLocale()/defineForkLocale() merge `en` underneath
// their authored overrides, so their MERGED catalogs can never miss a key —
// a missing authored key hides as silent English fallback instead (that's
// the point: see fork/anchor.test.ts for the merge-mechanics coverage). For
// an honest drift check on the fork's OWN keys, a locale needs to expose its
// raw pre-merge authored object; this registry maps each locale that does so
// to that object. ja / zh-hant / ar declare their fork translations as a
// `TranslationOverrides` (fork/ja.ts, fork/ar.ts, fork/zh-hant.ts) and are
// allowed to omit a fork key there — an intentional, English-fallback
// translation gap, not a regression — so they are deliberately not
// registered below.
const AUTHORED: Partial<Record<Locale, Tree>> = {
  // zh keeps its full authored catalog typed as `Translations` (not
  // `TranslationOverrides`) and exports it separately from the
  // defineLocale() merge as `zhAuthored`, so its authored key set —
  // including the fork's own keys — is checkable directly rather than
  // through an always-covered merge.
  zh: zhAuthored as unknown as Tree
}

describe('desktop i18n fork-added key parity', () => {
  it('sanity: fork/en flattens to a non-trivial key set', () => {
    expect(FORK_KEYS.length).toBeGreaterThan(20)
    expect(new Set(FORK_KEYS).size).toBe(FORK_KEYS.length)
  })

  it.each(Object.keys(AUTHORED) as Locale[])('authored "%s" translations declare every fork-added key', locale => {
    const authored = AUTHORED[locale]

    if (!authored) {throw new Error(`no authored catalog registered for ${locale}`)}
    const keys = new Set(flattenKeys(authored))
    const missing = FORK_KEYS.filter(key => !keys.has(key))
    expect(missing).toEqual([])
  })
})
