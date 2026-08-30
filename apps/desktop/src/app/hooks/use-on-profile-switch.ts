import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'

/** Run `onSwitch` when the active gateway profile changes — never on first
 *  mount. For dropping per-profile view state (probes, cached usage, drafts)
 *  when the backend the app talks to swaps underneath a still-mounted view.
 *
 *  Guarded by comparing the last seen normalized profile key, not a one-shot
 *  "skip first run" flag: React Strict Mode re-invokes effects once after
 *  mount, and a first-flag treats that second pass as a real switch — it
 *  bumps every consumer's per-profile generation/epoch and wipes local drafts
 *  on every mount, which is the root cause behind the class of "Settings tab
 *  stuck on skeleton" bugs (upstream #74824). Keys are normalized with the
 *  same rule as the store-level cache invalidation in store/profile.ts, so
 *  both fire under identical conditions. */
export function useOnProfileSwitch(onSwitch: () => void): void {
  const profileKey = normalizeProfileKey(useStore($activeGatewayProfile))
  const prevProfileKey = useRef(profileKey)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (prevProfileKey.current === profileKey) {
      return
    }

    prevProfileKey.current = profileKey
    onSwitch()
    // Fire on profile change only; onSwitch identity is intentionally ignored.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileKey])
}
