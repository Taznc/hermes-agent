import { useEffect } from 'react'

import { refreshActiveProfile } from '@/store/profile'

/**
 * Re-pull the running profile + list on mount, and again whenever the window
 * regains focus/visibility -- a profile created, deleted, or renamed by
 * another surface (Manage Profiles, another window, the CLI) leaves the
 * rail's cached $profiles stale until something re-fetches it. Without this,
 * a deleted profile's square lingers in the rail until the user happens to
 * open Manage Profiles (whose own refresh() call was the only other reader).
 *
 * Cheap and best-effort, matching the focus/visibilitychange refresh pattern
 * used elsewhere in the sidebar (see refreshProjects/refreshProjectTree).
 * Extracted into its own hook (rather than left inline in ProfileRail) so the
 * focus/visibility wiring is unit-testable without rendering the whole rail.
 *
 * `scopeKey` identifies the backend the rail is currently pointed at (active
 * gateway connection + profile). Switching connection or profile is a soft
 * re-home that deliberately keeps ProfileRail mounted, so a mount-only fetch
 * would leave the rail showing the PREVIOUS machine's profiles forever
 * (#85731 -- "profile rail never updates when connecting to a remote"). Keying
 * the effect on it re-enumerates against whichever backend is now live.
 * Optional so callers that only need the focus/visibility behaviour (and the
 * hook's own tests) can omit it.
 */
export function useProfileRailRefreshOnActive(scopeKey?: string): void {
  useEffect(() => {
    void refreshActiveProfile()

    const onActive = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      void refreshActiveProfile()
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [scopeKey])
}
