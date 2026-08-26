import { providerFamilyLabel } from '@/lib/model-status-label'
import type { SessionListDensity } from '@/store/session-list-density'
import type { SessionInfo } from '@/types/hermes'

export interface SessionRowDetails {
  metadata: string
  preview: null | string
}

export interface SessionRowFormatters {
  messageCount: (count: number) => string
  toolCallCount: (count: number) => string
}

const modelLabel = (model: null | string) => model?.split('/').pop()?.trim() || null
const oneLine = (value: null | string) => value?.replace(/\s+/g, ' ').trim() || null

export const sessionRowEstimate = (density: SessionListDensity) =>
  ({ compact: 28, comfortable: 45, detailed: 63 })[density]

/** Configured-vs-served provider identity for a session row (Phase 2.13).
 *  `configured` is the primary, always-shown family label (or `null` for a
 *  legacy/unresolved session — never guessed). `served` is the secondary
 *  "via <provider>" family, populated ONLY when it differs from `configured`
 *  (case-insensitive) — the common case (they match) carries `served: null`
 *  so callers render nothing extra. */
export interface SessionRowIdentity {
  configured: string | null
  served: string | null
}

export function sessionRowIdentity(session: SessionInfo): SessionRowIdentity {
  const configured = providerFamilyLabel(session.configured_provider)
  const served = providerFamilyLabel(session.served_provider)

  if (!configured || !served) {return { configured, served: null }}

  if (configured.toLowerCase() === served.toLowerCase()) {return { configured, served: null }}

  return { configured, served }
}

export function sessionRowDetails(session: SessionInfo, fmt: SessionRowFormatters): SessionRowDetails {
  const preview = oneLine(session.preview)
  const hasOwnTitle = Boolean(session.title?.trim())

  const metadata = [
    session.git_branch?.trim() || null,
    modelLabel(session.model),
    session.message_count > 0 ? fmt.messageCount(session.message_count) : null,
    session.tool_call_count > 0 ? fmt.toolCallCount(session.tool_call_count) : null
  ]
    .filter(Boolean)
    .join(' · ')

  return {
    metadata,
    preview: hasOwnTitle ? preview : null
  }
}
