import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tui_gateway import compute_host, server
from tui_gateway.compute_host import ComputeHost, _default_workers
from tui_gateway.host_supervisor import (
    MUTATOR_ROUTE_TABLE,
    HostSupervisor,
    append_log_record,
)


def _json_lines(out: io.StringIO) -> list[dict]:
    frames = []
    for line in out.getvalue().splitlines():
        if line.strip():
            frames.append(json.loads(line))
    return frames


def _wait_for_frame(out: io.StringIO, predicate, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in _json_lines(out):
            if predicate(frame):
                return frame
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for frame; saw={_json_lines(out)}")


def test_compute_host_workers_inherit_tui_pool_env_or_8(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_RPC_POOL_WORKERS", raising=False)
    monkeypatch.delenv("HERMES_COMPUTE_HOST_WORKERS", raising=False)
    assert _default_workers() == 8

    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "11")
    assert _default_workers() == 11

    # Dead-RC tombstone: malformed env falls back to 8, not the old except-branch 4.
    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "not-an-int")
    assert _default_workers() == 8


def test_compute_host_routes_clarify_response_to_child_pending_registry(monkeypatch):
    """Interactive answers are handled in the process that owns `_pending`."""
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "host-clarify"
    server._sessions[sid] = {"history_lock": threading.Lock()}
    calls = []
    monkeypatch.setitem(
        server._methods,
        "clarify.respond",
        lambda rid, params: calls.append((rid, dict(params))) or {"result": {"status": "ok"}},
    )

    try:
        host._handle_respond(
            {
                "sid": sid,
                "request_id": "relay-response",
                "params": {"request_id": "clarify-request", "answer": "yes"},
            }
        )
        assert calls == [("relay-response", {"request_id": "clarify-request", "answer": "yes"})]
        frame = _json_lines(out)[-1]
        assert frame == {
            "type": "respond.ack",
            "sid": sid,
            "request_id": "relay-response",
            "response": {"result": {"status": "ok"}},
            "host_ns": frame["host_ns"],
        }
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_routes_clarify_explanation_to_child_live_session(monkeypatch):
    """Help runs in the child, which owns the pending Event and non-mirrored history."""
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "host-explain"
    live_history = [{"role": "user", "content": "host-only context"}]
    server._sessions[sid] = {"history": live_history, "history_lock": threading.Lock()}
    calls = []
    monkeypatch.setitem(
        server._methods,
        "clarify.explain",
        lambda rid, params: calls.append((rid, dict(params), server._sessions[sid]["history"])) or {
            "result": {"status": "complete", "explanation_id": "host-help"}},
    )

    try:
        host._handle_explain({
            "sid": sid, "request_id": "relay-explain",
            "params": {"version": 1, "request_id": "clarify-request", "choice": "yes"},
        })
        _wait_for_frame(out, lambda frame: frame.get("type") == "explain.ack")
        assert calls == [(
            "relay-explain", {"version": 1, "request_id": "clarify-request", "choice": "yes"},
            live_history,
        )]
        frame = _json_lines(out)[-1]
        assert frame["type"] == "explain.ack"
        assert frame["sid"] == sid
        assert frame["request_id"] == "relay-explain"
        assert frame["response"] == {"result": {"status": "complete", "explanation_id": "host-help"}}
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_supervisor_explain_delivers_host_ack(monkeypatch, tmp_path):
    """The supervisor recognizes the dedicated explanation frame, not a control mutation."""
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(supervisor, "start", lambda: None)
    sent = []

    def send(frame):
        sent.append(dict(frame))
        supervisor._handle_host_frame({
            "type": "explain.ack", "request_id": frame["request_id"], "sid": frame["sid"],
            "response": {"result": {"status": "complete", "explanation_id": "help-1"}},
        })

    monkeypatch.setattr(supervisor, "_send_frame", send)
    result = supervisor.explain("s1", {"version": 1, "request_id": "clarify-1"})
    assert result["response"]["result"]["explanation_id"] == "help-1"
    assert sent[0]["type"] == "explain"
    assert sent[0]["params"] == {"version": 1, "request_id": "clarify-1"}


def test_supervisor_keeps_concurrent_explanation_correlations_distinct(monkeypatch, tmp_path):
    """Concurrent help frames retain their own supervisor waiter and host response."""
    host = ComputeHost(heartbeat_secs=0)
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    sid = "host-concurrent-help"
    server._sessions[sid] = {"history_lock": threading.Lock()}
    emitted = []

    def emit(frame):
        emitted.append(dict(frame))
        supervisor._handle_host_frame(frame)

    def explain(_rid, params):
        text = str(params["follow_up"])
        return {"result": {"status": "complete", "explanation_id": f"help-{text}"}}

    monkeypatch.setattr(host, "emit", emit)
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "_send_frame", host.handle_frame)
    monkeypatch.setitem(server._methods, "clarify.explain", explain)
    try:
        replies = {}
        threads = [
            threading.Thread(
                target=lambda text=text: replies.setdefault(
                    text, supervisor.explain(sid, {"request_id": "clarify-1", "follow_up": text}, timeout=1)),
            )
            for text in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive()
        assert {reply["response"]["result"]["explanation_id"] for reply in replies.values()} == {
            "help-first", "help-second",
        }
        assert len([frame for frame in emitted if frame["type"] == "explain.ack"]) == 2
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_supervisor_interrupt_cancels_inflight_host_explanation_without_late_ack(monkeypatch, tmp_path):
    """The child reader accepts interrupt frames while its explanation worker is blocked."""
    host = ComputeHost(heartbeat_secs=0)
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    sid = "host-explain-race"
    started = threading.Event()
    release = threading.Event()
    emitted = []
    server._sessions[sid] = {
        "history": [], "history_lock": threading.Lock(), "session_key": sid, "running": False,
    }
    pending = threading.Event()
    with server._prompt_lock:
        server._pending["clarify-race"] = (sid, pending)
        server._pending_prompt_payloads["clarify-race"] = ("clarify.request", {
            "request_id": "clarify-race", "question": "Continue?", "choices": ["yes", "no"],
        })

    def emit(frame):
        emitted.append(dict(frame))
        supervisor._handle_host_frame(frame)

    def explain(_session, _prompt):
        started.set()
        assert release.wait(2)
        return "too late"

    monkeypatch.setattr(host, "emit", emit)
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "_send_frame", host.handle_frame)
    monkeypatch.setattr(server, "_spawn_clarify_explanation", explain)
    try:
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "reply", supervisor.explain(sid, {
                    "version": 1, "session_id": sid, "request_id": "clarify-race",
                }, timeout=1)),
        )
        thread.start()
        assert started.wait(1)
        interrupt = threading.Thread(
            target=lambda: supervisor.interrupt(sid, request_id="interrupt-race"),
        )
        interrupt.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(
            frame["type"] == "interrupt.ack" for frame in emitted
        ):
            time.sleep(0.01)
        assert any(frame["type"] == "interrupt.ack" for frame in emitted)
        release.set()
        interrupt.join(1)
        assert not interrupt.is_alive()
        thread.join(1)
        assert not thread.is_alive()
        assert result["reply"]["type"] == "explain.error"
        assert [frame["type"] for frame in emitted] == ["interrupt.ack", "explain.error"]
        assert pending.is_set()
        assert not any(frame["type"] == "rpc" for frame in emitted)
    finally:
        server._sessions.pop(sid, None)
        with server._prompt_lock:
            server._pending.pop("clarify-race", None)
            server._pending_prompt_payloads.pop("clarify-race", None)
        host.close()


def test_mutator_route_table_matches_prd_inventory():
    assert MUTATOR_ROUTE_TABLE == {
        "prompt.submit": "turn-path",
        "session.interrupt": "turn-path",
        "reload.mcp": "run-concurrent",
        "session.save": "run-concurrent",
        "session.compress": "idle-gated",
        "prompt.submit.truncate": "idle-gated",
        "slash.model": "idle-gated",
        "slash.personality": "idle-gated",
        "slash.prompt": "idle-gated",
        "slash.compress": "idle-gated",
        "session.reset": "idle-gated",
        "session.history.reload": "idle-gated",
        "slash.retry": "idle-gated",
    }


def test_append_log_record_single_write_lines(tmp_path):
    path = tmp_path / "agent.log"

    def writer(i: int) -> None:
        append_log_record(path, f"line-{i:03d}-" + ("x" * 2000))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 32
    assert sorted(line.split("-", 2)[1] for line in lines) == [f"{i:03d}" for i in range(32)]
    assert all(line.endswith("x" * 2000) for line in lines)


def test_supervisor_startup_reconcile_pid_reuse_guard(tmp_path, monkeypatch):
    registry = tmp_path / "dashboard-compute-host.json"
    registry.write_text(json.dumps({"host_pid": os.getpid(), "boot_id": "stale"}), encoding="utf-8")

    killed: list[int] = []
    supervisor = HostSupervisor(registry_path=registry, argv=[sys.executable, "-c", ""], autostart=False)
    monkeypatch.setattr(supervisor, "_pid_matches_compute_host", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_terminate_pid", lambda pid, **_kw: killed.append(pid))

    result = supervisor.reconcile_startup_orphan()

    assert result == "pid-reuse-ignored"
    assert killed == []
    assert not registry.exists()


def _make_compress_host_session(events: list) -> dict:
    class _Agent:
        model = "host-model"
        provider = "host-provider"
        tools = []
        _cached_system_prompt = ""
        session_input_tokens = 1
        session_output_tokens = 1
        session_prompt_tokens = 1
        session_completion_tokens = 1
        session_total_tokens = 2
        session_api_calls = 1
        session_id = "rotated-id"

    agent = _Agent()
    agent.context_compressor = type("ContextEngineStub", (), {})()
    agent.context_compressor.on_session_start = (
        lambda *_args, **_kwargs: events.append("notify")
    )
    return {
        "agent": agent,
        "session_key": "before-key",
        "history": [
            {"role": "user", "content": "before"},
            {"role": "assistant", "content": "before"},
        ],
        "history_lock": threading.Lock(),
        "history_version": 2,
        "running": False,
        "manual_compression_lock": threading.Lock(),
    }


def _record_finalize(monkeypatch, events: list[str], *sids: str) -> None:
    """Give ``flush_all_sessions`` sessions and record which ones finalize."""
    keys = sids or ("s1",)
    monkeypatch.setattr(
        server,
        "_sessions",
        {sid: {"session_key": sid} for sid in keys},
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_finalize_session",
        lambda _session, end_reason="tui_close": events.append(
            f"finalize:{_session['session_key']}:{end_reason}"
        ),
        raising=False,
    )


def _register_turn(host: ComputeHost, fn, sid: str = "s1") -> None:
    """Submit a turn exactly the way ``_handle_turn_start`` does."""
    host._track_turn_future(host._executor.submit(fn), sid)


def test_shutdown_drains_in_flight_turn_before_finalizing_sessions(monkeypatch):
    events: list[str] = []
    _record_finalize(monkeypatch, events)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    running = threading.Event()

    def _turn() -> None:
        running.set()
        time.sleep(0.3)
        events.append("turn_end")

    _register_turn(host, _turn, sid="s1")
    assert running.wait(timeout=5.0)

    host.shutdown(reason="sigterm", wait=3.0)

    # ``_finalize_session`` latches on ``session["_finalized"]``, so its single
    # run has to observe the finished turn or the tail is unpersistable. A turn
    # that *did* drain must still finalize — the live-turn skip must not
    # over-reach into sessions whose work is done.
    assert events == ["turn_end", "finalize:s1:compute_host_sigterm"]

    # The done-callback still has to remove the entry now that the container is
    # a dict: ``set.discard`` was a valid bare callback, ``dict.pop`` is not.
    deadline = time.monotonic() + 2.0
    while host._turn_futures and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host._turn_futures == {}, "in-flight turns must not accumulate"


def test_shutdown_retains_a_live_turns_session_when_the_drain_deadline_expires(monkeypatch):
    wait = 1.0
    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="sigterm", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    # ``_finalize_session`` is one-shot, and the ``shutdown(wait=False)`` that
    # follows does not join the turn. Spending "live"'s single latch mid-turn
    # would leave it permanently un-finalizable and release its active-session
    # lease out from under running work — the same lifecycle race the drain
    # exists to close, just moved past the deadline. It is retained unfinalized
    # for recovery instead. A turn outliving the window must not cost the flush
    # for anyone else, so "idle" still finalizes in the same pass.
    assert events == ["finalize:idle:compute_host_sigterm"]
    assert elapsed < wait


def test_shutdown_retains_live_sessions_within_the_stdin_closed_budget(monkeypatch):
    """The tightest real budget any caller uses is ``wait=2.0``.

    ``run_host`` finalizes through ``host.shutdown(reason="stdin_closed",
    wait=2.0)``, which is where the reserve — ``wait`` minus
    ``min(_FLUSH_RESERVE_SECS, wait / 2)`` — has the least room to work with.
    The retain-live-sessions rule must hold there without costing the flush for
    idle sessions and without pushing the call past the budget the supervisor's
    kill escalation is timed against.
    """
    wait = 2.0
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="stdin_closed", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_stdin_closed"]
    assert elapsed >= drain_budget - 1e-6, "the drain must use its full window"
    assert elapsed < wait


def test_shutdown_drain_sleep_never_overshoots_the_reserve(monkeypatch):
    """The drain's per-tick sleep must be bounded by the time left to it.

    A flat tick overshoots the drain deadline by up to one tick, eating the
    reserve held back for ``flush_all_sessions``; for a small ``wait`` that is
    the whole reserve. Asserting on the *requested* sleep totals rather than on
    wall-clock keeps this deterministic: each sleep is clamped to the remaining
    time, so the sum can never exceed the drain budget however the scheduler
    interleaves.
    """
    wait = 0.34
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "idle")

    slept: list[float] = []
    real_sleep = time.sleep

    def _recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(compute_host.time, "sleep", _recording_sleep)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        host.shutdown(reason="sigterm", wait=wait)
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_sigterm"]
    assert slept, "the drain loop should have ticked at least once"
    assert sum(slept) <= drain_budget + 1e-6
