"""Rolling per-board worker start budget (``hermes_fork.kanban.start_budget``).

Drives the real ``dispatch_once`` -> ``_dispatch_once_locked`` path on an
isolated HERMES_HOME/kanban.db, so the ``start-budget`` FORK ANCHOR is what is
under test, not the helper in isolation. ``spawn_fn`` returns a fake pid so
upstream's ``_set_worker_pid`` writes the durable ``spawned`` event the budget
counts; nothing is actually started. The reclaim phase is stubbed out (the
fake pids are not real processes) and the clock is ``kbd.time.time``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import start_budget

T0 = 1_800_000_000
WINDOW = 600


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
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

    kanban_cfg: dict = {
        "review_dispatch": True,
        "dispatch_start_budget": 2,
        "dispatch_start_window_seconds": WINDOW,
    }
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": kanban_cfg})
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_run_reclaim_phase", lambda *a, **k: None)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: None)
    clock = Clock(T0)
    monkeypatch.setattr(kbd.time, "time", clock)

    pids = iter(range(40_000, 50_000))
    spawned: list[str] = []

    def spawn(task, workspace):
        spawned.append(task.id)
        return next(pids)

    with kbc.connect() as conn:
        yield conn, kanban_cfg, clock, spawned, spawn


def _tick(conn, spawn, **kw):
    return kbd.dispatch_once(conn, spawn_fn=spawn, **kw)


def _finish(conn, *task_ids):
    """Worker finished quickly: frees its concurrency slot, the start still counts."""
    for tid in task_ids:
        assert kb.complete_task(conn, tid, summary="done", force=True)


def _deferred(conn, tid):
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, start_budget.EVENT_KIND),
        )
    ]


def _new(conn, n, prefix="t"):
    return [kb.create_task(conn, title=f"{prefix}{i}", assignee="coder") for i in range(n)]


# ---------------------------------------------------------------- window edges

def test_window_boundary_holds_at_plus_window_and_resumes_one_second_later(env):
    conn, _cfg, clock, spawned, spawn = env
    a, b, c = _new(conn, 3)

    first = _tick(conn, spawn)
    assert [t for t, *_ in first.spawned] == [a, b]
    _finish(conn, a, b)  # concurrency is free again; only the start budget binds

    # Inside the window, and exactly at the inclusive edge: nothing starts.
    for now in (T0 + 1, T0 + WINDOW):
        clock.now = now
        held = _tick(conn, spawn)
        assert held.spawned == []
        assert (c, "start_budget") in held.respawn_guarded
        assert kb.get_task(conn, c).status == "ready"
        assert kb.get_task(conn, c).claim_lock is None

    payload = _deferred(conn, c)[-1]
    assert payload == {
        "reason": "start_budget_exceeded", "recent_starts": 2, "budget": 2,
        "window_seconds": WINDOW, "next_eligible_at": T0 + WINDOW + 1,
    }

    # One second past the edge: resumes with no operator action.
    clock.now = T0 + WINDOW + 1
    resumed = _tick(conn, spawn)
    assert [t for t, *_ in resumed.spawned] == [c]
    assert spawned == [a, b, c]


def test_budget_spent_across_multiple_ticks_then_single_slot_frees(env):
    conn, cfg, clock, spawned, spawn = env
    cfg["dispatch_start_budget"] = 3
    ids = _new(conn, 5)

    starts = []
    for offset in (0, 100, 200):  # one card per tick (max_spawn=1)
        clock.now = T0 + offset
        res = _tick(conn, spawn, max_spawn=1)
        assert len(res.spawned) == 1
        starts.append(res.spawned[0][0])
        _finish(conn, res.spawned[0][0])
    assert starts == ids[:3]

    clock.now = T0 + 300
    assert _tick(conn, spawn).spawned == []
    # The OLDEST start (T0) expires first: exactly one slot at T0+601 ...
    clock.now = T0 + WINDOW + 1
    res = _tick(conn, spawn)
    assert [t for t, *_ in res.spawned] == [ids[3]]
    _finish(conn, ids[3])
    # ... and the next only once the T0+100 start ages out.
    clock.now = T0 + 700
    assert _tick(conn, spawn).spawned == []
    clock.now = T0 + 701
    assert [t for t, *_ in _tick(conn, spawn).spawned] == [ids[4]]
    assert spawned == ids


def test_single_tick_never_overshoots_across_ready_and_review_lanes(env):
    conn, cfg, _clock, _spawned, spawn = env
    cfg["dispatch_start_budget"] = 3
    ready = _new(conn, 3, "r")
    review = _new(conn, 2, "v")
    conn.execute(
        f"UPDATE tasks SET status = 'review' WHERE id IN ({','.join('?' * len(review))})", review,
    )
    conn.commit()

    res = _tick(conn, spawn, max_spawn=10)

    assert len(res.spawned) == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'spawned'"
    ).fetchone()[0] == 3
    # Upstream's review reservation still applies inside the narrowed budget.
    assert any(t in review for t, *_ in res.spawned)


def test_hot_lowered_budget_waits_for_enough_starts_to_age_out(env):
    conn, cfg, clock, _spawned, spawn = env
    cfg["dispatch_start_budget"] = 3
    a, b, c, d = _new(conn, 4)
    for offset in (0, 50, 100):
        clock.now = T0 + offset
        res = _tick(conn, spawn, max_spawn=1)
        _finish(conn, res.spawned[0][0])

    cfg["dispatch_start_budget"] = 1  # live reload, no restart
    clock.now = T0 + 200
    assert _tick(conn, spawn).spawned == []
    # 3 starts, budget 1: the T0+100 start (index 3-1=2) must leave the window.
    assert _deferred(conn, d)[-1]["next_eligible_at"] == T0 + 100 + WINDOW + 1
    clock.now = T0 + 100 + WINDOW
    assert _tick(conn, spawn).spawned == []
    clock.now = T0 + 100 + WINDOW + 1
    assert [t for t, *_ in _tick(conn, spawn).spawned] == [d]


# ------------------------------------------------------ events and bookkeeping

def test_deferred_event_is_written_once_per_cooldown_not_every_tick(env):
    conn, _cfg, clock, _spawned, spawn = env
    a, b, c = _new(conn, 3)
    _tick(conn, spawn)
    _finish(conn, a, b)

    for offset in (10, 70, 130, 190):
        clock.now = T0 + offset
        _tick(conn, spawn)
    assert len(_deferred(conn, c)) == 1

    # A new cooldown (new next_eligible_at) is a new durable record.
    clock.now = T0 + WINDOW + 1
    assert [t for t, *_ in _tick(conn, spawn).spawned] == [c]
    _finish(conn, c)
    d, e = _new(conn, 2, "late")
    clock.now = T0 + WINDOW + 2
    # In window: c (T0+601) + d now = 2; e is held until c ages out.
    assert [t for t, *_ in _tick(conn, spawn).spawned] == [d]
    clock.now = T0 + WINDOW + 3
    assert _tick(conn, spawn).spawned == []
    assert _deferred(conn, e)[-1]["next_eligible_at"] == T0 + WINDOW + 1 + WINDOW + 1
    assert len(_deferred(conn, c)) == 1


def test_held_reason_reaches_the_stuck_warning_summary(env):
    conn, _cfg, _clock, _spawned, spawn = env
    a, b, _c = _new(conn, 3)
    _tick(conn, spawn)
    _finish(conn, a, b)
    held = _tick(conn, spawn)
    assert kbd.describe_suppression([held]) == "start_budget=1"


def test_unassigned_and_claimed_rows_get_no_deferred_event(env):
    conn, _cfg, _clock, _spawned, spawn = env
    a, b = _new(conn, 2)
    orphan = kb.create_task(conn, title="no assignee")
    _tick(conn, spawn)
    _finish(conn, a, b)
    _tick(conn, spawn)
    assert _deferred(conn, orphan) == []


def test_dry_run_reports_but_writes_nothing(env):
    conn, _cfg, _clock, _spawned, spawn = env
    a, b, c = _new(conn, 3)
    _tick(conn, spawn)
    _finish(conn, a, b)
    before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    res = _tick(conn, spawn, dry_run=True)

    assert res.spawned == []
    assert (c, "start_budget") in res.respawn_guarded
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before


# ------------------------------------------------- independence and defaults

def test_max_in_progress_stays_an_independent_concurrency_cap(env):
    conn, cfg, _clock, _spawned, spawn = env
    cfg["dispatch_start_budget"] = 10
    ids = _new(conn, 5)

    # Concurrency is the tighter cap: 2 running, start budget untouched.
    first = _tick(conn, spawn, max_in_progress=2)
    assert len(first.spawned) == 2
    second = _tick(conn, spawn, max_in_progress=2)
    assert second.spawned == []
    assert not any(r == "start_budget" for _t, r in second.respawn_guarded)
    assert all(_deferred(conn, t) == [] for t in ids)

    # Start budget is the tighter cap even though concurrency has room.
    cfg["dispatch_start_budget"] = 3
    _finish(conn, *[t for t, *_ in first.spawned])
    third = _tick(conn, spawn, max_in_progress=5)
    assert len(third.spawned) == 1


@pytest.mark.parametrize("value", [None, 0, -3, "six", True, ""])
def test_budget_off_leaves_upstream_behaviour_unchanged(env, value):
    conn, cfg, _clock, _spawned, spawn = env
    cfg["dispatch_start_budget"] = value
    ids = _new(conn, 4)
    for _ in range(3):
        res = _tick(conn, spawn)
        _finish(conn, *[t for t, *_ in res.spawned])
    assert len(_spawned) == 4
    assert all(_deferred(conn, t) == [] for t in ids)


def test_invalid_window_falls_back_to_default(env):
    _conn, cfg, *_ = env
    cfg["dispatch_start_window_seconds"] = "soon"
    assert start_budget.settings() == (2, start_budget.DEFAULT_WINDOW_SECONDS)
    cfg["dispatch_start_window_seconds"] = 0
    assert start_budget.settings() == (2, start_budget.DEFAULT_WINDOW_SECONDS)


# -------------------------------------------------------------- contention

def test_contended_dispatcher_starts_nothing_and_holder_starts_once(env):
    conn, cfg, _clock, spawned, spawn = env
    cfg["dispatch_start_budget"] = 1
    (only,) = _new(conn, 1)
    before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    with kbc._dispatch_tick_lock(kb.kanban_db_path()) as held:
        assert held is True
        loser = _tick(conn, spawn)
    assert loser.skipped_locked is True
    assert loser.spawned == [] and spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before

    winner = _tick(conn, spawn)
    again = _tick(conn, spawn)
    assert [t for t, *_ in winner.spawned] == [only]
    assert again.spawned == []
    assert spawned == [only]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'spawned'"
    ).fetchone()[0] == 1


def test_exhausted_budget_is_enforced_before_any_claim_in_either_lane(env):
    conn, _cfg, _clock, spawned, spawn = env
    a, b, ready_c = _new(conn, 3)
    (review_d,) = _new(conn, 1, "v")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_d,))
    conn.commit()
    _tick(conn, spawn)
    started = list(spawned)
    assert len(started) == 2
    _finish(conn, *started)
    held_ids = sorted({a, b, ready_c, review_d} - set(started))

    def claims():
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'claimed'"
        ).fetchone()[0]

    def runs():
        return conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]

    claims_before, runs_before = claims(), runs()
    held = _tick(conn, spawn, max_spawn=10)

    assert held.spawned == [] and spawned == started
    assert (claims(), runs()) == (claims_before, runs_before)
    for tid in held_ids:
        row = kb.get_task(conn, tid)
        assert row.claim_lock is None and row.status in ("ready", "review")
        assert (tid, "start_budget") in held.respawn_guarded
