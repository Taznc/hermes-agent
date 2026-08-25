import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import { en } from './en'
import type { Locale } from './types'
import { zhAuthored } from './zh'

// Key-parity invariant: every locale catalog must declare every key `en`
// declares — including keys inside plain Record<string, string> sections
// (keybinds.actions, settings.fieldLabels/fieldDescriptions, …) where a
// missing member does NOT fail tsc. A missing key there ships raw ids or
// English text to non-English users (e.g. keybind-settings falls back to
// `action.label ?? action.id`).
//
// Extra locale-only keys are allowed: open Record sections such as
// messaging.platformIntro are intentionally empty in `en` (the component
// falls back to its own English constant) and populated per-locale.

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

const EN_KEYS = flattenKeys(en as unknown as Tree)

// Discovered from the catalog (typed Record<Locale, Translations>), never a
// hardcoded list — a newly declared locale is covered automatically.
const LOCALES = Object.keys(TRANSLATIONS) as Locale[]

// Locales built with defineLocale() merge `en` underneath their authored
// overrides, so their MERGED catalogs below can never miss a key — missing
// authored keys hide as silent English fallback instead. For an honest drift
// check those locales must expose their raw authored object (pre-merge); this
// registry maps each locale that does so to that object. ja / zh-hant / ar
// keep their authored overrides inline (not exported) in files outside this
// change's scope, so today they are covered only by the merged-catalog check.
const AUTHORED: Partial<Record<Locale, Tree>> = {
  // zh keeps its full authored catalog typed as `Translations` and exported
  // separately from the defineLocale() merge, so its authored key set is
  // checkable directly.
  zh: zhAuthored as unknown as Tree
}

describe('desktop i18n locale key parity', () => {
  it('sanity: en flattens to a non-trivial key set', () => {
    expect(EN_KEYS.length).toBeGreaterThan(1000)
    expect(new Set(EN_KEYS).size).toBe(EN_KEYS.length)
  })

  it.each(LOCALES)('merged catalog "%s" declares every en key (record sections included)', locale => {
    const keys = new Set(flattenKeys(TRANSLATIONS[locale] as unknown as Tree))
    const missing = EN_KEYS.filter(key => !keys.has(key))
    expect(missing).toEqual([])
  })

  it.each(Object.keys(AUTHORED) as Locale[])('authored "%s" translations declare every en key', locale => {
    const authored = AUTHORED[locale]

    if (!authored) {throw new Error(`no authored catalog registered for ${locale}`)}
    const keys = new Set(flattenKeys(authored))
    const missing = EN_KEYS.filter(key => !keys.has(key))
    expect(missing).toEqual([])
  })
})
