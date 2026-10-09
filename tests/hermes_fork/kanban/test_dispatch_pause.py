"""Per-board dispatch pause (``hermes_fork.kanban.dispatch_pause``).

Drives the real ``dispatch_once`` -> ``_dispatch_once_locked`` path on an
isolated HERMES_HOME, so the ``start-budget`` FORK ANCHOR (which carries the
pause check) is what is under test. ``spawn_fn`` returns a fake pid; nothing
is actually started.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import dispatch_pause


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "NOT ISOLATED"
    kb.init_db()

    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}})
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_run_reclaim_phase", lambda *a, **k: None)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: None)

    pids = iter(range(40_000, 50_000))
    spawned: list[str] = []

    def spawn(task, workspace):
        spawned.append(task.id)
        return next(pids)

    return spawned, spawn


def _tick(spawn, board=None):
    with kbc.connect(board=board) as conn:
        return kbd.dispatch_once(conn, spawn_fn=spawn, board=board)


def _new(n, board=None, prefix="t"):
    with kbc.connect(board=board) as conn:
        return [kb.create_task(conn, title=f"{prefix}{i}", assignee="coder", board=board) for i in range(n)]


def _status(tid, board=None):
    with kbc.connect(board=board) as conn:
        return kb.get_task(conn, tid)


def test_paused_board_claims_nothing_and_resume_dispatches(env):
    spawned, spawn = env
    a, b = _new(2)

    assert dispatch_pause.pause(note="restart gateway")["paused"] is True
    held = _tick(spawn)

    assert held.spawned == [] and spawned == []
    assert sorted(held.respawn_guarded) == sorted([(a, "dispatch_paused"), (b, "dispatch_paused")])
    assert _status(a).status == "ready" and _status(a).claim_lock is None
    assert "dispatch_paused=2" in kbd.describe_suppression([held])

    assert dispatch_pause.resume() == {"resumed": True, "was_paused": True}
    resumed = _tick(spawn)
    assert sorted(t for t, *_ in resumed.spawned) == sorted([a, b])


def test_pause_never_touches_running_workers(env):
    spawned, spawn = env
    (a,) = _new(1)
    _tick(spawn)
    assert _status(a).status == "running"

    dispatch_pause.pause()
    _tick(spawn)

    task = _status(a)
    assert task.status == "running" and task.worker_pid is not None


def test_pause_is_per_board(env):
    spawned, spawn = env
    kb.create_board("other")
    (mine,) = _new(1, prefix="default")
    (theirs,) = _new(1, board="other", prefix="other")

    dispatch_pause.pause("other")

    assert [t for t, *_ in _tick(spawn).spawned] == [mine]
    assert _tick(spawn, board="other").spawned == []
    assert _status(theirs, board="other").status == "ready"
    assert dispatch_pause.is_paused("other") and not dispatch_pause.is_paused()


def test_state_uses_devs_sentinel_format_and_path(env):
    dispatch_pause.pause(note="  drain  ", paused_by="desktop")

    path = kb.kanban_db_path().with_suffix(".dispatch-pause.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["reason"] == "operator_paused" and raw["note"] == "drain" and raw["paused_by"] == "desktop"
    assert isinstance(raw["paused_at"], int)
    assert dispatch_pause.read_state() == raw


@pytest.mark.parametrize("body", ["{not json", "[]", "{}", '{"reason": ""}'])
def test_damaged_sentinel_fails_closed(env, body):
    spawned, spawn = env
    _new(1)
    dispatch_pause.pause_path().write_text(body, encoding="utf-8")

    assert dispatch_pause.read_state()["reason"] == dispatch_pause.UNREADABLE
    assert _tick(spawn).spawned == [] and spawned == []


def test_inaccessible_pause_sentinel_parent_must_not_claim(env, monkeypatch):
    """Python 3.14 Path.exists can hide PermissionError from an inaccessible parent."""
    spawned, spawn = env
    (task_id,) = _new(1)
    dispatch_pause.pause()
    sentinel = dispatch_pause.pause_path()
    assert dispatch_pause.is_paused()
    original_exists = Path.exists
    original_read_text = Path.read_text

    def inaccessible_exists(path):
        return False if path == sentinel else original_exists(path)

    def inaccessible_read_text(path, *args, **kwargs):
        if path == sentinel:
            raise PermissionError("pause sentinel parent inaccessible")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", inaccessible_exists)
    monkeypatch.setattr(Path, "read_text", inaccessible_read_text)

    state = dispatch_pause.read_state()
    result = _tick(spawn)
    assert result.spawned == [] and spawned == [], f"pause state {state!r} admitted a claim"
    assert state is not None and state["reason"] == dispatch_pause.UNREADABLE
    assert _status(task_id).status == "ready"


def test_dev_start_budget_record_still_pauses_until_resumed(env):
    """A dev-home cooldown record has no next writer to expire it: held until resume."""
    spawned, spawn = env
    _new(1)
    dispatch_pause.pause_path().write_text(json.dumps({
        "reason": "start_budget_exceeded", "recent_starts": 6, "budget": 6, "window_seconds": 600,
    }), encoding="utf-8")

    assert _tick(spawn).spawned == []
    dispatch_pause.resume()
    assert len(_tick(spawn).spawned) == 1


def test_resume_when_not_paused_is_a_noop(env):
    assert dispatch_pause.resume() == {"resumed": True, "was_paused": False}


def test_pause_reports_busy_while_a_tick_holds_the_lock(env):
    db_path = kb.kanban_db_path()
    entered, release = threading.Event(), threading.Event()

    def hold():
        with kbc._dispatch_tick_lock(db_path) as held:
            assert held
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(5)
        result = dispatch_pause.pause(wait_seconds=0.1)
    finally:
        release.set()
        holder.join(5)

    assert result == {"paused": False, "busy": True, "state": None}
    assert not dispatch_pause.is_paused()
    # Lock free again: the next pause lands.
    assert dispatch_pause.pause(wait_seconds=0.1)["paused"] is True


def test_pinned_db_is_isolated_from_the_live_board(env, tmp_path, monkeypatch):
    live = kb.kanban_db_path()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "sandbox" / "kanban.db"))
    dispatch_pause.pause()
    assert dispatch_pause.pause_path().parent == tmp_path / "sandbox"
    assert not live.with_suffix(".dispatch-pause.json").exists()
