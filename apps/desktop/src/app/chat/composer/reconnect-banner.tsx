import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $gatewayState } from '@/store/session'
import { $catchingUpSessionIds, $turnLostSessionIds, dismissTurnLost } from '@/store/session-states'

/**
 * Reconnect UX (audit MEDIUM): the socket-level toast in use-gateway-boot.ts
 * only fires after ~25s of failed retries, which is correct for "don't cry
 * wolf on a 2s Wi-Fi blip" but leaves the composer with NO cue for that whole
 * window beyond the tiny statusbar dot. This banner is the fast, non-modal
 * layer: it renders the instant `$gatewayState` leaves `open`, same signal
 * `gateway-connecting-overlay.tsx` uses for cold boot but scoped to the
 * post-boot composer instead of a full-screen takeover. Composer stays
 * editable throughout — see use-composer-placeholder.ts's own reconnecting
 * copy, which this banner complements rather than replaces.
 *
 * Independently, once the socket reopens, a session that was busy right
 * before the drop may have had its busy flag force-cleared by
 * reconcileBusyStatesOnReconnect (its runtime id died with the old
 * connection). That is indistinguishable from "the turn actually finished"
 * without a signal, so session-states.ts tracks it explicitly: catching-up
 * while the grace window runs, turn-lost if nothing re-asserts busy before it
 * expires. Rendered here, scoped to THIS composer's session (stored id), so a
 * tile's banner never shows another tile's reconnect story.
 */
export function ReconnectStatusBanner({
  onReload,
  storedSessionId
}: {
  onReload?: (parentId: string | null) => Promise<void>
  storedSessionId?: null | string
}) {
  const { t } = useI18n()
  const gatewayState = useStore($gatewayState)
  const reconnecting = gatewayState === 'closed' || gatewayState === 'error'
  const catchingUp = useStore($catchingUpSessionIds)
  const turnLost = useStore($turnLostSessionIds)

  const isCatchingUp = Boolean(storedSessionId) && catchingUp.includes(storedSessionId!)
  const isTurnLost = Boolean(storedSessionId) && turnLost.includes(storedSessionId!)

  // Priority: turn-lost (needs a decision) > catching-up (transient, no
  // action) > plain reconnecting (no session-specific claim at all). Only one
  // renders — stacking all three would repeat the same "socket is down" fact.
  if (isTurnLost) {
    return (
      <div
        className="flex items-center justify-between gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[0.72rem] text-amber-700 dark:text-amber-300"
        data-slot="composer-turn-lost-banner"
        role="status"
      >
        <span className="min-w-0 truncate">{t.composer.turnLostNotice}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          {onReload && (
            <Button
              className="h-6 rounded-md px-2 text-[0.68rem]"
              onClick={() => void onReload(null)}
              size="xs"
              type="button"
              variant="outline"
            >
              {t.composer.turnLostRegenerate}
            </Button>
          )}
          <Button
            aria-label={t.common.close}
            className="h-6 rounded-md px-1.5 text-[0.68rem]"
            onClick={() => dismissTurnLost(storedSessionId)}
            size="xs"
            type="button"
            variant="ghost"
          >
            {t.common.close}
          </Button>
        </div>
      </div>
    )
  }

  if (isCatchingUp) {
    return (
      <div
        className={cn(
          'flex items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--dt-composer-ring)_32%,transparent)] bg-accent/12 px-2.5 py-1.5 text-[0.72rem] text-muted-foreground'
        )}
        data-slot="composer-catching-up-banner"
        role="status"
      >
        <Loader2 className="size-3 shrink-0 animate-spin" />
        <span className="min-w-0 truncate">{t.composer.catchingUpNotice}</span>
      </div>
    )
  }

  if (reconnecting) {
    return (
      <div
        className="flex items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--dt-composer-ring)_32%,transparent)] bg-accent/12 px-2.5 py-1.5 text-[0.72rem] text-muted-foreground"
        data-slot="composer-reconnecting-banner"
        role="status"
      >
        <Loader2 className="size-3 shrink-0 animate-spin" />
        <span className="min-w-0 truncate">{t.composer.reconnectingBanner}</span>
      </div>
    )
  }

  return null
}
