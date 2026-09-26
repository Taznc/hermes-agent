"""Safe standby + liveness/progress telemetry for the embedded Kanban dispatcher.

Exercises ``hermes_fork.kanban.dispatcher_standby`` THROUGH the upstream
``dispatcher-standby`` FORK ANCHOR (``runner._kanban_dispatcher_watcher()``), on
an isolated HERMES_HOME + kanban home, with a REAL machine-global dispatcher
flock. A second process holds that lock to model "another gateway"; killing it
models the leader exiting. Only tick cadence is shortened.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch import DispatchResult
from hermes_fork.kanban import dispatcher_standby as ds

REPO = Path(__file__).resolve().parents[3]


class Runner(GatewayKanbanWatchersMixin):
    def __init__(self) -> None:
        self._running = True

    async def _sleep_between_ticks(self, interval: float) -> None:  # test cadence only
        await asyncio.sleep(0.02)

    @property
    def health(self) -> ds.DispatcherHealth:
        return getattr(self, ds.HEALTH_ATTR)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
        "HERMES_KANBAN_DISPATCH_IN_GATEWAY",
    ):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "NOT ISOLATED"
    assert str(ds.snapshot_path()).startswith(str(tmp_path)), "NOT ISOLATED"
    kb.init_db()

    import hermes_cli.config as cfgmod

    kanban_cfg: dict = {"dispatch_interval_seconds": 1}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": kanban_cfg})
    monkeypatch.setattr(ds, "_LEADER_START_DELAY_S", 0.0)
    monkeypatch.setattr(ds, "_PUBLISH_PERIOD_S", 0.05)
    # waitpid(-1) would reap the lock-holder subprocess out from under Popen.
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    lock_path = kb.kanban_home() / "kanban" / ".dispatcher.lock"
    return kanban_cfg, lock_path


async def until(pred, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            if pred():
                return
        except AttributeError:  # watcher task not scheduled yet
            pass
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


class OtherGateway:
    """A separate OS process holding the machine-global dispatcher lock."""

    def __init__(self, lock_path: Path) -> None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        code = textwrap.dedent(f"""
            import sys
            from gateway.status import _try_acquire_file_lock
            h = open({str(lock_path)!r}, "a+", encoding="utf-8")
            print("held" if _try_acquire_file_lock(h) else "contended", flush=True)
            sys.stdin.read()
        """)
        self.proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True,
                                     env={**os.environ, "PYTHONPATH": str(REPO)})
        assert self.proc.stdout.readline().strip() == "held"

    def exit(self) -> None:
        self.proc.kill()  # crash-style exit: the OS drops the flock
        self.proc.wait(10)


def lock_is_free(lock_path: Path) -> bool:
    handle, state = _acquire_singleton_lock(lock_path)
    _release_singleton_lock(handle)
    return state == "held"


async def stop(runner: Runner, task: asyncio.Task) -> None:
    runner._running = False
    await asyncio.wait_for(task, 5)


def snapshot_file() -> dict:
    return json.loads(ds.snapshot_path().read_text(encoding="utf-8"))


# -- anchor ----------------------------------------------------------------

# sha256 of the upstream dispatcher body the anchor shadows. If an upstream sync
# changes it, re-read the diff and port any behavior change into
# hermes_fork/kanban/dispatcher_standby.py before updating this hash.
_SHADOWED_UPSTREAM_SHA256 = "e99c55167196e8e4e401d1d188d6052a20425f0c8b3ce0c74b25e1e22fb1c71e"


def _shadowed_upstream_source() -> str:
    watcher = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)
    head, _, rest = watcher.partition("# >>> FORK ANCHOR: dispatcher-standby <<<")
    assert rest, "dispatcher-standby FORK ANCHOR missing from _kanban_dispatcher_watcher"
    _, _, body = rest.partition("# <<< FORK ANCHOR >>>")
    return inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_boot) + head + body


def test_shadowed_upstream_dispatcher_unchanged_since_port():
    digest = hashlib.sha256(_shadowed_upstream_source().encode()).hexdigest()
    assert digest == _SHADOWED_UPSTREAM_SHA256, (
        "upstream _kanban_dispatcher_boot/_kanban_dispatcher_watcher changed: port the "
        f"change into hermes_fork.kanban.dispatcher_standby, then set the hash to {digest}")


# -- leadership --------------------------------------------------------------

@pytest.mark.asyncio
async def test_sole_gateway_leads_and_idle_tick_is_liveness_not_progress(env):
    """Healthy idle tick: real dispatch_once on an empty SQLite board."""
    _cfg, lock_path = env
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["completed_ticks"] >= 2, what="two idle ticks")
        snap = runner.health.snapshot()
        assert snap["role"] == "leader" and snap["lock_state"] == "held"
        assert runner._owns_kanban_dispatcher_lock() and not lock_is_free(lock_path)
        assert snap["last_outcome"] == "idle"
        assert snap["last_success_at"] is not None  # live
        assert snap["last_progress_at"] is None and snap["spawned_total"] == 0  # no progress
        assert snap["stalled"] is False and snap["error_boards"] == 0
        await until(lambda: snapshot_file().get("completed_ticks", 0) >= 1, what="snapshot file")
        on_disk = snapshot_file()
        assert on_disk["pid"] == os.getpid() and on_disk["role"] == "leader"
    finally:
        await stop(runner, task)
    assert lock_is_free(lock_path), "clean stop must release the dispatcher lock"
    assert runner.health.snapshot()["role"] == "stopped"
    assert snapshot_file()["role"] == "stopped"


@pytest.mark.asyncio
async def test_contended_gateway_stands_by_then_promotes_when_leader_exits(env, caplog):
    _cfg, lock_path = env
    other = OtherGateway(lock_path)
    ticks: list[int] = []
    orig = ds._KanbanDispatcher.tick_once

    def counting_tick(self):
        ticks.append(os.getpid())
        return orig(self)

    runner = Runner()
    caplog.set_level(logging.INFO, logger="gateway.run")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ds._KanbanDispatcher, "tick_once", counting_tick)
        task = asyncio.create_task(runner._kanban_dispatcher_watcher())
        try:
            # Lock contention: no dispatch, repeated retries, watcher stays alive.
            await until(lambda: runner.health.snapshot()["standby_attempts"] >= 5, what="standby retries")
            snap = runner.health.snapshot()
            assert (snap["role"], snap["lock_state"], snap["phase"]) == ("standby", "contended", "standby")
            assert not task.done(), "a contended watcher must not return (upstream bug)"
            assert ticks == [] and not runner._owns_kanban_dispatcher_lock()
            assert snap["stalled"] is False, "standby is not a stall"
            assert sum("standing by" in r.getMessage() for r in caplog.records) == 1, "log once, not per retry"
            assert snapshot_file()["role"] == "standby"

            other.exit()  # leader exit -> standby promotion
            await until(lambda: ticks, what="promoted dispatch tick")
            snap = runner.health.snapshot()
            assert snap["role"] == "leader" and snap["leader_since"] is not None
            assert runner._owns_kanban_dispatcher_lock()
            assert any("assumed leadership after standby" in r.getMessage() for r in caplog.records)
        finally:
            if other.proc.poll() is None:
                other.exit()
            await stop(runner, task)
    assert lock_is_free(lock_path)


@pytest.mark.asyncio
async def test_two_watchers_never_lead_concurrently(env):
    """Two runners in one process race for the same OS lock: exactly one leads."""
    _cfg, lock_path = env
    a, b = Runner(), Runner()
    ta = asyncio.create_task(a._kanban_dispatcher_watcher())
    tb = asyncio.create_task(b._kanban_dispatcher_watcher())
    try:
        await until(lambda: getattr(a, ds.HEALTH_ATTR, None) and getattr(b, ds.HEALTH_ATTR, None)
                    and {a.health.snapshot()["role"], b.health.snapshot()["role"]} == {"leader", "standby"},
                    what="one leader, one standby")
        leader, follower, lt = (a, b, ta) if a.health.snapshot()["role"] == "leader" else (b, a, tb)
        for _ in range(20):
            assert [a._owns_kanban_dispatcher_lock(), b._owns_kanban_dispatcher_lock()].count(True) == 1
            await asyncio.sleep(0.01)
        await stop(leader, lt)
        await until(lambda: follower.health.snapshot()["role"] == "leader", what="follower promotion")
        assert follower._owns_kanban_dispatcher_lock() and not leader._owns_kanban_dispatcher_lock()
    finally:
        a._running = b._running = False
        await asyncio.wait_for(asyncio.gather(ta, tb), 5)
    assert lock_is_free(lock_path)


@pytest.mark.asyncio
async def test_unavailable_lock_fails_closed(env, monkeypatch):
    """Upstream dispatched 'on config control alone'; an unprotected leader is refused."""
    monkeypatch.setattr(ds, "_acquire_singleton_lock", lambda path: (None, "unavailable"))
    monkeypatch.setattr(ds._KanbanDispatcher, "tick_once",
                        lambda self: pytest.fail("dispatched without exclusion"))
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["standby_attempts"] >= 3, what="unavailable retries")
        snap = runner.health.snapshot()
        assert (snap["role"], snap["lock_state"]) == ("standby", "unavailable")
    finally:
        await stop(runner, task)


@pytest.mark.asyncio
async def test_disable_while_standing_by_exits_without_leading(env):
    kanban_cfg, lock_path = env
    other = OtherGateway(lock_path)
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["role"] == "standby", what="standby")
        kanban_cfg["dispatch_in_gateway"] = False
        other.exit()  # even with the lock now free, the disabled gateway must not lead
        await asyncio.wait_for(task, 5)
        assert runner.health.snapshot()["role"] == "disabled"
        assert not runner._owns_kanban_dispatcher_lock() and lock_is_free(lock_path)
    finally:
        if other.proc.poll() is None:
            other.exit()
        runner._running = False


@pytest.mark.asyncio
async def test_duplicate_start_is_refused_and_keeps_the_lock(env):
    _cfg, lock_path = env
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["role"] == "leader", what="leader")
        await asyncio.wait_for(runner._kanban_dispatcher_watcher(), 1)  # returns immediately
        assert runner._owns_kanban_dispatcher_lock() and not lock_is_free(lock_path)
        assert not task.done()
    finally:
        await stop(runner, task)


# -- progress vs liveness ----------------------------------------------------

@pytest.mark.asyncio
async def test_stuck_worker_queue_is_live_but_no_progress(env, monkeypatch, caplog):
    """Ready work that never spawns: ticks keep completing, progress never advances."""
    guarded = DispatchResult(respawn_guarded=[("t_x", "active_pr")])
    monkeypatch.setattr(ds._KanbanDispatcher, "tick_once", lambda self: [("default", DispatchResult())])
    monkeypatch.setattr(ds._KanbanDispatcher, "ready_nonempty", lambda self: True)
    caplog.set_level(logging.INFO, logger="gateway.run")
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["completed_ticks"] >= ds._HEALTH_WINDOW,
                    what="health window of no-spawn ticks")
        snap = runner.health.snapshot()
        assert snap["last_outcome"] == "no_spawn" and snap["ready_pending"] is True
        assert snap["no_spawn_streak"] >= ds._HEALTH_WINDOW
        assert snap["last_success_at"] is not None and snap["last_progress_at"] is None
        assert snap["stalled"] is False
        warnings = [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]
        assert len(warnings) == 1 and "Dispatcher itself is live" in warnings[0]

        monkeypatch.setattr(ds._KanbanDispatcher, "tick_once", lambda self: [("default", guarded)])
        before = runner.health.snapshot()["completed_ticks"]
        await until(lambda: runner.health.snapshot()["completed_ticks"] > before + 1, what="guarded tick")
        assert runner.health.snapshot()["last_outcome"] == "guard_deferred"

        monkeypatch.setattr(ds._KanbanDispatcher, "tick_once", lambda self: [
            ("default", DispatchResult(spawned=[("t_y", "coder", "/ws")]))])
        monkeypatch.setattr(ds, "_log_spawn_results", lambda results: True)
        await until(lambda: runner.health.snapshot()["last_progress_at"] is not None, what="progress")
        snap = runner.health.snapshot()
        assert snap["last_outcome"] == "spawned" and snap["spawned_total"] >= 1
        assert snap["no_spawn_streak"] == 0
        assert "t_y" not in json.dumps(snap) and "coder" not in json.dumps(snap), "payload-free"
    finally:
        await stop(runner, task)


@pytest.mark.asyncio
async def test_hung_dispatch_thread_is_stalled_and_blocks_handover_until_it_returns(env, monkeypatch):
    """Stuck dispatch + shutdown: the lock outlives cancellation until the thread exits."""
    _cfg, lock_path = env
    entered, release = threading.Event(), threading.Event()

    def hung_tick(self):
        entered.set()
        assert release.wait(10), "test must release the blocked thread"
        return [("default", DispatchResult())]

    monkeypatch.setattr(ds._KanbanDispatcher, "tick_once", hung_tick)
    monkeypatch.setattr(ds.DispatcherHealth, "stall_threshold", lambda self: 0.1)
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(entered.is_set, what="dispatch thread entered")
        await until(lambda: runner.health.snapshot()["stalled"], what="stalled")
        snap = runner.health.snapshot()
        assert snap["phase"] == "dispatch" and snap["completed_ticks"] == 0
        assert snap["last_success_at"] is None
        # The loop timer keeps publishing while the tick is hung.
        await until(lambda: snapshot_file().get("stalled") is True, what="stall on disk")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.health.snapshot()["phase"] == "draining"
        assert runner._owns_kanban_dispatcher_lock() and not lock_is_free(lock_path), (
            "lock must not pass to a standby while the dispatch thread may still write")
        restarted = asyncio.create_task(runner._kanban_dispatcher_watcher())
        await asyncio.wait_for(restarted, 1)  # refused: still draining
        assert runner.health.snapshot()["phase"] == "draining"
    finally:
        release.set()
    await until(lambda: not runner._owns_kanban_dispatcher_lock(), what="release after drain")
    assert lock_is_free(lock_path)
    assert runner.health.snapshot()["role"] == "stopped"


def test_loop_teardown_cancel_retains_lock_until_process_exit(env):
    """If teardown cancels even the shielded service, the lock is retained (not raced)."""
    _cfg, lock_path = env
    entered, release = threading.Event(), threading.Event()

    def hung_tick(self):
        entered.set()
        release.wait(10)
        return []

    runner = Runner()
    loop = asyncio.new_event_loop()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ds._KanbanDispatcher, "tick_once", hung_tick)
            task = loop.create_task(runner._kanban_dispatcher_watcher())

            async def wait_entered():
                await until(entered.is_set, what="dispatch thread entered")

            loop.run_until_complete(wait_entered())
            for t in asyncio.all_tasks(loop):  # what asyncio.run's teardown does
                t.cancel()
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
            assert task.done()
            snap = runner.health.snapshot()
            assert (snap["role"], snap["phase"], snap["lock_state"]) == ("retained", "retained", "held")
            assert runner._owns_kanban_dispatcher_lock() and not lock_is_free(lock_path)
    finally:
        release.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        runner._release_kanban_dispatcher_lock()


def test_error_tick_completes_but_is_not_success():
    now = [1000.0]
    health = ds.DispatcherHealth(interval=1, clock=lambda: now[0], wall=lambda: now[0])
    health.lead()
    health.begin_tick()
    health.finish_tick([("b", DispatchResult())])
    ok_at = health.snapshot()["last_success_at"]
    now[0] += 5
    health.begin_tick()
    health.finish_tick([("b", None)])  # per-board wrapper swallowed an exception
    snap = health.snapshot()
    assert snap["completed_ticks"] == 2 and snap["last_outcome"] == "error"
    assert snap["error_boards"] == 1 and snap["last_success_at"] == ok_at
    health.begin_tick()
    health.phase("dispatch")
    now[0] += 31
    assert health.snapshot()["stalled"] is True
    health.standby("contended")
    now[0] += 1000
    assert health.snapshot()["stalled"] is False
