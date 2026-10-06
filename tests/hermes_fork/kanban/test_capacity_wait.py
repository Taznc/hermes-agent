"""Healthy capacity wait vs zero-spawn starvation (``hermes_fork.kanban.capacity_wait``).

Drives the REAL ``dispatch_once`` on an isolated HERMES_HOME/kanban.db (through
the ``start-budget`` FORK ANCHOR, where upstream's verdict is marked) and the
embedded watcher through the ``dispatcher-standby`` anchor. "Healthy running
workers" are claimed rows whose worker pid is this test process (alive) with a
fresh heartbeat, so upstream's reclaim phase keeps them. Spawns are stubbed:
nothing is actually started.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch import DispatchResult
from hermes_fork.kanban import capacity_wait as cw
from hermes_fork.kanban import dispatch_pause
from hermes_fork.kanban import dispatcher_standby as ds
from tests.hermes_fork.kanban.test_dispatcher_standby import Runner, stop, until


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "NOT ISOLATED"
    kb.init_db()

    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    # gateway-next's live shape: aggregate 2, per profile 2, review dispatch on.
    kanban_cfg: dict = {
        "dispatch_interval_seconds": 1, "review_dispatch": True,
        "max_in_progress": 2, "max_in_progress_per_profile": 2, "failure_limit": 1000,
    }
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": kanban_cfg})
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(ds, "_LEADER_START_DELAY_S", 0.0)
    monkeypatch.setattr(ds, "_PUBLISH_PERIOD_S", 0.05)

    spawned: list[str] = []
    fail = {"on": False}

    def spawn(task, workspace, board=None):
        if fail["on"]:
            raise RuntimeError("exec format error")  # broken venv / PATH: zero-spawn starvation
        spawned.append(task.id)
        return os.getpid()  # alive, so the next reclaim phase keeps it running

    monkeypatch.setattr(kbd, "_default_spawn", spawn)
    with kbc.connect() as conn:
        yield conn, kanban_cfg, spawned, fail


def _running(conn, title: str, assignee: str) -> str:
    """A healthy worker: claimed, live pid, fresh heartbeat."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.claim_task(conn, tid, ttl_seconds=3600) is not None
    kbd._set_worker_pid(conn, tid, os.getpid())
    assert kbd.heartbeat_worker(conn, tid)
    return tid


def _review(conn, title: str, assignee: str) -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def _rate_limited(conn, title: str, assignee: str) -> str:
    """Ready card inside the provider rate-limit cooldown (respawn guard)."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute("UPDATE task_runs SET outcome='rate_limited', status='rate_limited', ended_at=? "
                 "WHERE id=?", (int(kbd.time.time()), run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                 "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
                 ("worker exited rate-limited (quota wall), requeued", tid))
    conn.commit()
    return tid


def _full_fleet(conn) -> tuple[str, str]:
    """Live evidence shape: 2 healthy workers, a rework card and a review gate queued."""
    _running(conn, "w1", "coder")
    _running(conn, "w2", "reviewer")
    return kb.create_task(conn, title="rework", assignee="coder"), _review(conn, "gate", "reviewer")


def _tick(conn, cfg, **overrides):
    kw = dict(max_in_progress=cfg.get("max_in_progress"),
              max_in_progress_per_profile=cfg.get("max_in_progress_per_profile"),
              failure_limit=cfg["failure_limit"])
    kw.update(overrides)
    return kbd.dispatch_once(conn, **kw)


# -- watcher: the reported false "dispatcher stuck" -----------------------------

@pytest.mark.asyncio
async def test_full_fleet_is_capacity_wait_not_stuck_then_spawns_when_a_slot_frees(board, caplog):
    conn, _cfg, spawned, _fail = board
    _full_fleet(conn)
    caplog.set_level(logging.INFO, logger="gateway.run")
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["completed_ticks"] >= ds._HEALTH_WINDOW + 2,
                    what="more than a health window of saturated ticks")
        snap = runner.health.snapshot()
        assert snap["ready_pending"] is True and snap["spawned_total"] == 0
        assert snap["last_outcome"] == "capacity_wait"
        assert snap["capacity_wait_boards"] == 1 and snap["no_spawn_streak"] == 0
        assert snap["capacity_wait_streak"] >= ds._HEALTH_WINDOW
        assert snap["error_boards"] == 0 and snap["stalled"] is False
        assert not [r for r in caplog.records if "dispatcher stuck" in r.getMessage()]

        # A worker finishes: the next tick spawns into the freed slot.
        with kbc.connect() as other:
            w1 = other.execute("SELECT id FROM tasks WHERE title='w1'").fetchone()[0]
            assert kb.complete_task(other, w1, summary="done", force=True)
        await until(lambda: runner.health.snapshot()["spawned_total"] >= 1, what="spawn into freed slot")
        snap = runner.health.snapshot()
        assert len(spawned) == 1 and snap["capacity_wait_streak"] == 0
    finally:
        await stop(runner, task)


@pytest.mark.asyncio
async def test_free_capacity_zero_spawn_still_warns_stuck(board, caplog):
    conn, _cfg, spawned, fail = board
    fail["on"] = True
    kb.create_task(conn, title="starved", assignee="coder")
    caplog.set_level(logging.INFO, logger="gateway.run")
    runner = Runner()
    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        await until(lambda: runner.health.snapshot()["no_spawn_streak"] >= ds._HEALTH_WINDOW,
                    what="stuck streak with free capacity")
        snap = runner.health.snapshot()
        assert snap["last_outcome"] == "no_spawn" and snap["capacity_wait_boards"] == 0
        await until(lambda: any("dispatcher stuck" in r.getMessage() for r in caplog.records),
                    what="stuck warning")
        assert spawned == []
    finally:
        await stop(runner, task)


# -- per-board classification on the real dispatch path ------------------------

def test_aggregate_limit_full_is_capacity_wait(board):
    conn, cfg, spawned, _ = board
    _full_fleet(conn)
    res = _tick(conn, cfg)
    assert res.spawned == [] and res.skipped_per_profile_capped == []  # upstream records nothing
    assert cw.board_state(conn, res) is True and spawned == []


def test_per_board_max_spawn_full_is_capacity_wait(board):
    conn, cfg, _spawned, _ = board
    _full_fleet(conn)
    res = _tick(conn, cfg, max_in_progress=None, max_in_progress_per_profile=None, max_spawn=2)
    assert res.spawned == [] and cw.board_state(conn, res) is True


def test_per_profile_limit_full_with_aggregate_headroom_is_capacity_wait(board):
    conn, cfg, _spawned, _ = board
    _running(conn, "w1", "coder")
    _running(conn, "w2", "coder")
    queued = {kb.create_task(conn, title=f"c{i}", assignee="coder") for i in range(2)}
    res = _tick(conn, cfg, max_in_progress=5)
    assert res.spawned == [] and {t for t, *_ in res.skipped_per_profile_capped} == queued
    assert cw.board_state(conn, res) is True


def test_per_profile_cap_does_not_hide_another_profiles_failed_spawn(board):
    conn, cfg, _spawned, fail = board
    _running(conn, "w1", "coder")
    _running(conn, "w2", "coder")
    kb.create_task(conn, title="capped", assignee="coder")
    kb.create_task(conn, title="starved", assignee="beta")  # free slot, spawn fails
    fail["on"] = True
    res = _tick(conn, cfg, max_in_progress=5)
    assert res.spawned == [] and len(res.skipped_per_profile_capped) == 1
    assert cw.board_state(conn, res) is False


def test_free_capacity_failed_spawn_is_not_capacity_wait(board):
    conn, cfg, _spawned, fail = board
    kb.create_task(conn, title="starved", assignee="coder")
    fail["on"] = True
    res = _tick(conn, cfg)
    assert res.spawned == [] and cw.board_state(conn, res) is False


def test_provider_backoff_with_free_capacity_stays_a_guard_hold(board):
    conn, cfg, _spawned, _ = board
    tid = _rate_limited(conn, "quota", "coder")
    res = _tick(conn, cfg)
    assert (tid, "rate_limit_cooldown") in res.respawn_guarded
    assert cw.board_state(conn, res) is False


def test_provider_backoff_behind_a_full_fleet_waits_on_capacity_first(board):
    conn, cfg, _spawned, _ = board
    _full_fleet(conn)
    _rate_limited(conn, "quota", "coder")
    res = _tick(conn, cfg)
    assert res.respawn_guarded == [] and cw.board_state(conn, res) is True


def test_paused_board_with_free_capacity_is_not_capacity_wait(board):
    conn, cfg, _spawned, _ = board
    tid = kb.create_task(conn, title="held", assignee="coder")
    assert dispatch_pause.pause()["paused"]
    res = _tick(conn, cfg)
    assert (tid, dispatch_pause.REASON) in res.respawn_guarded
    assert cw.board_state(conn, res) is False


def test_critical_memory_pressure_is_not_healthy_capacity_wait(board, monkeypatch):
    conn, cfg, _spawned, _ = board
    kb.create_task(conn, title="held", assignee="coder")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "critical")
    res = _tick(conn, cfg)
    assert res.memory_pressure == "critical" and cw.board_state(conn, res) is False


def test_capacity_freeing_spawns_and_board_has_nothing_pending(board):
    conn, cfg, spawned, _ = board
    _running(conn, "w1", "coder")
    kb.create_task(conn, title="next", assignee="coder")
    res = _tick(conn, cfg)
    assert len(res.spawned) == 1 and spawned and cw.board_state(conn, res) is None


def test_dead_worker_is_not_capacity_wait_its_slot_is_reclaimed(board):
    """A crashed worker must surface as a crash and free its slot, not hide behind 'full'."""
    import subprocess
    import sys

    conn, cfg, spawned, _ = board
    _rework, _gate = _full_fleet(conn)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(10)
    w1 = conn.execute("SELECT id FROM tasks WHERE title='w1'").fetchone()[0]
    kbd._set_worker_pid(conn, w1, dead.pid)
    conn.execute("UPDATE tasks SET started_at = started_at - 3600 WHERE id = ?", (w1,))  # past launch grace
    conn.commit()
    res = _tick(conn, cfg)
    assert w1 in res.crashed
    assert len(res.spawned) == 1 and not getattr(res, cw.ATTR, False)
    assert cw.board_state(conn, res) is None


def test_tick_is_capacity_wait_only_when_no_board_starves(board):
    """max_spawn is per board: a full board must not mask another board's starvation."""
    conn, cfg, _spawned, fail = board
    _full_fleet(conn)
    full = _tick(conn, cfg, max_in_progress=None, max_spawn=2)
    kb.create_board(slug="b2", name="B2")
    with kbc.connect(board="b2") as conn2:
        kb.create_task(conn2, title="starved", assignee="coder")
        fail["on"] = True
        starving = _tick(conn2, cfg, max_in_progress=None, max_spawn=2, board="b2")
    assert cw.tick_capacity_wait_boards([("default", full)]) == 1
    assert cw.tick_capacity_wait_boards([("default", full), ("b2", starving)]) == 0
    assert cw.tick_capacity_wait_boards([("default", full), ("b2", None)]) == 0  # error board


# -- health outcome precedence ------------------------------------------------

def test_capacity_wait_outcome_precedence():
    health = ds.DispatcherHealth(interval=1)
    health.begin_tick()
    health.finish_tick([("b", DispatchResult())], ready_pending=True, capacity_wait_boards=1)
    assert health.snapshot()["last_outcome"] == "capacity_wait"
    health.begin_tick()
    health.finish_tick([("b", DispatchResult(respawn_guarded=[("t", "active_pr")]))],
                       ready_pending=True, capacity_wait_boards=1)
    assert health.snapshot()["last_outcome"] == "guard_deferred"
    health.begin_tick()
    health.finish_tick([("b", None)], ready_pending=True, capacity_wait_boards=1)
    assert health.snapshot()["last_outcome"] == "error"
