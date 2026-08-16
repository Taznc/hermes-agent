# Customized Hermes Development

This fork is reserved for upstream-compatible Hermes core changes. Keep product-specific integrations, skills, MCP packages, and Desktop extensions in the private sibling repository at `~/Projects/hermes-customizations` whenever an extension surface can support them.

## Repository topology

```text
~/Projects/hermes-agent/             # Taznc/hermes-agent fork; core changes only
  origin   https://github.com/Taznc/hermes-agent.git
  upstream https://github.com/NousResearch/hermes-agent.git (fetch-only)

~/Projects/hermes-customizations/    # private integrations and extensions

~/.hermes/venvs/hermes-dev/          # external editable Python development venv
~/.hermes-dev/                       # isolated development runtime state
~/.hermes/                           # daily-use Hermes state; do not use for development
```

`main` is a mirror of `upstream/main`; never add custom commits to it. Custom core work belongs in `custom/<area>-<feature>`, upstream-ready fixes in `fix/<description>`, and documentation in `docs/<description>`. Create disposable parallel worktrees only under `.worktrees/`.

## First-time setup

The setup is intentionally split from the daily-use runtime:

```bash
cd ~/Projects/hermes-agent
uv venv ~/.hermes/venvs/hermes-dev --python 3.11
uv pip install --python ~/.hermes/venvs/hermes-dev/bin/python -e '.[all,dev]'
PATH="$(brew --prefix node)/bin:$PATH" npm ci
./scripts/dev-env.sh hermes config set display.interface cli
```

`./scripts/dev-env.sh` selects the Node 26 line pinned by `.nvmrc`, the external development venv, the source checkout, and `~/.hermes-dev`. Run all development commands through it. It never reads or writes credentials from the daily-use `~/.hermes` runtime.

Settings belong in the active `config.yaml`, managed with `hermes config set`; credentials belong only in the active `.env`. Do not copy production credentials into this repository or commit any `.env` file.

## Run and verify

```bash
# CLI and local health
./scripts/dev-env.sh hermes doctor
./scripts/dev-env.sh hermes gateway --help
./scripts/dev-env.sh hermes serve --help

# Python tests: canonical hermetic runner, external venv explicitly selected
./scripts/dev-env.sh scripts/run_tests.sh tests/hermes_cli/test_config.py -q
./scripts/dev-env.sh scripts/run_tests.sh tests/hermes_cli/test_plugins.py -q

# Desktop development and focused checks
./scripts/dev-env.sh npm --workspace apps/desktop run typecheck
./scripts/dev-env.sh npm --workspace apps/desktop run test -- src/themes/skin.test.ts
./scripts/dev-env.sh npm --workspace apps/desktop run dev

# Release-path checks when install, update, or packaging code changes
./scripts/dev-env.sh npm --workspace apps/desktop run test:desktop:all
```

Desktop starts its local Hermes backend itself. `HERMES_DESKTOP_HERMES_ROOT` is set to this checkout, so the Electron app uses the fork source; `HERMES_HOME` stays isolated in `~/.hermes-dev`.

## Safely synchronize upstream

Only synchronize a clean `main` branch:

```bash
cd ~/Projects/hermes-agent
git switch main
git fetch upstream --prune
git merge --ff-only upstream/main
git push origin main
```

Rebase a custom branch instead of merging upstream into it:

```bash
git switch custom/<area>-<feature>
git fetch upstream --prune
git rebase upstream/main
./scripts/dev-env.sh scripts/run_tests.sh <targeted-test-path>
git push --force-with-lease origin custom/<area>-<feature>
```

Never use plain `--force`, and never rebase `main`. `rerere` is enabled locally to remember conflict resolutions, but each rebased feature must still be tested against the current `upstream/main`.

## Rollback and recovery

- Abort an unfinished rebase with `git rebase --abort`.
- Create a rescue branch before any reset: `git branch rescue/<name> HEAD`.
- Use `git reflog` to locate a known-good commit.
- Preserve changes to the managed daily runtime before updating it. Development work belongs in this fork or the customization repository, never as an uncommitted edit under `~/.hermes/hermes-agent`.

## Extension selection

Use the smallest surface that solves the problem:

1. Existing configuration or toolset.
2. Skill for repeatable procedure and orchestration guidance.
3. Python plugin for custom tools, hooks, gateway commands, or adapters.
4. MCP server for structured external functionality reusable by other hosts.
5. Desktop plugin for panes, pages, palette commands, and native UI.
6. Hermes core patch only when no generic extension contract can support it.

The customization repository records the compatibility target for each extension. Keep third-party integrations there as standalone plugins; do not hard-code vendor behavior into Hermes core. Core patches must preserve prompt caching, message-role alternation, profile-aware paths, and cross-platform behavior.

## Extension-specific checks

```bash
# Native Python plugin from the customization repository
./scripts/dev-env.sh hermes plugins doctor /path/to/plugin --ci

# Desktop plugin
# Load from $HERMES_HOME/desktop-plugins/<id>/plugin.js, then use
# Cmd+K -> Reload desktop plugins in the running Desktop app.
```

Desktop disk plugins are uncompiled ESM files: use only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`; use `jsx()`/`jsxs()` rather than JSX syntax.
