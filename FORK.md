# FORK.md — Taznc/hermes-agent `next` branch rules

This file is fork-owned (does not exist upstream; zero merge cost). It is the
project-level contract for every agent and human working on this branch.
Read it before editing. `AGENTS.md` (upstream's) still applies for code shape,
testing, and area routing — this file adds the fork's constraints on top.

Control-plane docs with the full rationale: `~/projects/hermes/docs/next-branch-policy.md`
and `~/projects/hermes/docs/feature-ledger.md`.

## Branch model

| Branch | Role |
|---|---|
| `main` | Pure mirror of `upstream/main`. Never receives fork commits. |
| `next` | The fork. Merge-based (never rebased). Weekly `upstream/main` merge. |
| `dev` | Frozen. Read-only port source. Never merge `dev` into `next`. |

Landing: feature branch → review → merge into `next`. Direct pushes to
`next` are for the automated sync and verified merges only.

## Tiers — declare one per card; never exceed it

| Tier | Where the change lives | Confirmation |
|---|---|---|
| **T0 Customization** | `~/projects/hermes/hermes-customizations/` — skills, MCP, Python plugins, desktop plugins. Nothing in this repo. | none |
| **T1 Fork-owned** | `hermes_fork/`, `apps/desktop/electron/fork/`, `apps/desktop/src/fork/`, `apps/desktop/src/i18n/fork/`, `tests/hermes_fork/`, any **new** file. | none |
| **T2 Anchor** | ≤ 5 lines in an upstream file: exactly one call-site into a T1 module, wrapped in `# >>> FORK ANCHOR: <name> <<<` … `# <<< FORK ANCHOR >>>` (or `//` in TS). | operator, at card creation |
| **T3 Inline** | Editing upstream code bodies. | operator, with a written reason T0–T2 cannot work |

A worker that discovers mid-card it must exceed its tier **blocks with
`needs_input`** stating what it found. It does not proceed at a higher tier.

Decision order for any request: can it be a skill? an MCP server? a plugin
(Python `on_*` hook or Desktop SDK contribution point)? a new file? an anchor?
Only then inline.

## Hard rules

- **Never** edit i18n catalogs (`apps/desktop/src/i18n/*.json`) directly — use the overlay in `src/i18n/fork/`.
- **Never** bump `_config_version` or edit `hermes_cli/config_defaults.py` / `config_migrations.py`. Fork config keys live in `hermes_fork/config.py` with their own defaults.
- **Never** commit `dist/`, built bundles, design mockups, audit evidence, or screenshots. Those belong in the control plane (`~/projects/hermes/docs/`).
- **Never** open, draft, or reference NousResearch upstream PRs.
- **Generated files** (lockfiles, catalogs, snapshots) go in their **own commit** so a sync conflict is "re-run the generator", not a manual merge.
- Editing an upstream file that already carries fork lines? Reduce the delta or keep it flat. `scripts/fork-budget.sh` (control plane) fails the pre-review gate if it grows.

## Before any feature work — the prior-art gate (mandatory, report-back)

Reinventing something upstream already ships, is reviewing, or has rejected is
the most expensive mistake on this fork: it costs the build, the review, AND a
permanent merge conflict. So this is a **gate, not a suggestion**: it runs for
every feature request — from a card or from chat — before any design or code,
and its result is reported to the user before proceeding.

1. **Ledger.** `~/projects/hermes/docs/feature-ledger.md`. If listed, follow its
   Decision (PORT / WAIT / DROP) and tier.
2. **Upstream shipped?** `git log upstream/main --grep="<keyword>" --oneline`,
   `git log upstream/main -- <likely path>`, and grep the tree. Also check
   upstream docs (`website/docs/`) and the plugin/skill catalogs.
3. **Upstream in flight?** `GH_CONFIG_DIR=/home/hermes/.config/gh gh search prs
   --repo NousResearch/hermes-agent "<keyword>" --state all --limit 20` and
   `gh search issues` likewise. Open PR = **WAIT** (default) or build a thin
   T0/T1 shim that is deleted when it lands. Closed/rejected PR = read the
   reason before proposing the same thing.
4. **`dev` prior art.** `git log origin/dev --oneline -- <path>`,
   `git show origin/dev:<path>`. Ports re-implement at the lowest tier;
   cherry-pick only when the source commit is entirely in T1 dirs.
5. **Extension seam?** Can it be a plugin / skill / MCP / desktop-plugin
   (T0)? Check `plugins/AGENTS.md`, `HERMES_PLUGIN_HOOKS`, the desktop
   contribution areas (`ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `STATUSBAR_AREA`).
6. **Codebase-memory** project `home-hermes-projects-hermes-hermes-agent-next`
   for architecture/callers before broad reading;
   `home-hermes-projects-hermes-hermes-agent-dev` is the read-only `dev` index.
   Never `index_repository` from a worktree.

**Report-back format** (in chat, or as the first comment on a card):

```
Prior art — <feature>
  ledger:    <id + decision | not listed>
  upstream:  <shipped in <sha/PR> | open PR #N (<title>, <age>) | rejected PR #N: <reason> | none found (queries: ...)>
  dev:       <commits/paths | none>
  seam:      <T0 plugin via <hook> | T1 | needs T2 anchor at <file> | T3 because ...>
  proposal:  <use upstream's | wait for PR #N | build at T<n>: ...>
```

"none found" must list the queries actually run. A card without this block in
its body or first comment is not ready to work.

## Verification (what "done" means)

- Python: `scripts/run_tests.sh <paths derived from the diff>` — never bare `pytest`. Judge against the `next` baseline, not zero.
- Desktop: `cd apps/desktop && npm run typecheck && npm run lint && npx vitest run <paths>`. Fresh worktrees have no `node_modules` — `npm install` first.
- Budget: `~/projects/hermes/scripts/fork-budget.sh` must not report growth for the touched files.
- The completion summary quotes the **command and its output**, not "tests pass".

## Environment

- Checkout: `~/projects/hermes/hermes-agent-next` (branch `next`, served at :5177, backend :9220, `HERMES_HOME=~/.hermes-next`). Protected by `git-tree-guard`: work in `.worktrees/<task>` or a Kanban workspace, land by merge.
- Runtime python: `~/.hermes-next-runtime/venv/bin/python`.
- Kanban board: `hermes-next`. Profiles: `coder` (Opus, judgment/T2+/keystones), `coder-lite` (Sonnet, bounded T0/T1 with explicit criteria), `reviewer` + `debugger` (Codex Sol), `orchestrator` (Sonnet).
- Remotes: `origin` = Taznc/hermes-agent (yours), `upstream` = NousResearch/hermes-agent (read-only). Always name the remote when reporting branch actions.
