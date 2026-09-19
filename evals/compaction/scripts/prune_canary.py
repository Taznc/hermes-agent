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

Round-1 review (comment 1223, re-verified comment 1233) found the original
scoring unsound in two ways, both fixed here:

  1. `cache_breaks_total = full_compressions + prune_commits` weighted a
     full compression (an LLM-backed summarizer call over the whole collapsed
     region, ~460-840K input tokens on these transcripts) the same as a prune
     commit (no auxiliary call at all) — the wrong sign follows directly from
     that miscount. This version reports the AC1-required token-level
     metrics instead (new input, cache reads/writes, output, compaction
     auxiliary input/output, total calls) so the conclusion follows from
     usage, not from an unweighted event sum. Methodology matches the
     reviewer's probe (`probe_usage_accounting.py`, attachment 42): per
     simulated request, the longest byte-identical message prefix vs the
     previous request is a cache read, the remainder is new input / a fresh
     cache write.
  2. `skill_reload_risk_events` rescanned every originally-protected
     `skill_view` index still inside the live window on every commit, so one
     lost body was recounted at each subsequent commit (18-42 reported vs
     4-7 actual distinct bodies lost — probe attachment 41). This version
     counts each lost body exactly once via `skill_reload_events`
     (skill_view-sourced only) and `reread_events` (any tool-result body,
     the AC1-named superset metric) — both DISTINCT counts, not cumulative.

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
# Compaction-summary output budget: order-of-magnitude only, used solely to
# report an aux_output figure alongside aux_input; does not affect any other
# metric or the pass/fail comparison.
SUMMARY_OUTPUT_TOKENS = 4000
OUTPUT_PER_CALL = 500  # identical across every candidate; reported for AC1 completeness


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


def _all_tool_result_indices(messages) -> set[int]:
    """Indices of every tool-result message (the AC1 'rereads' superset)."""
    return {i for i, m in enumerate(messages) if m.get("role") == "tool"}


def _fingerprint(m: dict) -> tuple:
    """Identity used for cache-prefix matching: same shape probe_usage_accounting.py used."""
    return (
        m.get("role"),
        m.get("content") if isinstance(m.get("content"), str) else None,
        json.dumps(m.get("tool_calls"), default=str, sort_keys=True) if m.get("tool_calls") else None,
        m.get("tool_call_id"),
    )


def simulate(transcript_path: str, candidates: dict[str, int], config_context_length: int | None = None) -> dict:
    messages = load_transcript(transcript_path)
    results = {}
    for label, prune_tokens in candidates.items():
        comp = _new_compressor(prune_tokens, config_context_length=config_context_length)
        threshold = comp.threshold_tokens  # 50% of 1,000,000 = 500,000 by default
        working = copy.deepcopy(messages)
        skill_idx_original = _skill_view_tool_result_indices(working)
        all_tool_idx_original = _all_tool_result_indices(working)

        full_compressions = 0
        prune_commits = 0
        prune_reclaimed = 0
        cumulative = 0
        window_start = 0  # index of the start of the "live" (post-compaction) window

        # Distinct-body tracking (fixes the round-1 double-counting bug): once an
        # original index's content is gone from the still-live window, it counts
        # once, ever — later commits touching the same already-lost index are not
        # recounted.
        skill_bodies_lost: set[int] = set()
        reread_bodies_lost: set[int] = set()

        # Token/cache accounting (fixes the round-1 missing-AC1-metrics gap):
        # per simulated request, mirrors probe_usage_accounting.py step-for-step.
        new_input = 0
        cache_read = 0
        cache_write = 0
        output_tokens = 0
        calls = 0
        aux_calls = 0
        aux_input = 0
        aux_output = 0
        tokcache: dict[int, int] = {}
        _keepalive = []  # id() is only stable while the object is alive; pin every key

        def tok(m):
            k = id(m)
            if k not in tokcache:
                tokcache[k] = estimate_tokens(m)
                _keepalive.append(m)
            return tokcache[k]

        prev_fps: list = []
        prev_toks: list = []

        # Walk in fixed-size turn batches (10 messages) to bound simulation cost
        # while still crossing every threshold multiple times on 500K-1.2M token transcripts.
        i = 0
        step = 10
        while i < len(working):
            i = min(i + step, len(working))
            live = working[window_start:i]
            current_tokens = sum(tok(m) for m in live)
            if current_tokens >= threshold:
                # Full compression fires: cache break; model resets to a short live window
                # (head + synthetic summary + tail), matching compress()'s effect on size.
                full_compressions += 1
                # LLM-backed summarizer auxiliary call over the region it collapses.
                region = live[comp.protect_first_n:max(comp.protect_first_n, len(live) - comp.protect_last_n)]
                aux_calls += 1
                aux_input += sum(tok(m) for m in region)
                aux_output += SUMMARY_OUTPUT_TOKENS
                window_start = max(window_start, i - comp.protect_last_n)
                live = working[window_start:i]
            elif prune_tokens > 0:
                # Exact production gate (agent/turn_preflight.py L361-376).
                pruned_live, pruned_n = comp.prune_tool_results_only(live, current_tokens=current_tokens)
                if pruned_n and pruned_live is not live:
                    prune_commits += 1
                    before_tok = sum(tok(m) for m in live)
                    after_tok = sum(estimate_tokens(m) for m in pruned_live)
                    prune_reclaimed += max(0, before_tok - after_tok)
                    working[window_start:i] = pruned_live
                    live = pruned_live
                    # Any originally-tracked body that no longer survives verbatim inside
                    # the still-live window is lost. Count distinct indices only once.
                    still_live_content = {
                        m.get("content") for m in pruned_live
                        if isinstance(m.get("content"), str)
                    }
                    for orig_idx in skill_idx_original:
                        if orig_idx in skill_bodies_lost:
                            continue
                        if window_start <= orig_idx < i:
                            orig_content = messages[orig_idx].get("content")
                            if isinstance(orig_content, str) and orig_content not in still_live_content:
                                skill_bodies_lost.add(orig_idx)
                                cumulative += 1
                    for orig_idx in all_tool_idx_original:
                        if orig_idx in reread_bodies_lost:
                            continue
                        if window_start <= orig_idx < i:
                            orig_content = messages[orig_idx].get("content")
                            if isinstance(orig_content, str) and orig_content not in still_live_content:
                                reread_bodies_lost.add(orig_idx)

            # --- the actual request that goes to the provider this turn ---
            fps = [_fingerprint(m) for m in live]
            toks = [tok(m) for m in live]
            n = 0
            while n < len(fps) and n < len(prev_fps) and fps[n] == prev_fps[n]:
                n += 1
            cache_read += sum(toks[:n])
            fresh = sum(toks[n:])
            new_input += fresh
            cache_write += fresh
            output_tokens += OUTPUT_PER_CALL
            calls += 1
            prev_fps, prev_toks = fps, toks

        results[label] = {
            "proactive_prune_tokens": prune_tokens,
            "threshold_tokens": threshold,
            "full_compressions": full_compressions,
            "prune_commits": prune_commits,
            "prune_reclaimed_tokens": prune_reclaimed,
            # Informational only — NOT the decision metric (round-1 finding #1):
            # a full compression and a prune commit cost very different amounts.
            # See new_input/cache_read/cache_write/aux_* below for the real signal.
            "cache_breaks_total": full_compressions + prune_commits,
            "new_input_tokens": new_input,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "output_tokens": output_tokens,
            "compaction_aux_calls": aux_calls,
            "compaction_aux_input_tokens": aux_input,
            "compaction_aux_output_tokens": aux_output,
            "total_calls": calls + aux_calls,
            # Distinct counts (round-1 finding #2 fix) — each lost body counts once.
            "skill_reload_events": len(skill_bodies_lost),
            "reread_events": len(reread_bodies_lost),
            "skill_reload_events_cumulative_DEPRECATED": cumulative,
            "transcript_total_tokens": sum(estimate_tokens(m) for m in messages),
            "transcript_messages": len(messages),
        }
    return results


def main():
    candidates = {"disabled": 0, "prune_96k": 96_000, "prune_48k": 48_000}
    all_results = {}
    campaign = {
        label: dict(new_input_tokens=0, cache_read_tokens=0, cache_write_tokens=0, output_tokens=0,
                    compaction_aux_calls=0, compaction_aux_input_tokens=0, compaction_aux_output_tokens=0,
                    total_calls=0, full_compressions=0, prune_commits=0, skill_reload_events=0, reread_events=0)
        for label in candidates
    }
    for path in sys.argv[1:]:
        name = Path(path).stem
        print(f"== {name} ==", flush=True)
        r = simulate(path, candidates)
        all_results[name] = r
        for label, m in r.items():
            print(
                f"  {label:12s} new_in={m['new_input_tokens']:>9,} cache_rd={m['cache_read_tokens']:>11,} "
                f"aux_calls={m['compaction_aux_calls']}({m['compaction_aux_input_tokens']:,}in) "
                f"prune_commits={m['prune_commits']:3d} total_calls={m['total_calls']:4d} "
                f"skill_reload={m['skill_reload_events']:2d} reread={m['reread_events']:3d}"
            )
            for k in campaign[label]:
                campaign[label][k] += m[k]
    print("\n== CAMPAIGN TOTAL ==")
    for label, c in campaign.items():
        print(
            f"  {label:12s} new_in={c['new_input_tokens']:>10,} cache_rd={c['cache_read_tokens']:>12,} "
            f"aux_calls={c['compaction_aux_calls']:3d} aux_in={c['compaction_aux_input_tokens']:>10,} "
            f"full_comp={c['full_compressions']:3d} prune_commits={c['prune_commits']:3d} "
            f"total_calls={c['total_calls']:4d} skill_reload={c['skill_reload_events']:3d} reread={c['reread_events']:4d}"
        )
    out = Path(__file__).resolve().parent.parent / "results" / "prune-canary-t_b6dc22f0.json"
    out.write_text(json.dumps(all_results, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    campaign_out = Path(__file__).resolve().parent.parent / "results" / "prune-canary-t_b6dc22f0-campaign.json"
    campaign_out.write_text(json.dumps(campaign, indent=1), encoding="utf-8")
    print(f"wrote {campaign_out}")


if __name__ == "__main__":
    main()
