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

## Measured effect

Before/after on a real profile (`hermes_cli/prompt_size.py::compute_prompt_breakdown`,
platform=`cli`, 86 total installed skills, identical skill set both runs):

| | before | after | Δ |
|---|---:|---:|---:|
| `<available_skills>` index | 8,739 B | 4,769 B | **-45.4%** |
| system prompt total | 16,252 B | 12,322 B | -24.2% |
| skill names present | 86 | 86 | 0 (never hidden) |

45.4% exceeds the ≥40% acceptance bar. Every one of the 86 skill names was
present in both the before and after index (script-verified: `names_match`).

Applying this to a session with more installed skills / a larger shared
library scales proportionally — the demoted categories' full descriptions
are removed entirely from the wire cost and replaced by one shared
comma-joined names line.

## Cache safety

`compact_categories` is folded into the skills-prompt cache key
(`agent/prompt_builder.py`, `_skills_prompt` in `agent/system_prompt.py`), so
a change to `skills.compact_categories` only affects **new** sessions — an
already-running conversation's system prompt is never mutated mid-session
(prompt-cache invariant, root `AGENTS.md`).
