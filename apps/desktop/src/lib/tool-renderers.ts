import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import type { ReactNode } from 'react'

import type { Contribution } from '@/contrib/types'

/**
 * TOOL RENDERERS — a plugin's seam to own how ONE tool's card renders in the
 * transcript.
 *
 * Core renders every tool call through a hardcoded chain keyed on `toolName`
 * (`clarify`, `setup_mcp`, `delegate_task`, `image_generate`, a handful of
 * presentation-only cases, ... falling through to the generic `ToolFallback`
 * row) — see `message-parts.tsx`. That chain is exactly what still runs when
 * nobody claims a name: this area only lets a plugin swap ONE tool's whole
 * card for its own, never touch another tool's row or the surrounding
 * transcript chrome.
 *
 * Precedence is LAST REGISTRATION WINS for a duplicate `toolName` — a
 * deliberate, tested contract (unlike `chat.empty`'s mount-everyone or
 * `thread.activity`'s first-wins): the most recently resolved claim on a
 * name is trusted over an earlier one, so re-registering (a plugin reload,
 * a hot update) takes effect without first removing the old registration.
 */
export const TOOL_RENDERERS_AREA = 'toolRenderers'

/** Payload of a `toolRenderers` data contribution. */
export interface ToolRendererContribution {
  /** The exact tool name this contribution claims (e.g. `'clarify'`). */
  toolName: string
  /** Renders the tool's whole card. Mounted inside an error boundary that
   *  degrades to the CORE renderer (not a generic error card) on a throw —
   *  a broken plugin renderer costs one tool's card, never the transcript. */
  render: (props: ToolCallMessagePartProps) => ReactNode
}

/** One resolved renderer: the winning contribution's id (used to label its
 *  error boundary) plus its render function. */
export interface ResolvedToolRenderer {
  id: string
  render: ToolRendererContribution['render']
}

/**
 * Resolve the `toolRenderers` claim for `toolName`, or `undefined` when
 * nobody claims it. `contributions` is the area's already-resolved,
 * sorted-and-filtered snapshot (`registry.getArea` / `useContributions`
 * order: ascending `order`, ties in registration order) — the LAST entry in
 * that order matching `toolName` wins, so a plugin registered later (or
 * given a higher `order`) supersedes an earlier claim on the same name.
 */
export function resolveToolRenderer(
  contributions: readonly Contribution[],
  toolName: string
): ResolvedToolRenderer | undefined {
  let match: ResolvedToolRenderer | undefined

  for (const contribution of contributions) {
    const data = contribution.data as ToolRendererContribution | undefined

    if (data?.toolName === toolName && typeof data.render === 'function') {
      match = { id: contribution.id, render: data.render }
    }
  }

  return match
}
