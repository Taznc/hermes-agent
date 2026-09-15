"""Regression for the cache-aware proactive-prune canary (t_b6dc22f0).

Behavior contract, not a real-transcript snapshot: on a synthetic but
representative transcript (mirrors evals/compaction/fixtures.py's shape),
enabling `proactive_prune_tokens` at either candidate threshold must not
produce FEWER cache-breaking events than leaving it disabled. Production's
only two cache-breaking paths per turn_preflight.py are (a) a full
ContextCompressor.compress() and (b) a committed
ContextCompressor.prune_tool_results_only() — every commit rewrites the
message list and calls archive_and_compact(), each one repricing the next
request's context at new-input rates under prompt caching. If a future
change makes pruning strictly cheaper in cache-break count, this test is
expected to start failing and should be revisited alongside the real
5-transcript evidence in evals/compaction/results/prune-canary-t_b6dc22f0.json
before flipping any default.
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


def test_proactive_prune_never_reduces_cache_breaks(tmp_path):
    # Tiny context window (explicit config override, the same knob production
    # uses) so full-compression AND prune thresholds both fire repeatedly
    # within a transcript this small, without needing a real
    # multi-hundred-K-token lineage.
    transcript_path = _write_transcript(tmp_path)
    candidates = {"disabled": 0, "prune_active_high": 20_000, "prune_active_low": 10_000}
    results = simulate(transcript_path, candidates, config_context_length=150_000)

    disabled_breaks = results["disabled"]["cache_breaks_total"]
    for label in ("prune_active_high", "prune_active_low"):
        assert results[label]["cache_breaks_total"] >= disabled_breaks, (
            f"{label} produced fewer cache breaks ({results[label]['cache_breaks_total']}) "
            f"than disabled ({disabled_breaks}) on the synthetic canary transcript — "
            "re-run the real-transcript canary before treating pruning as a win."
        )
        # Every commit is itself a cache break in addition to whatever full
        # compressions still fire; a non-trivial prune candidate must show
        # actual prune activity for the comparison to be meaningful.
        assert results[label]["prune_commits"] > 0


def test_proactive_prune_reclaims_tokens_when_it_commits(tmp_path):
    """Sanity: when prune_tool_results_only commits, it must actually shrink content."""
    transcript_path = _write_transcript(tmp_path)
    results = simulate(transcript_path, {"prune_active": 15_000}, config_context_length=150_000)
    r = results["prune_active"]
    assert r["prune_commits"] > 0
    assert r["prune_reclaimed_tokens"] > 0
