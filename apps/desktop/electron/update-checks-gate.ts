'use strict'

/**
 * update-checks-gate.ts
 *
 * Pure predicate for whether the desktop self-update checker (git
 * ls-remote/fetch against origin, plus the GitHub compare API) may contact
 * upstream at all. Extracted out of main.ts so it can be unit tested without
 * importing electron.
 *
 * Two independent, either-is-sufficient guards:
 *
 *  - `HERMES_DEV=1` — the same local-checkout dev-mode env guard used
 *    elsewhere in the codebase (hermes_cli.banner._update_checks_disabled,
 *    tools/computer_use/cua_backend._driver_update_checks_disabled) for "this
 *    is a source checkout being iterated on directly, skip upstream-contact
 *    machinery meant for managed/packaged installs". This matters because the
 *    actual local dev workflow is `npm run dev` -> `electron .` directly (see
 *    apps/desktop/package.json's dev/dev:electron scripts and the repo
 *    AGENTS.md worktree rules) — that path never goes through the `hermes
 *    desktop` CLI wrapper that bridges `desktop.auto_update_checks_enabled`
 *    from config.yaml into HERMES_DESKTOP_DISABLE_UPDATE_CHECKS. Without this
 *    guard, a config.yaml opt-out is silently ineffective for the very
 *    iteration loop it exists to quiet.
 *  - `HERMES_DESKTOP_DISABLE_UPDATE_CHECKS` truthy — bridged from
 *    `desktop.auto_update_checks_enabled: false` by the `hermes desktop`
 *    launcher, or set directly by anything invoking `electron .` itself.
 */
export function updateChecksDisabled(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.HERMES_DEV === '1') {
    return true
  }

  return ['1', 'true', 'yes', 'on'].includes(
    String(env.HERMES_DESKTOP_DISABLE_UPDATE_CHECKS || '').trim().toLowerCase()
  )
}
