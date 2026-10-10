// fork/env-lockdown.ts — the installed (packaged) fork app ignores environment
// overrides that would change which Hermes checkout, runtime, data directory or
// backend it uses — and therefore which repository/branch it updates from.
//
// Why: the Desktop Update button updates the checkout at $HERMES_HOME/hermes-agent
// (or $HERMES_DESKTOP_HERMES_ROOT). A stray `launchctl setenv`, shell profile or
// parent process could point the app at another install whose updates are not
// pinned to the fork (hermes_fork/update_pin.py). The pin itself lives in the
// checkout's git config; this module makes sure the packaged app always opens
// THE checkout that carries it.
//
// Dev runs (`npm run dev`, not packaged) keep every override: developers rely
// on HERMES_DESKTOP_HERMES_ROOT / HERMES_HOME to point at worktrees.

export const LOCKED_DESKTOP_ENV: readonly string[] = Object.freeze([
  // which Hermes home, and so which checkout ($HERMES_HOME/hermes-agent) and config
  'HERMES_HOME',
  // which checkout / runtime / renderer the app runs and updates
  'HERMES_DESKTOP_HERMES_ROOT',
  'HERMES_DESKTOP_HERMES',
  'HERMES_DESKTOP_PYTHON',
  'HERMES_DESKTOP_WEB_DIST',
  // which userData dir (holds updates.json and connections) / home fallback
  'HERMES_DESKTOP_USER_DATA_DIR',
  'HERMES_DATA_DIR_SUFFIX',
  // a remote backend replaces the local, pinned one
  'HERMES_DESKTOP_REMOTE_URL',
  'HERMES_DESKTOP_REMOTE_TOKEN',
  // fakes "packaged" for a dev run; meaningless and misleading in a real one
  'HERMES_DESKTOP_IS_PACKAGED'
])

/**
 * Remove the locked overrides from `env` (in place) when running packaged.
 * Returns the names that were removed, for the log.
 */
export function lockDesktopEnv(env: NodeJS.ProcessEnv, isPackaged: boolean): string[] {
  if (!isPackaged) {
    return []
  }

  const removed: string[] = LOCKED_DESKTOP_ENV.filter(name => env[name] !== undefined)

  for (const name of removed) {
    delete env[name]
  }

  if (removed.length > 0) {
    console.warn(`[hermes] fork env lockdown: ignoring ${removed.join(', ')} in the installed app`)
  }

  return removed
}
