"""Dispatcher liveness must not be inferred from worker activity logs."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

import pytest

from gateway import run_startup, shutdown_watchdog
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli.kanban_db_dispatch import DispatchResult
from tests.gateway.test_kanban_dispatcher_standby import harness  # noqa: F401


class Runner(GatewayKanbanWatchersMixin, run_startup.GatewayStartupMixin):
    _running = True


@pytest.fixture
def short_socket(monkeypatch):
    # pytest's descriptive tmp paths can exceed sockaddr_un.sun_path.
    with tempfile.TemporaryDirectory(prefix="kb-heartbeat-") as tmp:
        path = Path(tmp) / "tick.sock"
        monkeypatch.setattr(shutdown_watchdog, "get_loop_tick_socket_path", lambda home=None: path)
        yield path


@pytest.mark.asyncio
async def test_real_heartbeat_distinguishes_idle_ticks_from_hung_dispatch(harness, monkeypatch, short_socket):
    h = harness
    runner = Runner()
    beats = asyncio.Queue()
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    write = shutdown_watchdog.write_loop_heartbeat

    def capture_write(**kwargs):
        path = write(**kwargs)
        loop.call_soon_threadsafe(beats.put_nowait, json.loads(path.read_text()))
        return path

    async def next_dispatch_beat(phase):
        async def matching():
            while True:
                payload = await beats.get()
                health = payload.get("kanban_dispatcher", {})
                if health.get("phase") == phase:
                    return payload
        return await asyncio.wait_for(matching(), 5)

    monkeypatch.setattr(shutdown_watchdog, "write_loop_heartbeat", capture_write)
    monkeypatch.setattr(run_startup, "DEFAULT_HEARTBEAT_INTERVAL_S", 1)
    watcher = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        _, resume = await h.clock.paused(watcher)
        resume.set_result(None)
        _, resume = await h.clock.paused(watcher)  # real empty SQLite dispatch finished
        runner._start_loop_heartbeat_task()
        first = await asyncio.wait_for(beats.get(), 5)
        assert "kanban_dispatcher" in first, "heartbeat omits dispatcher liveness on idle ticks"
        idle = first["kanban_dispatcher"]
        assert idle["phase"] == "sleeping"
        assert idle["last_outcome"] == "idle"
        assert idle["completed_ticks"] == 1
        assert idle["last_success_at"] >= idle["last_attempt_at"]
        assert idle["spawned"] == 0

        def stuck_dispatch(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert finish.wait(10), "test must release the blocked thread"
            return DispatchResult()

        h.dispatch.side_effect = stuck_dispatch
        # The production sleep is sliced into one-second sleeps by the harness.
        resume.set_result(None)
        _, resume = await h.clock.paused(watcher)
        resume.set_result(None)
        await asyncio.wait_for(entered.wait(), 5)
        second = await next_dispatch_beat("dispatch")
        health = second["kanban_dispatcher"]
        assert second["monotonic"] > first["monotonic"]  # gateway itself still ticks
        assert health["completed_ticks"] == idle["completed_ticks"]
        assert health["last_success_at"] == idle["last_success_at"]
        assert health["last_attempt_at"] >= idle["last_attempt_at"]
        assert health["phase_elapsed_seconds"] > 0
        if os.name == "posix":
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(short_socket), 5)
        else:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", second["loop_tick_tcp_port"]), 5)
        try:
            assert await asyncio.wait_for(reader.read(1), 5) == b"1"
        finally:
            writer.close()
            await writer.wait_closed()
        finish.set()
        _, resume = await h.clock.paused(watcher)
        recovered = (await next_dispatch_beat("sleeping"))["kanban_dispatcher"]
        assert recovered["completed_ticks"] == 2
        assert recovered["last_success_at"] > idle["last_success_at"]

        # The per-board wrapper catches this exception. A heartbeat may report
        # a completed error tick, but must not promote it to successful liveness.
        h.dispatch.side_effect = RuntimeError("injected board failure")
        resume.set_result(None)
        _, resume = await h.clock.paused(watcher)
        resume.set_result(None)
        await h.clock.paused(watcher)
        failed = (await next_dispatch_beat("sleeping"))["kanban_dispatcher"]
        assert failed["completed_ticks"] == 3
        assert failed["last_outcome"] == "error"
        assert failed["error_boards"] == 1
        assert failed["last_success_at"] == recovered["last_success_at"]
    finally:
        finish.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        heartbeat = getattr(runner, "_loop_heartbeat_task", None)
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        # Any shielded thread must finish before teardown releases its lock.
        for _ in range(100):
            if not runner._owns_kanban_dispatcher_lock():
                break
            await asyncio.sleep(0.01)
        runner._release_kanban_dispatcher_lock()


@pytest.mark.parametrize("result,ready,outcome", [
    (DispatchResult(), False, "idle"),
    (DispatchResult(), True, "no_spawn"),
    (DispatchResult(dispatch_paused={"reason": "private-detail"}), False, "guard_deferred"),
    (DispatchResult(skipped_locked=True), True, "guard_deferred"),
    (DispatchResult(skipped_per_profile_capped=[("secret-task", "secret-profile", 4)]), True, "no_capacity"),
    (DispatchResult(memory_pressure="critical"), True, "no_capacity"),
    (DispatchResult(spawned=[("secret-task", "secret-profile", "/private/path")]), True, "spawned"),
    (None, True, "error"),
])
def test_tick_outcomes_stalls_and_logs_are_bounded(result, ready, outcome, monkeypatch, caplog):
    from gateway import kanban_dispatcher_health as telemetry

    clock = [1000.0]
    monkeypatch.setattr(telemetry, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
    ))
    caplog.set_level("DEBUG", logger="gateway.run")
    health = telemetry.DispatcherHealth(interval=60)
    health.begin_tick()
    health.phase("dispatch")
    clock[0] += 181
    snapshot = health.snapshot()
    assert snapshot["stalled"] is True
    assert snapshot["last_success_at"] is None
    assert snapshot["completed_ticks"] == 0
    for _ in range(10):
        health.snapshot()
    assert sum("phase stalled:" in r.message for r in caplog.records) == 1
    health.finish_tick([("private-board", result)], ready_pending=ready)
    snapshot = health.snapshot()
    assert snapshot["stalled"] is False
    assert snapshot["last_outcome"] == outcome
    assert snapshot["last_success_at"] == (None if outcome == "error" else clock[0])
    assert snapshot["phase_durations_seconds"]["dispatch"] == 181
    encoded = json.dumps(snapshot)
    assert all(secret not in encoded for secret in ("private", "secret"))
    for _ in range(10):
        health.begin_tick()
        health.finish_tick([("private-board", result)], ready_pending=ready)
    summaries = [r for r in caplog.records if "kanban dispatcher health:" in r.message and r.levelno >= 20]
    assert len(summaries) == 1, "steady-state health summaries must be throttled"
    assert all(secret not in caplog.text for secret in ("private", "secret"))
    clock[0] += 300
    health.begin_tick()
    health.finish_tick([("private-board", result)], ready_pending=ready)
    assert sum("kanban dispatcher health:" in r.message and r.levelno >= 20 for r in caplog.records) == 2
