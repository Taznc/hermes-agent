/**
 * Typed access to the fork's Kanban strings (`i18n.ts`, registered under the
 * `fork-kanban` plugin id) plus upstream kanban's own column labels, so a
 * status reads the same in the answer bar as on the lane header.
 */

import { useMemo } from 'react'

import { en, KANBAN_FORK_LOCALES } from '@/fork/kanban/i18n'
import { type PluginTranslate, registerPluginLocales, translatePlugin, usePluginI18n } from '@/i18n/plugin-i18n'
import { getRuntimeI18nLocale } from '@/i18n/runtime'

export const KANBAN_FORK_I18N_ID = 'fork-kanban'

// Idempotent (registry merges per locale); module scope so the strings exist
// before the first card renders.
registerPluginLocales(KANBAN_FORK_I18N_ID, KANBAN_FORK_LOCALES)

type Bound<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => string ? (...args: A) => string : string
}

export type KanbanText = Bound<typeof en> & { colLabel: (name: string) => string }

function bind(t: PluginTranslate, kanbanT: PluginTranslate): KanbanText {
  const out: Record<string, unknown> = {}

  for (const [key, value] of Object.entries(en)) {
    out[key] = typeof value === 'function' ? (...args: unknown[]) => t(key, ...args) : t(key)
  }

  out.colLabel = (name: string) => {
    const path = `col.${name}.label`
    const label = kanbanT(path)

    // plugin-i18n returns the key itself on a miss; unknown statuses show raw.
    return label === path ? name : label
  }

  return out as KanbanText
}

export function useKanban(): KanbanText {
  const t = usePluginI18n(KANBAN_FORK_I18N_ID)
  const kanbanT = usePluginI18n('kanban')

  return useMemo(() => bind(t, kanbanT), [t, kanbanT])
}

export const columnLabel = (k: KanbanText, name: string) => k.colLabel(name)

/** One-shot fork string for non-React call sites (the switcher label, which
 *  upstream computes in render but outside any fork hook). Its host component
 *  already re-renders on a locale switch through its own i18n hooks. */
export const kanbanForkText = (key: keyof typeof en): string =>
  translatePlugin(KANBAN_FORK_I18N_ID, getRuntimeI18nLocale(), key, [])
