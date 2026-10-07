"""``defer_active_compaction`` against a real ``CompressionCommitFence`` on a real ``AIAgent`` (spec §4, §6.4).

R4: with the rollback removed, ``test_commit_in_flight_is_too_late_and_rolls_back`` fails.
R5: with the defer implemented as ``agent.interrupt(hard_cancel=True)``, ``test_pending_defer_has_no_hard_
interrupt_fan_out`` fails (see ``test_hard_interrupt_would_fail_the_fan_out_check``, kept as the standing proof).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import agent.interrupt_control as ic
from agent.conversation_compression import CompressionCommitFence
from hermes_fork.compaction import defer as defer_mod
from hermes_fork.compaction import watermark as wm
from hermes_fork.compaction.defer import defer_active_compaction
from hermes_state import SessionDB

BASE = 484_000


@pytest.fixture
def agent(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("DEFER", source="tui")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
            patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        from run_agent import AIAgent

        a = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                    quiet_mode=True, session_db=db, session_id="DEFER", skip_context_files=True, skip_memory=True)
        cc = a.context_compressor
        cc.max_tokens = 32_000
        cc.threshold_percent = cc._base_threshold_percent = 0.50
        cc._aux_context_ceiling = None
        cc._threshold_tokens = None
        _ = cc.context_length
    assert cc.threshold_tokens == BASE
    cc.last_prompt_tokens = 490_000
    yield a
    db.close()


def _arm(agent, trigger: str = "auto") -> CompressionCommitFence:
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    agent.context_compressor._compression_telemetry_seed = {"trigger_source": trigger, "attempt_id": "x",
                                                            "session_id": "DEFER"}
    return fence


@pytest.fixture
def worker_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(ic, "_ic_signal_tool_workers", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(ic, "_set_interrupt", lambda *a, **k: calls.append((a, k)))
    return calls


def _assert_no_fan_out(agent, worker_spy):
    assert not getattr(agent, "_interrupt_requested", False)
    hard = getattr(agent, "_hard_interrupt_requested", None)
    assert hard is None or not hard.is_set()
    assert worker_spy == []


def test_pending_defer_cancels_the_fence_and_raises(agent):
    fence = _arm(agent)
    result = defer_active_compaction(agent)
    assert result["status"] == "deferred" and result["reason"] is None
    assert result["watermark_tokens"] == 586_800 and result["previous_threshold_tokens"] == BASE
    assert fence.is_cancelled
    assert agent.context_compressor.threshold_tokens == 586_800
    wm._get_executor().submit(lambda: None).result(timeout=5)
    assert agent._session_db.get_session_model_config_value("DEFER", wm.KEY)["tokens"] == 586_800


def test_pending_defer_has_no_hard_interrupt_fan_out(agent, worker_spy):
    _arm(agent)
    assert defer_active_compaction(agent)["status"] == "deferred"
    _assert_no_fan_out(agent, worker_spy)


def test_hard_interrupt_would_fail_the_fan_out_check(agent, worker_spy):
    """R5 standing proof: the fan-out check discriminates a defer implemented as hard_interrupt()."""
    _arm(agent)
    agent.interrupt(hard_cancel=True)
    with pytest.raises(AssertionError):
        _assert_no_fan_out(agent, worker_spy)


def test_watermark_is_installed_before_the_cancel(agent, monkeypatch):
    fence = _arm(agent)
    seen = {}
    real = CompressionCommitFence.try_cancel_before_commit

    def spy(self):
        seen["threshold_at_cancel"] = agent.context_compressor.threshold_tokens
        return real(self)

    monkeypatch.setattr(CompressionCommitFence, "try_cancel_before_commit", spy)
    assert defer_active_compaction(agent)["status"] == "deferred"
    assert seen["threshold_at_cancel"] == 586_800 and fence.is_cancelled


def test_commit_in_flight_is_too_late_and_rolls_back(agent):
    fence = _arm(agent)
    assert fence.begin_commit()
    try:
        result = defer_active_compaction(agent)
    finally:
        fence.finish_commit()
    assert result["status"] == "too_late"
    assert not fence.is_cancelled
    assert agent.context_compressor.threshold_tokens == BASE
    assert vars(agent.context_compressor).get(wm._SLOT) is None


def test_too_late_restores_the_exact_prior_record(agent):
    cc = agent.context_compressor
    prior = wm.plan_raise(cc).record
    wm.install_watermark(cc, prior)
    assert cc.threshold_tokens == 586_800
    fence = _arm(agent)
    # begin_commit lost the race after planning: simulate by winning the commit inside the cancel.
    real = CompressionCommitFence.try_cancel_before_commit

    def commit_first(self):
        assert self.begin_commit()
        self.finish_commit()
        return real(self)  # commit already started -> False

    with patch.object(CompressionCommitFence, "try_cancel_before_commit", commit_first):
        result = defer_active_compaction(agent)
    assert result["status"] == "too_late"
    assert vars(cc)[wm._SLOT] == prior
    assert cc.threshold_tokens == 586_800


def test_lock_held_without_commit_is_busy_within_the_bound(agent, monkeypatch):
    fence = _arm(agent)
    fence._lock.acquire()
    try:
        started = time.monotonic()
        result = defer_active_compaction(agent)
        elapsed = time.monotonic() - started
    finally:
        fence._lock.release()
    assert result["status"] == "busy"
    assert elapsed < 0.25 + 0.2
    assert not fence.is_cancelled
    assert agent.context_compressor.threshold_tokens == BASE


@pytest.mark.parametrize("trigger, reason", [("manual", "manual"), ("overflow", "overflow")])
def test_manual_and_overflow_are_refused(agent, trigger, reason):
    fence = _arm(agent, trigger)
    result = defer_active_compaction(agent)
    assert (result["status"], result["reason"]) == ("refused", reason)
    assert not fence.is_cancelled
    assert agent.context_compressor.threshold_tokens == BASE


def test_codex_app_server_is_refused(agent):
    fence = _arm(agent)
    agent.api_mode = "codex_app_server"
    result = defer_active_compaction(agent)
    assert (result["status"], result["reason"]) == ("refused", "unsupported")
    assert not fence.is_cancelled


def test_at_ceiling_is_refused_without_cancelling(agent):
    cc = agent.context_compressor
    wm.install_watermark(cc, {"v": 1, "tokens": 871_200, "context_length": 1_000_000, "session_id": "DEFER"})
    fence = _arm(agent)
    result = defer_active_compaction(agent)
    assert (result["status"], result["reason"]) == ("refused", "at_ceiling")
    assert not fence.is_cancelled


def test_no_fence_is_not_running(agent, worker_spy):
    assert defer_active_compaction(agent)["status"] == "not_running"
    assert agent.context_compressor.threshold_tokens == BASE
    _assert_no_fan_out(agent, worker_spy)


def test_already_cancelled_fence_is_not_running(agent):
    fence = _arm(agent)
    fence.cancel_before_commit()
    assert defer_active_compaction(agent)["status"] == "not_running"


def test_concurrent_defers_raise_once(agent):
    fence = _arm(agent)
    results = []
    threads = [threading.Thread(target=lambda: results.append(defer_active_compaction(agent)["status"]))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert sorted(results) == ["deferred", "not_running", "not_running", "not_running"]
    assert fence.is_cancelled
    assert agent.context_compressor.threshold_tokens == 586_800


def test_module_constants_bound_the_wait():
    assert defer_mod.CANCEL_ATTEMPTS * defer_mod.CANCEL_RETRY_SLEEP <= 0.25
