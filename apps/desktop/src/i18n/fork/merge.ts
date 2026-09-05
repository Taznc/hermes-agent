// Deep merge for fork-owned translation additions.
//
// Fork keys live under upstream-owned parents (`boot.failure.openLogsFailed`,
// `settings.gateway.…`), so the catalogs cannot be combined with a shallow
// object spread — `{ ...forkAr, ...ar }` would drop one side's whole `boot`
// branch. Merging recursively is what makes ONE anchor per locale file
// possible.
//
// The fork catalog is the FIRST argument so the upstream catalog stays the
// trailing object literal the formatter hugs: the anchor then costs a locale
// file one line at each end and leaves every translation line untouched.
//
// The parameters are deliberately concrete rather than generic: a bare generic
// would stop the trailing literal from being contextually typed, and every
// `count => …` in the catalogs would silently become `any`.
//
// Deliberately self-contained rather than reusing `define-locale.ts`'s merge:
// that one is built around `en` as the base, and importing it here would make
// `en.ts → fork/merge.ts → define-locale.ts → en.ts` circular.

import { defineLocale, type TranslationOverrides } from '../define-locale'
import type { Translations, UpstreamTranslations } from '../types'

import type { ForkTranslations } from './types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function merge(fork: unknown, base: unknown): unknown {
  if (!isRecord(fork) || !isRecord(base)) {
    return base === undefined ? fork : base
  }

  const result: Record<string, unknown> = { ...fork }

  for (const [key, value] of Object.entries(base)) {
    if (value === undefined) {
      continue
    }

    result[key] = merge(result[key], value)
  }

  return result
}

/**
 * Returns the upstream catalog with the fork-owned keys merged in, recursively.
 * The two key sets are disjoint by construction; where they overlap, the
 * upstream (second) value wins. tsc still enforces the whole `Translations`
 * contract across the pair — the upstream object must declare every upstream
 * key, the fork module every fork key.
 */
export function withForkKeys(fork: ForkTranslations, base: UpstreamTranslations): Translations {
  return merge(fork, base) as Translations
}

/**
 * `defineLocale()` for a partial locale that also ships fork-key translations:
 * merges the fork overrides under the locale's own, then falls back to English
 * for everything neither declares — identical to passing one combined override
 * object, which is what this used to be before the split.
 */
export function defineForkLocale(fork: TranslationOverrides, overrides: TranslationOverrides): Translations {
  return defineLocale(merge(fork, overrides) as TranslationOverrides)
}
