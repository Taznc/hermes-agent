'use strict'

import fs from 'node:fs'

/**
 * update-checks-gate.ts
 *
 * Pure predicate for whether the desktop self-update checker (git
 * ls-remote/fetch against origin, plus the GitHub compare API) may contact
 * upstream at all. Extracted out of main.ts so it can be unit tested without
 * importing electron.
 *
 * Three independent guards, in precedence order:
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
 *  - `HERMES_DESKTOP_DISABLE_UPDATE_CHECKS` — when the env var is present at
 *    all (any value, including an explicit falsy one), it wins outright and
 *    config.yaml is never consulted. Bridged from `desktop.
 *    auto_update_checks_enabled: false` by the `hermes desktop` launcher, or
 *    set directly by anything invoking `electron .` itself.
 *  - `config.yaml`'s `desktop.auto_update_checks_enabled` — read directly,
 *    ONLY when the env var above is entirely absent. A packaged Hermes.app
 *    launched from the Dock, Finder, or Spotlight gets none of the `hermes
 *    desktop` CLI's bridged env vars — process.env is whatever launchd/
 *    Explorer/the desktop shell handed it, never HERMES_DESKTOP_DISABLE_
 *    UPDATE_CHECKS — so without this rung a user's `desktop.
 *    auto_update_checks_enabled: false` in config.yaml is silently
 *    ineffective on every launch that doesn't go through the CLI wrapper.
 *    Caller supplies the resolved config.yaml path (main.ts already computes
 *    HERMES_HOME); omitting it disables this rung entirely rather than
 *    guessing a path, so unit tests stay hermetic (no ambient filesystem
 *    reads against the real user's HERMES_HOME).
 */

const TRUTHY_VALUES = ['1', 'true', 'yes', 'on']
const FALSY_VALUES = ['0', 'false', 'no', 'off']

/**
 * Extract `desktop.auto_update_checks_enabled` out of a config.yaml file with
 * a scoped line-by-line scan instead of a full YAML parser dependency — same
 * "smallest footprint" call as desktop-plugin-install.ts's frontmatter `name:`
 * regex extraction. Electron's desktop app has no js-yaml dependency and this
 * is the only config.yaml field it needs pre-boot.
 *
 * Returns `null` when the file is missing/unreadable, the top-level `desktop:`
 * section isn't present, the key isn't set inside it, or the value isn't one
 * of the truthy/falsy strings hermes_cli's `_desktop_launch_options()` uses
 * for the same key — every one of those cases falls through to the safe
 * default (checks enabled). Returns `true`/`false` only on an explicit,
 * recognized value.
 */
export function readConfigAutoUpdateChecksEnabled(configPath: string): boolean | null {
  let raw: string

  try {
    raw = fs.readFileSync(configPath, 'utf8')
  } catch {
    return null
  }

  let inDesktopSection = false

  for (const line of raw.split('\n')) {
    if (/^desktop:\s*(#.*)?$/.test(line)) {
      inDesktopSection = true

      continue
    }

    if (!inDesktopSection) {
      continue
    }

    const trimmed = line.trim()

    // A blank line or a comment line doesn't end the section; any other
    // unindented line starts the next top-level key and ends it.
    if (trimmed && !trimmed.startsWith('#') && /^\S/.test(line)) {
      break
    }

    const match = line.match(/^\s+auto_update_checks_enabled:\s*(.+?)\s*(#.*)?$/)

    if (!match) {
      continue
    }

    const value = match[1].trim().replace(/^['"]|['"]$/g, '').toLowerCase()

    if (FALSY_VALUES.includes(value)) {
      return false
    }

    if (TRUTHY_VALUES.includes(value)) {
      return true
    }

    return null
  }

  return null
}

export function updateChecksDisabled(
  env: NodeJS.ProcessEnv = process.env,
  options: { configPath?: string | null } = {}
): boolean {
  if (env.HERMES_DEV === '1') {
    return true
  }

  if ('HERMES_DESKTOP_DISABLE_UPDATE_CHECKS' in env) {
    return TRUTHY_VALUES.includes(
      String(env.HERMES_DESKTOP_DISABLE_UPDATE_CHECKS || '').trim().toLowerCase()
    )
  }

  if (!options.configPath) {
    return false
  }

  return readConfigAutoUpdateChecksEnabled(options.configPath) === false
}
