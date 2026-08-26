import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// Static-analysis guard for the danger/error color vocabulary (same category as
// no-native-title.test.ts — an ESLint-shaped rule expressed as a vitest).
//
// History: error text once used `text-(--ui-danger,#f87171)` — but `--ui-danger`
// was never defined in any theme, so the raw dark-theme hex fallback always
// rendered, on light themes too. The canonical danger vocabulary is Tailwind's
// `text-destructive` (→ `--color-destructive` → `--dt-destructive`), which is
// defined in the styles.css base theme and re-injected per theme preset by
// src/themes/context.tsx. This guard keeps the vocabulary converged:
//
//  1. The canonical token chain must stay defined (styles.css base value,
//     Tailwind @theme mapping, and the per-theme JS injection).
//  2. Nothing may reference the phantom `--ui-danger` again.
//  3. No className may pair a parenthesized custom-property utility with a raw
//     hex fallback (`text-(--x,#hex)`) — that pattern silently masks undefined
//     tokens, which is exactly how this bug shipped.
//
// Scope (kept honest): this does NOT validate every custom property used in the
// renderer. Some tokens are runtime-injected (e.g. `--ui-success` from
// src/themes/context.tsx) and some legacy references are known phantoms outside
// the danger vocabulary; a fully generic reference-vs-definition audit is a
// separate cleanup. This guard covers the danger token and the literal-fallback
// masking pattern only.

const srcDir = resolve(__dirname, '../../..')

// Recursively walk a directory and collect all .ts/.tsx file paths.
function collectSourceFiles(dir: string): string[] {
  const results: string[] = []

  for (const entry of readdirSync(dir)) {
    // Skip node_modules, dist, and __tests__ (this file itself)
    if (entry === 'node_modules' || entry === 'dist' || entry === '__tests__') {
      continue
    }

    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)

    if (stat.isDirectory()) {
      results.push(...collectSourceFiles(fullPath))
    } else if (entry.endsWith('.ts') || entry.endsWith('.tsx')) {
      results.push(fullPath)
    }
  }

  return results
}

describe('danger color token', () => {
  it('keeps the canonical destructive token chain defined per theme', () => {
    const styles = readFileSync(join(srcDir, 'styles.css'), 'utf-8')
    const themeContext = readFileSync(join(srcDir, 'themes/context.tsx'), 'utf-8')

    // Base value every theme inherits unless it overrides.
    expect(styles).toMatch(/--dt-destructive:\s*\S/)
    expect(styles).toMatch(/--dt-destructive-foreground:\s*\S/)
    // Tailwind @theme mapping that makes `text-destructive` resolve to it.
    expect(styles).toMatch(/--color-destructive:\s*var\(--dt-destructive\)/)
    // Per-theme injection so JS theme presets carry their own destructive color.
    expect(themeContext).toContain("'--dt-destructive'")
  })

  it('never references the phantom --ui-danger and never hex-falls-back a token utility', () => {
    const violations: string[] = []

    // A parenthesized custom-property utility with a raw hex fallback, e.g.
    // `text-(--ui-danger,#f87171)`. The `-\(` requires a utility prefix, so
    // plain CSS `var(--x, #hex)` fallbacks in style strings are out of scope.
    const hexFallbackPattern = /[a-zA-Z][\w-]*-\(--[\w-]+\s*,\s*#[0-9a-fA-F]{3,8}\)/g

    for (const filePath of collectSourceFiles(srcDir)) {
      const content = readFileSync(filePath, 'utf-8')
      const relativePath = filePath.replace(srcDir + '/', '')

      const phantomIndex = content.indexOf('--ui-danger')

      if (phantomIndex !== -1) {
        const lineNum = content.slice(0, phantomIndex).split('\n').length
        violations.push(
          `${relativePath}:${lineNum} references --ui-danger (undefined in every theme) — use text-destructive`
        )
      }

      let match: RegExpExecArray | null

      while ((match = hexFallbackPattern.exec(content)) !== null) {
        const lineNum = content.slice(0, match.index).split('\n').length
        violations.push(
          `${relativePath}:${lineNum} \`${match[0]}\` hardcodes a hex fallback that masks an undefined token — use the defined token with no literal fallback`
        )
      }
    }

    expect(violations, violations.join('\n')).toEqual([])
  })
})
