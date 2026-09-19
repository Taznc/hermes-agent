# Cache-aware proactive-prune canary — t_b6dc22f0 (round 2, 2026-09-17)

## Goal (from the card)

Determine and implement a conservative cache-aware tool-result pruning
default (`compression.proactive_prune_tokens`) ONLY if it lowers
complete-campaign usage. Candidates under test: 96,000 and 48,000 tokens,
compared against the shipping default (disabled, `proactive_prune_tokens=0`).
Audit v2 found 96.47% cache-read input across the measured campaign and
explicitly retracted the earlier unconditional 48K rollout recommendation
pending this evidence.

## Round 1 finding and why it changed

The round-1 report (`PRUNE-CANARY-t_b6dc22f0-2026-09-15.md`, kept in-tree for
the record) concluded NEGATIVE using `cache_breaks_total = full_compressions
+ prune_commits`, an unweighted sum of two events of very different cost — a
full compression pays an LLM-backed summarizer call over the whole collapsed
region (~460–840K input tokens on these transcripts) and a prune commit pays
none. Review (comment 1223, re-verified comment 1233) blocked on three
points, all addressed in this round:

1. **Unsound headline metric / missing AC1 token metrics.** Fixed: the
   harness (`evals/compaction/scripts/prune_canary.py`) now records
   `new_input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
   `output_tokens`, `compaction_aux_calls`/`compaction_aux_input_tokens`/
   `compaction_aux_output_tokens`, and `total_calls` per candidate per
   transcript and at campaign level, using the reviewer's own probe
   methodology (`probe_usage_accounting.py`, attachment 42): per simulated
   request, the longest byte-identical message prefix vs. the previous
   request is a cache read, the remainder is new input / a fresh cache
   write. `cache_breaks_total` is retained in the JSON as an informational
   field only, explicitly labeled as not the decision metric.
2. **Cumulative skill-reload double-counting.** Fixed: `skill_reload_events`
   and `reread_events` (the AC1-named all-tool-result superset) now count
   each originally-tracked body as lost **once**, the first time it drops out
   of the still-live window — not on every subsequent commit. The old
   cumulative number is kept only as
   `skill_reload_events_cumulative_DEPRECATED` for traceability.
3. **697 MB of world-readable test data in `/tmp`.** Fixed: the old
   `/tmp/prune_canary_t_b6dc22f0/` (two `state.db` copies at 644, debug
   scratch files) was deleted. The five transcript JSONs needed to
   regenerate results were preserved at `/tmp/prune_canary_t_b6dc22f0_private/`
   with `700`/`600` permissions (owner-only).

## Method

Unchanged from round 1: deterministic, no-LLM simulation
(`evals/compaction/scripts/prune_canary.py`) against the EXACT production
gate in `agent/turn_preflight.py` and the real
`agent.context_compressor.ContextCompressor.prune_tool_results_only()`
implementation. No LLM calls; nothing mutates live session state. The same
five reconstructed lineages from round 1 (copies of `claudecode` and
`reviewer` profile `state.db` files, never the live files) are reused:

| transcript | profile | chain len | messages | tokens (chars/4 est.) |
|---|---|---:|---:|---:|
| cc_c2ffe5 | claudecode | 3 | 1,549 | 964,472 |
| cc_1e84c0 | claudecode | 3 | 1,093 | 1,257,127 |
| cc_1b7595 | claudecode | 5 | 818 | 531,346 |
| rv_0f366f | reviewer | 4 | 585 | 744,504 |
| rv_1b5a91 | reviewer | 3 | 674 | 540,515 |

Per candidate per transcript, the harness now also runs a token/cache
accounting pass alongside the existing full-compression/prune-commit walk:
for each simulated provider request, the longest byte-identical prefix
against the previous request is charged as a cache read, the remainder as
new input (and a fresh cache write for next turn); a full compression's
summarizer call is charged separately as `compaction_aux_*`.

Raw per-transcript output: `evals/compaction/results/prune-canary-t_b6dc22f0.json`.
Raw campaign totals: `evals/compaction/results/prune-canary-t_b6dc22f0-campaign.json`.
Regression test: `tests/agent/test_proactive_prune_cache_canary.py`, restated
against the token-level metrics (see "Round 1 finding" above) instead of the
unweighted event count.

## Results — campaign totals (5 transcripts)

| candidate | new_input | cache_read | aux_calls | aux_input | full_comp | prune_commits | total_calls | skill_reload (distinct) | reread (distinct) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| disabled | 4,133,475 | 115,695,513 | 6 | 2,743,434 | 6 | 0 | 480 | 0 | 0 |
| prune_96k | 5,760,860 | 84,134,717 | 1 | 472,061 | 1 | 30 | 475 | 18 | 391 |
| prune_48k | 6,165,912 | 81,061,819 | 1 | 458,968 | 1 | 52 | 475 | 23 | 463 |

Applying illustrative Anthropic-style relative unit pricing (new input=1.0,
cache read=0.10, cache write=1.25, output=5.0 — the same ratios the
reviewer's probe used; not a vendor-quoted rate, for relative comparison
only) to `new_input + cache_read*0.10 + cache_write*1.25 + output*5.0 +
aux_input*1.0 + aux_output*5.0`:

| candidate | cost units | delta vs disabled |
|---|---:|---:|
| disabled | 24,918,304 | — |
| prune_96k | 23,052,468 | **-7.49%** |
| prune_48k | 23,643,452 | **-5.12%** |

## Finding: reverses round 1 — pruning lowers modeled campaign cost, but "reread" risk is real and unresolved

Under the corrected token-level accounting, **both candidates reduce modeled
campaign cost** relative to disabled, and 96K is the stronger candidate (not
48K):

1. **Pruning does what it is designed to do.** In 4 of 5 transcripts pruning
   eliminates every full compression (6 campaign-wide → 1), removing 5 of 6
   LLM-backed summarizer calls (~2.27M aux-input tokens) and — despite adding
   ~1.6–2.0M new-input tokens from the prune commits themselves — cuts
   cache-read volume by 27–34M tokens campaign-wide. Under the illustrative
   pricing ratios, new-input is charged at 10x cache-read's rate, so the
   reduction in *cache-read volume* alone does not explain the win; the
   dominant term is removing the aux-call's full-priced input+output. This
   is the opposite sign from round 1's conclusion, which scored the same
   underlying mechanism (full compressions eliminated) as a loss because it
   weighted a free prune commit and a summarizer-backed full compression
   identically.
2. **96K beats 48K on this cost model** (-7.49% vs -5.12%): 48K commits more
   often (52 vs 30) for a smaller per-commit reclaim, paying more new-input
   overhead for the same one full-compression elimination that 96K already
   achieves on 4 of 5 transcripts. This confirms the audit's original
   caution against the unconditional 48K recommendation was directionally
   correct — 48K is not the better floor — but for a different reason than
   round 1 reported (round 1 had it as the worse of two losers; it is
   actually the weaker of two wins).
3. **The reread/skill-reload risk from round 1 is real, not eliminated by
   fixing the double-count.** Even with distinct-body counting, 96K causes
   391 tool-result rereads (18 of them skill_view bodies) and 48K causes 463
   (23 skill_view) across the campaign — these are messages a live
   conversation would have to reread or (for skill bodies) potentially
   reload framework/skill content mid-session. The cost model above does
   **not** price this risk at all (it has no line item for "skill reload"),
   and the audit's original concern — pruning can trigger skill/source
   reloads — is not addressed by the sign flip on token cost. A real
   production rollout would need either a floor that protects skill_view
   bodies specifically from pruning eligibility, or a priced cost for reread
   risk, neither of which exists yet.
4. **The illustrative pricing is exactly that — illustrative.** Real
   provider pricing, real prompt-cache TTL/eviction behavior under
   concurrent sessions, and real full-compression frequency at production
   scale are not reproduced here. This is a directional signal from 5 real
   transcripts under a no-LLM deterministic replay, not a production
   receipt.

## Decision: still NO production change this round

Per the card's Decisions — FINAL ("Change one lever at a time... implement
only the winning conservative behavior/config if quality and whole-campaign
usage improve"), the corrected evidence now shows a candidate (96K) with a
directional cost win, but:

- The skill-reload/reread risk the audit specifically flagged is confirmed
  present and unpriced — "quality" is not yet established as unaffected, one
  of the AC1/Decisions-FINAL gates for actually flipping a default.
- The win is measured on an illustrative cost model over 5 transcripts, not
  a live campaign receipt.
- No mechanism yet exists to protect `skill_view` bodies from proactive
  pruning specifically (the round-1 audit's named failure mode) — until one
  does, shipping 96K as a default would trade a measured token-cost win for
  an unmeasured, unmitigated skill-reload correctness risk.

**No config/threshold/default is changed by this round.**
`proactive_prune_tokens` stays at its shipping default (0 / disabled). This
is not a repeat of round 1's negative result — it is a corrected positive
cost signal that is not yet sufficient, on its own, to clear the bar for a
production default change under this card's Decisions-FINAL gate. The
natural next step (recommended as a separate follow-up card, not undertaken
here per "change one lever at a time") is a `skill_view`-protection floor in
`prune_tool_results_only` itself, re-run against this same harness.

## What WAS changed by this round

- `evals/compaction/scripts/prune_canary.py` — added token/cache accounting
  (`new_input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
  `output_tokens`, `compaction_aux_*`, `total_calls`) and fixed
  `skill_reload_events`/added `reread_events` to count distinct lost bodies
  once instead of cumulatively on every subsequent commit.
- `evals/compaction/results/prune-canary-t_b6dc22f0.json` — regenerated with
  the corrected metrics (same 5 transcripts, same production gate).
- `evals/compaction/results/prune-canary-t_b6dc22f0-campaign.json` — new,
  campaign-level rollup (not present in round 1).
- `tests/agent/test_proactive_prune_cache_canary.py` — restated the
  regression contract against the sound token/usage metrics instead of the
  unweighted `cache_breaks_total` event count.
- This scorecard (supersedes `PRUNE-CANARY-t_b6dc22f0-2026-09-15.md`, kept
  in-tree unmodified for the audit trail of what round 1 actually reported
  and why it was wrong).
- Cleaned up the round-1 hygiene blocker: deleted
  `/tmp/prune_canary_t_b6dc22f0/` (697 MB, world-readable, included two full
  `state.db` copies); the 5 transcript JSONs needed to reproduce this report
  now live at `/tmp/prune_canary_t_b6dc22f0_private/transcripts/` with
  `700`/`600` permissions.

## Verification

- `scripts/run_tests.sh tests/agent/test_proactive_prune_cache_canary.py`:
  2 passed.
- `scripts/run_tests.sh tests/agent/test_proactive_prune_config.py
  tests/agent/test_proactive_prune_restart_safety.py
  tests/agent/test_proactive_prune_rearm_threshold.py
  tests/run_agent/test_proactive_prune_loop_wiring.py
  evals/compaction/test_region_scoping.py
  tests/agent/test_proactive_prune_cache_canary.py`: 26 passed, 0 failed (no
  regression to the existing proactive-prune/region-scoping suites).
- `scripts/run_tests.sh tests/agent/ -k "compress or compact or prune"`:
  805 passed, 0 failed (full compression-related regression sweep).
- `ruff check evals/compaction/scripts/prune_canary.py
  tests/agent/test_proactive_prune_cache_canary.py`: clean.
- `python scripts/check_compat_pointers.py`: clean.
- `git diff --check`: clean.
- `git status --porcelain`: clean tree at commit time (only the intended
  files changed).
- Test data: five reconstructed transcripts (real historical tool/assistant
  content, never the live `state.db`) at
  `/tmp/prune_canary_t_b6dc22f0_private/transcripts/`, mode 600 inside a
  mode-700 directory — not committed to the repository (per the existing
  `evals/compaction/README.md` convention). The round-1 world-readable
  `state.db` copies and debug scratch files have been deleted. No live
  session, board, or config was mutated by this or the prior round.
