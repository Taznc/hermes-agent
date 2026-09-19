# Context efficiency: skill-index compaction (t_e6c09107)

## Problem

The `<available_skills>` index in the Desktop system prompt ships every
installed skill's one-line description on every turn. Measured 2026-09-19 on
a real profile (127 installed skills, 30-day usage audit): **61 categories/
skills were never loaded in 30 days**, and the index accounted for
30-55% of the total prompt bytes on Codex-route sessions.

`agent/prompt_builder.py::_render_skills_index` already supported
`compact_categories` — a category name in that set renders as a single
`<category> [names only]: a, b, c` line instead of full per-skill
descriptions. Nothing is ever hidden: every name stays in the index and
`skill_view(name)` / `skills_list` load the full skill exactly as before.

Before this change the ONLY producer of `compact_categories` was
`agent/coding_context.py::coding_compact_skill_categories()`, which is gated
on the opt-in `agent.coding_context: focus` posture and a detected code
workspace. A Desktop session outside that posture (the common case) got the
full, uncompacted index regardless of how large the operator's skill library
was.

## What changed

`coding_compact_skill_categories()` now unions two sources:

1. **Posture deny-list** (unchanged): `_NON_CODING_SKILL_CATEGORIES`, applied
   only when `agent.coding_context` resolves to `focus` in a coding
   workspace.
2. **Operator config** (new): `skills.compact_categories` in `config.yaml` —
   a list (or bare string) of top-level category names to demote, applied on
   **every** platform and posture, unconditionally.

Both feed the same `_render_skills_index(compact_categories=...)` seam, so
the rendering, caching, and "never hidden" contract are shared code — no new
prompt-builder logic was needed. See `agent/coding_context.py`:
`_configured_compact_skill_categories()` (config parsing, fails open to `[]`
on any malformed value) and the updated `coding_compact_skill_categories()`
(union).

Nested categories (`social-media/twitter`) fold to their top-level segment,
matching the posture deny-list's own semantics.

## Prior art check

Three upstream PRs on `NousResearch/hermes-agent` propose overlapping designs
for the same `skills.compact_categories` surface (checked via
`GH_CONFIG_DIR=/home/hermes/.config/gh gh search prs --repo
NousResearch/hermes-agent compact skills categories` before implementing):

- #87199 (open) — `pinned_categories` / `demote_all_categories` /
  `keep_full_categories`, `"*"` wildcard support.
- #109961 (open, marked duplicate) — plain `skills.compact_categories` list,
  closest to this fork's implementation.
- #102160 (closed, superseded by #87199) — same surface, closed in favor of
  the broader wildcard design.

This fork's change is the smallest isolated delta matching #109961's shape
(a plain list, unioned with the existing posture set) rather than #87199's
wildcard/keep-full design, since the acceptance criteria here only require
demoting a fixed set of never-used categories, not an operator-wide `"*"`
toggle. If the fleet later wants the wildcard behavior, adopt #87199's
`keep_full_categories` shape instead of layering a second config surface.

## Recommended Desktop default

Categories with zero 30-day usage on the audited profile (creative, media,
research, email, smart-home, social-media, mlops, apple, note-taking,
autonomous-ai-agents) — kept OUT of the demote list: kanban/session/xlsx-
adjacent productivity items the user actually loads, and any category with
recent usage.

```yaml
skills:
  compact_categories:
    - creative
    - media
    - research
    - email
    - smart-home
    - social-media
    - mlops
    - apple
    - note-taking
    - autonomous-ai-agents
```

## Measured effect — fresh Desktop session, `system_prompts` table

Round-1 review correctly rejected the original measurement
(`compute_prompt_breakdown(platform="cli")` on an 86-skill profile) as not
evidence for the Desktop default on the real 127-skill population. Re-measured
with a script
(`/tmp/desktop_fresh_session_probe.py` — throwaway, not committed) that:

1. Builds two isolated `HERMES_HOME` temp dirs, each **symlinking** the real
   `/home/hermes/.hermes/skills` (the actual 127-skill installed population,
   not a synthetic stand-in) and seeding `config.yaml` from the live file
   (`skills.compact_categories: []` for "before", the recommended list above
   for "after" — never hand-written from scratch).
2. Resolves the Desktop toolset list through the REAL production path,
   `tui_gateway.server._load_enabled_toolsets(platform="desktop")` (the same
   function the Desktop backend calls), then constructs a real
   `platform="desktop"` `AIAgent` and calls `agent.system_prompt.build_system_prompt`.
3. Persists the rendered prompt into that isolated home's `state.db` via
   `SessionDB.create_session(..., system_prompt=full_prompt)` and reads it
   back from the **`system_prompts` table** (`SELECT sp.prompt FROM sessions
   s JOIN system_prompts sp ON sp.hash = s.system_prompt_hash`), asserting the
   persisted bytes equal the rendered bytes — this is the literal Desktop
   session-creation path, not an offline breakdown tool.

Results (both runs against the identical real skill population):

| | before (`compact_categories: []`) | after (recommended list) | Δ |
|---|---:|---:|---:|
| `<available_skills>` block | 11,858 B | 6,836 B | **-42.4%** |
| system prompt total | 27,124 B | 22,128 B | -18.4% |
| skill names rendered in index | 122 | 122 | 0 (never hidden) |

42.4% exceeds the ≥40% acceptance bar (AC1), measured from the
`system_prompts` table of a session built through the real Desktop
construction path (AC1's exact ask).

### AC2 — every installed skill still appears

Installed-name census (`agent.skill_utils.get_all_skills_dirs` +
`iter_skill_index_files`) against the same symlinked population finds **127**
directories with a `SKILL.md`. The rendered index (both before AND after)
contains **122** of them; the identical 5-name gap in both runs is
pre-existing, unrelated to this change, and never regresses:

- `apple-notes`, `apple-reminders`, `findmy` (all `platforms: [macos]` —
  filtered by `skill_matches_platform` on this Linux host; would render on
  macOS) — `agent/skill_utils.py`.
- `imessage` (`platforms: [macos]`, same gate).
- `research-paper-writing` (`metadata.hermes.requires_toolsets: [terminal,
  files]` in its frontmatter — `_skill_should_show`'s conditional-activation
  gate hides it because `"files"` is not a real toolset name in this
  install's `available_toolsets`) — pre-existing frontmatter issue in the
  skill itself, not something `compact_categories` touches.

`before_names == after_names` (both sets of 122 are byte-identical) is the
load-bearing assertion: the config change moves categories to names-only, it
does not drop a single visible name. The 5-name gap is orthogonal, was
present before this task started, and is out of scope for it.

## Cache safety

`compact_categories` is folded into the skills-prompt cache key
(`agent/prompt_builder.py`, `_skills_prompt` in `agent/system_prompt.py`), so
a change to `skills.compact_categories` only affects **new** sessions — an
already-running conversation's system prompt is never mutated mid-session
(prompt-cache invariant, root `AGENTS.md`).
