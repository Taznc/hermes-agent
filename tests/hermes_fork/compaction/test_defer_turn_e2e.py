"""End to end: "Keep full context" during a real automatic preflight compaction (spec §1.1, §1.3, §6.5).

Drives upstream's real preflight (``agent.turn_context_compaction._preflight_compression``) on a real
``AIAgent`` + ``SessionDB``: the owned ``_compress_context`` wrapper publishes its fence, the fake summariser
blocks until that fence is cancelled, and ``defer_active_compaction`` runs from another thread (the gateway
RPC thread in production). ``out.messages`` is what ``build_turn_context`` hands to the provider call.

R2: ``test_without_the_raise_the_cancelled_attempt_rearms`` runs the same flow with the raise monkeypatched
out (fence cancel only) and shows the next ``should_compress()`` at the same token count is True.
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import turn_context as tc
from agent.turn_context import PreflightCompressionTimedOut
from agent.turn_context_compaction import CompactionOutcome, _preflight_compression
from hermes_fork.compaction import defer as defer_mod
from hermes_fork.compaction import watermark as wm
from hermes_fork.compaction.defer import defer_active_compaction
from hermes_state import SessionDB

SID = "DEFER_E2E"


@pytest.fixture
def env(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SID, source="tui")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), \
            patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        from run_agent import AIAgent

        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, session_db=db, session_id=SID, skip_context_files=True, skip_memory=True)
        cc = agent.context_compressor
        cc.max_tokens = 32_000
        cc.threshold_percent = cc._base_threshold_percent = 0.50
        cc._aux_context_ceiling = None
        cc._threshold_tokens = None
        _ = cc.context_length
    assert cc.threshold_tokens == 484_000
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._cached_system_prompt = "sys"
    cc.last_prompt_tokens = 490_000
    yield db, agent
    db.close()


def _messages():
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(40)]


def _run_preflight_with_defer(agent, monkeypatch, *, request_tokens: int):
    """Run preflight with a summariser that blocks until its fence is cancelled; defer from another thread."""
    def _tokens(a, messages, system_prompt):
        a._request_pressure_anchored = True
        return request_tokens

    monkeypatch.setattr(tc, "_preflight_request_tokens", _tokens)
    started = threading.Event()
    calls = {"n": 0}

    def _blocking_summary(messages, **_kwargs):
        calls["n"] += 1
        started.set()
        fence = vars(agent).get("_active_compression_commit_fence")
        for _ in range(500):
            if fence is not None and fence.is_cancelled:
                break
            threading.Event().wait(0.01)
        return [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}, messages[-1]]

    agent.context_compressor.compress = _blocking_summary
    result: dict = {}

    def _click():
        assert started.wait(timeout=10), "summary never started"
        result["defer"] = defer_active_compaction(agent)

    clicker = threading.Thread(target=_click)
    clicker.start()
    original = _messages()
    out = CompactionOutcome(messages=copy.deepcopy(original), active_system_prompt="sys",
                            conversation_history=copy.deepcopy(original), current_turn_user_idx=len(original) - 2)
    try:
        _preflight_compression(agent, out, "sys", original[-2]["content"], "task")
    finally:
        clicker.join(timeout=10)
    return original, out, result.get("defer"), calls


def test_defer_keeps_the_full_transcript_and_does_not_rearm(env, monkeypatch):
    db, agent = env
    original, out, defer, calls = _run_preflight_with_defer(agent, monkeypatch, request_tokens=490_000)
    assert defer is not None and defer["status"] == "deferred", defer
    assert calls["n"] == 1
    # The provider call gets the uncompressed transcript; no PreflightCompressionTimedOut was raised.
    assert out.messages == original
    assert out.compressed is True and out.blocked is True  # one cancelled pass, no retry this turn
    # No rotation, no split: the session row is still the live one and has no compression child.
    assert db.get_session(SID)["end_reason"] is None
    assert db.find_live_compression_child(SID) is None
    # Raised watermark: the same token count no longer triggers; the next crossing does.
    cc = agent.context_compressor
    assert cc.threshold_tokens == 586_800
    assert cc.should_compress(490_000) is False
    assert cc.should_compress(586_800) is True
    wm._get_executor().submit(lambda: None).result(timeout=5)
    assert db.get_session_model_config_value(SID, wm.KEY)["tokens"] == 586_800


def test_without_the_raise_the_cancelled_attempt_rearms(env, monkeypatch):
    """R2 standing proof: fence cancel alone re-arms immediately, so the raise is necessary."""
    _db, agent = env
    monkeypatch.setattr(defer_mod, "install_watermark", lambda cc, record: None)
    monkeypatch.setattr(defer_mod, "schedule_durable_write", lambda cc, record: None)
    original, out, defer, _calls = _run_preflight_with_defer(agent, monkeypatch, request_tokens=490_000)
    assert defer is not None and defer["status"] == "deferred"
    assert out.messages == original
    assert agent.context_compressor.should_compress(490_000) is True


def test_request_above_the_window_still_fails_closed(env, monkeypatch):
    """§1.3(3): a turn whose preflight request is already above the window is not sent uncompressed."""
    _db, agent = env
    with pytest.raises(PreflightCompressionTimedOut, match="could not bring this session under"):
        _run_preflight_with_defer(agent, monkeypatch, request_tokens=1_200_000)
