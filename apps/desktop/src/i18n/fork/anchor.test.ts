import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from '../catalog'
import { en } from '../en'
import type { Locale } from '../types'
import { zhAuthored } from '../zh'

import { forkEn } from './en'
import { defineForkLocale, withForkKeys } from './merge'

// The anchor contract: fork-added keys live in ./fork/ and reach every locale
// through ONE call per catalog file. These tests pin the two ways that can
// silently break — a fork key that stops arriving in a locale (users see a raw
// key or English where a translation exists), and a merge that clobbers an
// upstream branch instead of merging into it (the reason a plain spread is not
// enough: fork keys sit UNDER upstream-owned parents).

type Tree = Record<string, unknown>

function isTree(value: unknown): value is Tree {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function flattenKeys(node: Tree, prefix = '', out: string[] = []): string[] {
  for (const [key, value] of Object.entries(node)) {
    const path = prefix ? `${prefix}.${key}` : key

    if (isTree(value)) {
      flattenKeys(value, path, out)
    } else {
      out.push(path)
    }
  }

  return out
}

const FORK_KEYS = flattenKeys(forkEn as unknown as Tree)
const LOCALES = Object.keys(TRANSLATIONS) as Locale[]

describe('fork i18n anchor', () => {
  it('declares a non-trivial fork key set', () => {
    expect(FORK_KEYS.length).toBeGreaterThan(20)
    expect(new Set(FORK_KEYS).size).toBe(FORK_KEYS.length)
  })

  it('merges fork keys into en without dropping upstream siblings', () => {
    const merged = en as unknown as Tree
    const enKeys = new Set(flattenKeys(merged))

    for (const key of FORK_KEYS) {
      expect(enKeys.has(key)).toBe(true)
    }

    // The branch a shallow spread would have replaced wholesale: `boot` is
    // upstream-owned, the fork only adds leaves inside it, so the merged branch
    // must still carry the upstream leaves alongside the fork ones.
    const forkBootKeys = flattenKeys((forkEn as unknown as Tree).boot as Tree)
    const mergedBootKeys = flattenKeys(merged.boot as Tree)

    expect(mergedBootKeys.length).toBeGreaterThan(forkBootKeys.length)
    expect(mergedBootKeys).toContain('failure.openLogsFailed')
    expect(mergedBootKeys).toContain('failure.openLogs')
  })

  it.each(LOCALES)('locale "%s" carries every fork key', locale => {
    const keys = new Set(flattenKeys(TRANSLATIONS[locale] as unknown as Tree))
    const missing = FORK_KEYS.filter(key => !keys.has(key))

    expect(missing).toEqual([])
  })

  it('keeps the zh authored catalog complete, including fork keys', () => {
    const keys = new Set(flattenKeys(zhAuthored as unknown as Tree))
    const missing = FORK_KEYS.filter(key => !keys.has(key))

    expect(missing).toEqual([])
  })

  it('withForkKeys merges nested branches and lets the upstream value win a collision', () => {
    const fork = { a: { forkOnly: 'fork' }, shared: 'fork' }
    const base = { a: { upstreamOnly: 'upstream' }, shared: 'upstream' }
    const merged = withForkKeys(fork as never, base as never) as unknown as Tree

    expect(merged).toEqual({ a: { forkOnly: 'fork', upstreamOnly: 'upstream' }, shared: 'upstream' })
  })

  it('defineForkLocale still falls back to English for keys neither side declares', () => {
    const locale = defineForkLocale({ errors: { openLogsFailed: 'FORK' } }, { common: { save: 'LOCALE' } })

    expect(locale.errors.openLogsFailed).toBe('FORK')
    expect(locale.common.save).toBe('LOCALE')
    expect(locale.common.cancel).toBe(en.common.cancel)
  })
})
