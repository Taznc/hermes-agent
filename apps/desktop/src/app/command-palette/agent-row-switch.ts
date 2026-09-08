import { selectConnection } from '@/store/connections'
import { notifyError } from '@/store/notifications'

import type { AgentPaletteRow } from './agent-rows'

/**
 * Re-home the window onto the agent a palette row names.
 *
 * The palette is one of three switching doors (the sidebar gateway selector and
 * the fleet rail's at-rest squares are the others) and they must all commit the
 * same way. `selectConnection` is that commit: dial the target WITHOUT
 * activating it, then — inside the activation's serialized section — raise the
 * barrier, reset the machine context and wipe the previous backend's session
 * bindings, and only then publish. Calling `ensureGatewayAgent` from here
 * instead (as this row did) skips all of it, so the new backend is published
 * while $activeSessionId still names a runtime the PREVIOUS backend minted and
 * the next RPC comes back "session not found" — the #93937 class, fixed
 * upstream by routing every cross-source switch through this one door.
 *
 * Rows carry the registry connection id verbatim (`local` for this device), so
 * there is no sentinel to translate: the registry resolves every source the
 * same way.
 *
 * `switchFailed` is the caller's already-localized message formatter (the same
 * `t.profiles.switchConnectionFailed` the sidebar selector uses), passed in so
 * this module stays i18n-free like the row builder beside it.
 */
export function switchToAgentRow(row: AgentPaletteRow, switchFailed: (device: string) => string): void {
  void selectConnection(row.connectionId, { profile: row.profile }).catch((error: unknown) =>
    notifyError(error, switchFailed(row.device))
  )
}
