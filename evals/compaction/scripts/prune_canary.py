"""Cache-aware proactive-prune canary (t_b6dc22f0).

Deterministic, no-LLM simulation. Walks each reconstructed transcript
turn-by-turn (assistant/tool message boundaries, in original chronological
order) and replays the EXACT production gate from turn_preflight.py:

  - Below the full-compression threshold_tokens (50% of window, ContextCompressor
    default) but above candidate `proactive_prune_tokens`, call
    ContextCompressor.prune_tool_results_only(messages, current_tokens) exactly
    as agent/turn_preflight.py does.
  - At/above threshold_tokens, a full compression would fire instead (modeled
    as a session reset to a fresh short prefix, matching compress()'s effect
    on message-list size — the deterministic prune never runs in the same
    turn as a full compression in production, see turn_preflight.py L349-376).

For each candidate (disabled, proactive_prune_tokens=96_000, 48_000) records,
per transcript:
  - full_compressions: how many times token growth crossed threshold_tokens
    (the expensive, LLM-backed, cache-breaking path every candidate shares)
  - prune_commits: how many times prune_tool_results_only actually committed
    (each commit is ALSO a cache break: it rewrites the message list and
    calls archive_and_compact)
  - prune_reclaimed_tokens: total tokens reclaimed across all commits
  - cache_breaks_total: full_compressions + prune_commits (the metric that
    matters under a 96%+ cache-read regime: every break re-prices everything
    downstream at new-input rates until the cache re-warms)
  - skill_reload_risk: count of prune commits that touched a message inside
    the region still holding a `skill_view`-sourced tool result (i.e. a
    protected-skill body that decayed out of protection because the prune
    boundary is independent of the skill freshness window) — the specific
    failure mode the audit flagged ("pruning ... can trigger skill/source
    reloads").

Usage:
    python evals/compaction/scripts/prune_canary.py <transcript.json> [...]
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from evals.compaction.fixtures import estimate_tokens, load_transcript  # noqa: E402
from agent.context_compressor import ContextCompressor  # noqa: E402

EVAL_MODEL = "claude-sonnet-5"  # this card's own routed model; 1,000,000-token window


def _new_compressor(proactive_prune_tokens: int, config_context_length: int | None = None) -> ContextCompressor:
    return ContextCompressor(
        model=EVAL_MODEL,
        quiet_mode=True,
        proactive_prune_tokens=proactive_prune_tokens,
        # production defaults (agent_init.py fallbacks)
        proactive_prune_min_result_chars=8000,
        proactive_prune_min_reclaim_tokens=4096,
        config_context_length=config_context_length,
    )


def _skill_view_tool_result_indices(messages) -> set[int]:
    """Indices of tool messages whose call was a skill_view (protected-skill bodies)."""
    call_id_to_tool = {}
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                call_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if call_id:
                    call_id_to_tool[call_id] = name
    out = set()
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and call_id_to_tool.get(m.get("tool_call_id", "")) == "skill_view":
            out.add(i)
    return out


def simulate(transcript_path: str, candidates: dict[str, int], config_context_length: int | None = None) -> dict:
    messages = load_transcript(transcript_path)
    results = {}
    for label, prune_tokens in candidates.items():
        comp = _new_compressor(prune_tokens, config_context_length=config_context_length)
        threshold = comp.threshold_tokens  # 50% of 1,000,000 = 500,000 by default
        working = copy.deepcopy(messages)
        skill_idx_original = _skill_view_tool_result_indices(working)

        full_compressions = 0
        prune_commits = 0
        prune_reclaimed = 0
        skill_reload_risk = 0
        cumulative = 0
        window_start = 0  # index of the start of the "live" (post-compaction) window

        # Walk in fixed-size turn batches (10 messages) to bound simulation cost
        # while still crossing every threshold multiple times on 500K-1.2M token transcripts.
        i = 0
        step = 10
        while i < len(working):
            i = min(i + step, len(working))
            live = working[window_start:i]
            current_tokens = sum(estimate_tokens(m) for m in live)
            if current_tokens >= threshold:
                # Full compression fires: cache break; model resets to a short live window
                # (head + synthetic summary + tail), matching compress()'s effect on size.
                full_compressions += 1
                window_start = max(window_start, i - comp.protect_last_n)
                continue
            if prune_tokens <= 0:
                continue
            # Exact production gate (agent/turn_preflight.py L361-376).
            pruned_live, pruned_n = comp.prune_tool_results_only(live, current_tokens=current_tokens)
            if pruned_n and pruned_live is not live:
                prune_commits += 1
                before_tok = sum(estimate_tokens(m) for m in live)
                after_tok = sum(estimate_tokens(m) for m in pruned_live)
                prune_reclaimed += max(0, before_tok - after_tok)
                working[window_start:i] = pruned_live
                # Any originally-protected skill_view body that no longer survives verbatim
                # inside the still-live window is a reload risk.
                still_live_content = {
                    m.get("content") for m in pruned_live
                    if isinstance(m.get("content"), str)
                }
                for orig_idx in skill_idx_original:
                    if window_start <= orig_idx < i:
                        orig_content = messages[orig_idx].get("content")
                        if isinstance(orig_content, str) and orig_content not in still_live_content:
                            skill_reload_risk += 1

        results[label] = {
            "proactive_prune_tokens": prune_tokens,
            "threshold_tokens": threshold,
            "full_compressions": full_compressions,
            "prune_commits": prune_commits,
            "prune_reclaimed_tokens": prune_reclaimed,
            "cache_breaks_total": full_compressions + prune_commits,
            "skill_reload_risk_events": skill_reload_risk,
            "transcript_total_tokens": sum(estimate_tokens(m) for m in messages),
            "transcript_messages": len(messages),
        }
    return results


def main():
    candidates = {"disabled": 0, "prune_96k": 96_000, "prune_48k": 48_000}
    all_results = {}
    for path in sys.argv[1:]:
        name = Path(path).stem
        print(f"== {name} ==", flush=True)
        r = simulate(path, candidates)
        all_results[name] = r
        for label, m in r.items():
            print(f"  {label:12s} cache_breaks={m['cache_breaks_total']:3d} "
                  f"(full={m['full_compressions']}, prune_commits={m['prune_commits']}) "
                  f"reclaimed={m['prune_reclaimed_tokens']:,} "
                  f"skill_reload_risk={m['skill_reload_risk_events']}")
    out = Path(__file__).resolve().parent.parent / "results" / "prune-canary-t_b6dc22f0.json"
    out.write_text(json.dumps(all_results, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
