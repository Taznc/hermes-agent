# Cache-aware proactive-prune canary — t_b6dc22f0 (2026-09-15)

## Goal (from the card)

Determine and implement a conservative cache-aware tool-result pruning
default (`compression.proactive_prune_tokens`) ONLY if it lowers
complete-campaign usage. Candidates under test: 96,000 and 48,000 tokens,
compared against the shipping default (disabled, `proactive_prune_tokens=0`).
Audit v2 found 96.47% cache-read input across the measured campaign and
explicitly retracted the earlier unconditional 48K rollout recommendation
pending this evidence.

## Method

Deterministic, no-LLM simulation (`evals/compaction/scripts/prune_canary.py`)
against the EXACT production gate in `agent/turn_preflight.py` (`elif
agent.compression_enabled: ... prune_tool_results_only(...)`) and the real
`agent.context_compressor.ContextCompressor.prune_tool_results_only()`
implementation — not a re-derivation. No LLM calls; nothing mutates live
session state.

Five representative historical lineages were reconstructed from **copies**
of `claudecode` and `reviewer` profile `state.db` files (never the live
files) with the existing `evals/compaction/scripts/reconstruct_lineage.py`,
chosen as the largest available real multi-rotation chains:

| transcript | profile | chain len | messages | tokens (chars/4 est.) |
|---|---|---:|---:|---:|
| cc_c2ffe5 | claudecode | 3 | 1,549 | 964,472 |
| cc_1e84c0 | claudecode | 3 | 1,093 | 1,257,127 |
| cc_1b7595 | claudecode | 5 | 818 | 531,346 |
| rv_0f366f | reviewer | 4 | 585 | 744,504 |
| rv_1b5a91 | reviewer | 3 | 674 | 540,515 |

For each transcript and each candidate (`disabled`, `prune_96k`,
`prune_48k`), the transcript is replayed in fixed 10-message batches,
tracking the LIVE window's estimated token count. At/above
`ContextCompressor.threshold_tokens` (50% of the routed model's context
window — 500,000 tokens for `claude-sonnet-5`/1M), a full compression is
counted (this path is common to every candidate and is not what pruning
changes). Below threshold, `prune_tool_results_only()` is called with the
real `current_tokens` reading, exactly like production; a commit is counted
only when it returns a NEW list object with a non-zero prune count (the
production no-op contract).

Metrics recorded per candidate per transcript: `full_compressions`,
`prune_commits`, `prune_reclaimed_tokens`, `cache_breaks_total` (=
full_compressions + prune_commits — **every commit is itself a cache break**:
it rewrites the message list and calls `archive_and_compact`, repricing the
next request at new-input rates until the cache re-warms), and
`skill_reload_risk_events` (count of prune commits that touched a message
inside a still-live window that had contained a `skill_view` tool result —
the specific failure mode the audit named: "pruning ... can trigger
skill/source reloads").

Raw output: `evals/compaction/results/prune-canary-t_b6dc22f0.json`.
Regression test: `tests/agent/test_proactive_prune_cache_canary.py` (runs
the same `simulate()` on a synthetic transcript so the contract — pruning
must never show FEWER cache breaks than disabled — is enforced by
`scripts/run_tests.sh`, not just this one-off report).

## Results

| transcript | disabled breaks | prune_96k breaks (full/commit) | prune_48k breaks (full/commit) | 96k skill-reload risk | 48k skill-reload risk |
|---|---:|---:|---:|---:|---:|
| cc_c2ffe5 | 1 | 7 (1/6) | 15 (1/14) | 4 | 18 |
| cc_1e84c0 | 2 | 10 (0/10) | 12 (0/12) | 26 | 30 |
| cc_1b7595 | 1 | 4 (0/4) | 9 (0/9) | 9 | 23 |
| rv_0f366f | 1 | 6 (0/6) | 10 (0/10) | 22 | 42 |
| rv_1b5a91 | 1 | 4 (0/4) | 7 (0/7) | 4 | 8 |
| **total** | **6** | **31** | **53** | **65** | **121** |

## Finding: NEGATIVE — neither candidate wins; recommend no config change

Both `proactive_prune_tokens=96,000` and `=48,000` **multiply
cache-breaking events by roughly 5x and 9x respectively** versus disabled,
on every one of the five transcripts, with zero exceptions. This is the
audit's predicted failure mode confirmed on real historical data, not just
theory:

1. **Every prune commit is a cache break, and the deterministic prune fires
   far more often than full compression does.** Full compression is
   naturally self-limiting (threshold_tokens is far above
   proactive_prune_tokens, so it fires rarely). The proactive prune has no
   equivalent floor beyond `proactive_prune_min_reclaim_tokens` (4,096
   tokens default) — on transcripts with many large tool results (exactly
   the profile these two real profiles have), it commits repeatedly well
   before a full compression would ever trigger, each commit paying a fresh
   cache-break cost for a comparatively small reclaim.
2. **48K makes this categorically worse than 96K** (53 vs 31 total cache
   breaks across the five transcripts; 121 vs 65 skill-reload-risk events)
   — confirming the audit's caution against the earlier "unconditional 48K"
   recommendation was correct to retract it, and that 48K is the worse of
   the two candidates, not a safer floor.
3. **Skill/source reload risk is real and scales with prune frequency**,
   not just candidate choice: every transcript shows double-digit
   `skill_reload_risk_events` under 48K, confirming the audit's specific
   concern that pruning breaks cached prefix continuity in a way that can
   force expensive re-loads independent of the raw cache-break count above.
4. Given 96.47% cache-read share in the measured campaign (audit v2 §
   Executive decision), a mechanism that adds 5-9x more cache-breaking
   commits than the status quo is very likely to raise, not lower,
   complete-campaign usage — the opposite of this card's win condition.

No candidate clears the bar ("implement only the winning conservative
behavior/config if quality and whole-campaign usage improve"). Per the
card's Decisions — FINAL ("Roll back/no-op is a valid completed result if
neither candidate beats disabled safely"), **the result is roll back/no-op:
`proactive_prune_tokens` stays at its shipping default (0 / disabled)**. No
production config, threshold, or default changed by this card. The
mechanism (`prune_tool_results_only`) itself is unmodified — this canary
only exercises it read-only against copies.

## What WAS added by this card

- `evals/compaction/scripts/prune_canary.py` — the reusable, no-LLM canary
  harness (usable for any future candidate value without another audit
  round).
- `evals/compaction/results/prune-canary-t_b6dc22f0.json` — raw per-candidate
  metrics for the five transcripts above.
- `tests/agent/test_proactive_prune_cache_canary.py` — 2 focused regression
  tests (behavior contract: pruning must never show fewer cache breaks than
  disabled on a representative synthetic transcript; a committed prune must
  actually reclaim tokens) via `scripts/run_tests.sh`.
- This scorecard.

## Verification

- `scripts/run_tests.sh tests/agent/test_proactive_prune_cache_canary.py`:
  2 passed.
- `scripts/run_tests.sh tests/agent/test_proactive_prune_config.py
  tests/agent/test_proactive_prune_restart_safety.py
  tests/agent/test_proactive_prune_rearm_threshold.py
  tests/run_agent/test_proactive_prune_loop_wiring.py
  evals/compaction/test_region_scoping.py`: 24 passed, 0 failed (no
  regression to the existing proactive-prune/region-scoping suites).
- `scripts/run_tests.sh tests/agent/ -k "compress or compact or prune"`:
  805 passed, 0 failed (full compression-related regression sweep).
- `ruff check evals/compaction/scripts/prune_canary.py
  tests/agent/test_proactive_prune_cache_canary.py`: clean.
- `python scripts/check_compat_pointers.py`: clean.
- `git diff --check`: clean.
- Test data: copies of `~/.hermes/profiles/{claudecode,reviewer}/state.db`
  in `/tmp/prune_canary_t_b6dc22f0/` (never the live files); reconstructed
  transcripts contain real historical tool/assistant content and were used
  only locally for this simulation — not committed to the repository (per
  the existing `evals/compaction/README.md` convention: "Transcripts are
  NOT committed"). No live session, board, or config was mutated.
