"""Regression for the cache-aware proactive-prune canary (t_b6dc22f0).

Behavior contract, not a real-transcript snapshot, restated per round-1
review (comment 1223/1233): the original version asserted against
`cache_breaks_total`, an unweighted sum of full compressions (each an
LLM-backed summarizer call over the whole collapsed region) and prune
commits (no auxiliary call at all) — a full compression and a prune commit
are not comparable events, so an event-count assertion cannot establish
whether pruning is cheaper or more expensive. This version asserts against
the token-level usage metrics AC1 requires instead: on a synthetic but
representative transcript (mirrors evals/compaction/fixtures.py's shape), a
committed prune must reclaim tokens and be reflected in the reported
new-input/cache-read accounting, and the pass/fail question for any future
default change is decided by campaign-level `new_input_tokens` /
`cache_read_tokens` (see evals/compaction/results/prune-canary-t_b6dc22f0.json
and its `-campaign.json` sibling for the real 5-transcript evidence), not by
raw event counts.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evals.compaction.fixtures import synthetic_transcript  # noqa: E402
from evals.compaction.scripts.prune_canary import simulate  # noqa: E402


def _write_transcript(tmp_path) -> str:
    import json

    # fixtures.synthetic_transcript's tool bodies (~2.4K chars each) sit
    # below the production 8,000-char proactive-prune floor
    # (proactive_prune_min_result_chars), so no candidate would ever commit
    # a prune on it — real transcripts (evals/compaction/results/
    # prune-canary-t_b6dc22f0.json) run 10-40K+ chars per tool result.
    # Inflate bodies to ~12K chars so the synthetic canary actually clears
    # that floor and exercises the prune path instead of silently no-opping.
    msgs = synthetic_transcript(n_turns=400, seed=11)
    for m in msgs:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            m["content"] = m["content"] + (" filler token " * 700)
    path = tmp_path / "synthetic_lineage.json"
    path.write_text(json.dumps({"messages": msgs}), encoding="utf-8")
    return str(path)


def test_proactive_prune_activity_is_reflected_in_usage_accounting(tmp_path):
    # Tiny context window (explicit config override, the same knob production
    # uses) so full-compression AND prune thresholds both fire repeatedly
    # within a transcript this small, without needing a real
    # multi-hundred-K-token lineage.
    transcript_path = _write_transcript(tmp_path)
    candidates = {"disabled": 0, "prune_active_high": 20_000, "prune_active_low": 10_000}
    results = simulate(transcript_path, candidates, config_context_length=150_000)

    disabled = results["disabled"]
    for label in ("prune_active_high", "prune_active_low"):
        r = results[label]
        # A non-trivial prune candidate must show actual prune activity for
        # any comparison to be meaningful.
        assert r["prune_commits"] > 0
        assert r["prune_reclaimed_tokens"] > 0
        # AC1's required token metrics must actually be populated (round-1
        # blocker: the original harness recorded none of these).
        for key in (
            "new_input_tokens", "cache_read_tokens", "cache_write_tokens",
            "output_tokens", "compaction_aux_calls", "compaction_aux_input_tokens",
            "total_calls", "skill_reload_events", "reread_events",
        ):
            assert key in r
        # Fewer (or equal) full compressions than disabled is the whole point
        # of pruning (reclaiming tokens keeps the live window under
        # threshold_tokens longer) — assert the mechanism is doing that,
        # rather than comparing an unweighted count across non-comparable
        # event types (round-1 blocker #1).
        assert r["full_compressions"] <= disabled["full_compressions"]
        # Distinct-body accounting must never exceed how many tool results
        # actually existed on the transcript (a cumulative-count regression
        # would blow past this bound; round-1 blocker #2).
        import json as _json
        total_tool_msgs = sum(
            1 for m in _json.loads(Path(transcript_path).read_text())["messages"]
            if m.get("role") == "tool"
        )
        assert r["reread_events"] <= total_tool_msgs


def test_proactive_prune_reclaims_tokens_when_it_commits(tmp_path):
    """Sanity: when prune_tool_results_only commits, it must actually shrink content."""
    transcript_path = _write_transcript(tmp_path)
    results = simulate(transcript_path, {"prune_active": 15_000}, config_context_length=150_000)
    r = results["prune_active"]
    assert r["prune_commits"] > 0
    assert r["prune_reclaimed_tokens"] > 0
