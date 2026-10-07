"""``fork.session.compaction_defer`` / ``fork.session.compaction_watermark`` (spec t_07c75c42 §4, §6)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import CompressionCommitFence
from hermes_fork.compaction import watermark as wm
from hermes_fork.gateway import methods_compaction as mc
from hermes_state import SessionDB
from tui_gateway.contracts import registry


def _flush():
    wm._get_executor().submit(lambda: None).result(timeout=5)


@pytest.fixture
def server(monkeypatch):
    from tui_gateway import server as srv

    monkeypatch.setattr(srv, "_sessions", {})
    return srv


@pytest.fixture
def db(tmp_path: Path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("KEY", source="tui")
    yield store
    _flush()
    store.close()


class _SpyDB:
    """Wrap a real SessionDB and record the thread every model_config write runs on."""

    def __init__(self, real: SessionDB):
        self._real = real
        self.write_threads: list[str] = []

    def patch_session_model_config(self, session_id, patch_):
        self.write_threads.append(threading.current_thread().name)
        return self._real.patch_session_model_config(session_id, patch_)

    def get_session_model_config_value(self, session_id, key, default=None):
        return self._real.get_session_model_config_value(session_id, key, default)

    def get_session(self, session_id):
        return self._real.get_session(session_id)


def _compressor(db_like, session_id="KEY") -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=1_000_000):
        cc = ContextCompressor(model="test/model", quiet_mode=True, threshold_percent=0.50, max_tokens=32_000)
        _ = cc.context_length
    cc._session_db, cc._session_id = db_like, session_id
    return cc


def _session(agent=None, key="KEY", **extra) -> dict:
    return {"agent": agent, "session_key": key, **extra}


def _call(server, name: str, params: dict, rid: int = 1) -> dict:
    return server.dispatch({"jsonrpc": "2.0", "id": rid, "method": name, "params": params})


def test_contract_shapes_are_registered(server):
    defer = registry.METHODS["fork.session.compaction_defer"]
    watermark = registry.METHODS["fork.session.compaction_watermark"]
    assert set(defer.params.model_fields) == {"session_id"}
    assert {"status", "reason", "watermark_tokens", "previous_threshold_tokens", "ceiling_tokens",
            "usable_tokens", "context_length"} == set(defer.result.model_fields)
    assert set(watermark.params.model_fields) == {"session_id", "action"}
    assert {"active", "watermark_tokens", "threshold_tokens", "base_threshold_tokens", "ceiling_tokens",
            "context_length"} == set(watermark.result.model_fields)


@pytest.mark.parametrize("method", ["fork.session.compaction_defer", "fork.session.compaction_watermark"])
def test_unknown_session_is_an_error(server, method):
    resp = _call(server, method, {"session_id": "nope"})
    assert resp["error"]["code"] == 4001


def test_unknown_param_is_rejected_at_admission(server):
    server._sessions["S"] = _session()
    resp = _call(server, "fork.session.compaction_defer", {"session_id": "S", "bogus": 1})
    assert resp["error"]["code"] == 4000


def test_defer_without_an_agent_is_not_running(server):
    server._sessions["S"] = _session(agent=None)
    assert _call(server, "fork.session.compaction_defer", {"session_id": "S"})["result"]["status"] == "not_running"


def test_defer_on_a_compute_host_session_is_unsupported(server, monkeypatch):
    server._sessions["S"] = _session(agent=None)
    monkeypatch.setattr(mc, "_uses_compute_host", lambda session: True)
    result = _call(server, "fork.session.compaction_defer", {"session_id": "S"})["result"]
    assert (result["status"], result["reason"]) == ("refused", "unsupported")


def test_defer_round_trip_and_write_lands_on_the_fork_executor(server, db):
    spy = _SpyDB(db)
    cc = _compressor(spy)
    cc.last_prompt_tokens = 490_000
    fence = CompressionCommitFence()
    cc._compression_telemetry_seed = {"trigger_source": "auto"}
    agent = SimpleNamespace(context_compressor=cc, api_mode="chat_completions",
                            _active_compression_commit_fence=fence)
    server._sessions["S"] = _session(agent=agent)
    result = _call(server, "fork.session.compaction_defer", {"session_id": "S"})["result"]
    assert result["status"] == "deferred" and result["watermark_tokens"] == 586_800
    assert fence.is_cancelled
    _flush()
    assert spy.write_threads and all(n.startswith("fork-compaction-wm") for n in spy.write_threads)
    assert threading.current_thread().name not in spy.write_threads
    assert db.get_session_model_config_value("KEY", wm.KEY)["tokens"] == 586_800

    got = _call(server, "fork.session.compaction_watermark", {"session_id": "S"})["result"]
    assert got["active"] is True and got["watermark_tokens"] == 586_800 and got["base_threshold_tokens"] == 484_000

    cleared = _call(server, "fork.session.compaction_watermark", {"session_id": "S", "action": "clear"})["result"]
    assert cleared["active"] is False and cleared["threshold_tokens"] == 484_000
    _flush()
    assert db.get_session_model_config_value("KEY", wm.KEY) is None


def test_get_lazily_loads_the_durable_record_after_restart(server, db):
    db.patch_session_model_config("KEY", {wm.KEY: {"v": 1, "tokens": 586_800, "context_length": 1_000_000,
                                                     "base_threshold_tokens": 484_000}})
    agent = SimpleNamespace(context_compressor=_compressor(db), api_mode="chat_completions")
    server._sessions["S"] = _session(agent=agent)
    got = _call(server, "fork.session.compaction_watermark", {"session_id": "S"})["result"]
    assert got["active"] is True and got["threshold_tokens"] == 586_800


def test_get_and_clear_without_an_agent_use_the_durable_row(server, db, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _owner(session):
        yield db

    monkeypatch.setattr(mc, "_owner_db", _owner)
    server._sessions["S"] = _session(agent=None)
    assert _call(server, "fork.session.compaction_watermark", {"session_id": "S"})["result"]["active"] is False
    db.patch_session_model_config("KEY", {wm.KEY: {"v": 1, "tokens": 586_800, "context_length": 1_000_000}})
    got = _call(server, "fork.session.compaction_watermark", {"session_id": "S"})["result"]
    assert got["active"] is True and got["watermark_tokens"] == 586_800 and got["threshold_tokens"] is None
    _call(server, "fork.session.compaction_watermark", {"session_id": "S", "action": "clear"})
    _flush()
    assert db.get_session_model_config_value("KEY", wm.KEY) is None
