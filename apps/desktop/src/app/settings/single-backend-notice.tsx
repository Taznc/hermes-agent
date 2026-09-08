import { useStore } from '@nanostores/react'

import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/external-link'
import { singleBackendHostLabel } from '@/lib/host-connections'
import { Info } from '@/lib/icons'
import { $connection } from '@/store/session'

/** Where "how do I connect more than one backend?" is actually answered. */
export const MULTI_CONNECTION_DOCS_URL =
  'https://hermes-agent.nousresearch.com/docs/user-guide/multi-connection-desktop'

/**
 * Shown on Settings → Gateways when the host owns no connection registry —
 * i.e. the browser build, which is pinned to the one backend that served it.
 *
 * The page it replaces used to render an "unavailable" empty state, which
 * reads as a bug the user might fix by retrying or reinstalling. This is the
 * honest version: the capability is absent by construction, here is the
 * backend you ARE on, and here is what would give you the missing one.
 */
export function SingleBackendNotice() {
  const { t } = useI18n()
  const g = t.settings.gateway
  const connection = useStore($connection)
  const host = singleBackendHostLabel(connection)

  return (
    <div className="flex items-start gap-2 rounded-xl border border-(--stroke-nous) bg-muted/40 px-3 py-2.5 text-[length:var(--conversation-caption-font-size)]">
      <Info className="mt-0.5 size-4 shrink-0 text-(--ui-text-tertiary)" />
      <div className="min-w-0">
        <div className="font-medium">{g.singleBackendTitle}</div>
        <p className="mt-1 leading-5 text-(--ui-text-tertiary)">
          {host ? g.singleBackendDesc(host) : g.singleBackendDescNoHost}
        </p>
        <ExternalLink className="mt-2 inline-block" href={MULTI_CONNECTION_DOCS_URL} native showExternalIcon>
          {g.singleBackendDocsLink}
        </ExternalLink>
      </div>
    </div>
  )
}
