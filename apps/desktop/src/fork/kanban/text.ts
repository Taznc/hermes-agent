/**
 * Typed access to the fork's Kanban strings (`i18n.ts`, registered under the
 * `kanban-fork` plugin id) plus upstream kanban's own column labels, so a
 * status reads the same in the answer bar as on the lane header.
 */

import { useMemo } from 'react'

import { type PluginTranslate, registerPluginLocales, usePluginI18n } from '@/i18n/plugin-i18n'

import { en, KANBAN_FORK_LOCALES } from './i18n'

export const KANBAN_FORK_I18N_ID = 'kanban-fork'

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
