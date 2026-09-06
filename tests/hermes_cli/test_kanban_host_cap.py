"""Host-level concurrency accounting + review-lane fairness (OOF-30 review).

Three gaps found in review of the original memory-guard PR:

1. The standalone daemon path (``hermes kanban daemon --force`` /
   :func:`hermes_cli.kanban_db_dispatch.run_daemon`) never resolved
   ``kanban.max_in_progress`` at all — the one shipped entry point that
   could still fan out an entire backlog in a single tick.
2. ``max_in_progress`` was enforced per-board while the gateway dispatcher
   ticks every active board — N boards multiplied the host budget by N.
3. The ready loop consumed the entire shared spawn budget before the
   review loop ran, so a sustained ready backlog starved autonomous
   reviews indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. Standalone daemon resolves max_in_progress (P1a)
# ---------------------------------------------------------------------------


def test_run_daemon_resolves_and_passes_max_in_progress(
    kanban_home, monkeypatch,
):
    """The daemon tick must pass a resolved cap into dispatch_once.

    Regression guard for the OOF-30 review finding: ``run_daemon`` only
    forwarded ``max_spawn`` — with no explicit ``--max`` (the shipped
    systemd shape) nothing capped the tick even though the gateway and
    ``hermes kanban dispatch`` paths both resolved the memory-derived
    default.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    # No explicit config → the derived default must flow through.
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3


def test_run_daemon_explicit_config_wins(kanban_home, monkeypatch):
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    monkeypatch.setattr(
        kbd, "derive_default_max_in_progress",
        lambda sample=None: pytest.fail("derived default must not be consulted"),
    )

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 7


def test_configured_max_in_progress_parsing(monkeypatch):
    import hermes_cli.config as cfgmod

    cases = [
        ({"kanban": {"max_in_progress": 4}}, 4),
        ({"kanban": {"max_in_progress": "5"}}, 5),
        ({"kanban": {"max_in_progress": 0}}, None),
        ({"kanban": {"max_in_progress": -2}}, None),
        ({"kanban": {"max_in_progress": "lots"}}, None),
        ({"kanban": {}}, None),
        ({}, None),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            cfgmod, "load_config_readonly", lambda c=config: c
        )
        assert kbd.configured_max_in_progress() == expected, config


# ---------------------------------------------------------------------------
# 2. max_in_progress counts running work on ALL boards (P1b)
# ---------------------------------------------------------------------------


def test_max_in_progress_counts_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """Workers running on another board consume the same host budget."""
    kb.create_board("second")

    # Two workers already running on the second board.
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Host budget (2) already consumed by the second board → nothing spawns.
    assert not spawns
    assert not res.spawned


def test_max_in_progress_partial_budget_across_boards(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")

    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # 1 running elsewhere + budget 2 → exactly one new spawn here.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_count_running_tasks_other_boards_fails_open(
    kanban_home, monkeypatch,
):
    """A broken board enumeration must not brick dispatch (returns 0)."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_other_boards() == 0


def test_host_cap_allows_only_one_concurrent_cross_board_dispatch(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A host cap is an atomic reservation across board-local ticks.

    Both boards start their budget calculation together.  Without a host-wide
    lock they each observe the sole free slot and both claim; with it, one tick
    runs and the other skips instead of exceeding the configured cap.
    """
    kb.create_board("second")
    original_budget = kbd._tick_spawn_budget
    rendezvous = threading.Barrier(2)

    def synchronized_budget(*args, **kwargs):
        try:
            rendezvous.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            # The host lock correctly prevents the second tick from entering.
            pass
        return original_budget(*args, **kwargs)

    monkeypatch.setattr(kbd, "_tick_spawn_budget", synchronized_budget)
    spawns: list[str] = []
    start = threading.Barrier(2)

    def dispatch(board: str) -> None:
        with kbc.connect(board=board) as conn:
            kb.create_task(conn, title=f"ready-{board}", assignee="alice")
            start.wait(timeout=2)
            kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1, board=board,
            )

    workers = [threading.Thread(target=dispatch, args=(board,)) for board in ("default", "second")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert len(spawns) == 1


def test_per_profile_cap_counts_running_workers_on_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """A profile's cap applies to the host, not merely the current board."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        busy = kb.create_task(conn, title="already-running", assignee="alice")
        assert kb.claim_task(conn, busy) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="must-wait", assignee="alice")
        result = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=2,
            max_in_progress_per_profile=1,
        )

    assert spawns == []
    assert len(result.skipped_per_profile_capped) == 1
    assert result.skipped_per_profile_capped[0][1:] == ("alice", 1)


def test_max_spawn_stays_per_board(kanban_home, all_assignees_spawnable):
    """``max_spawn`` keeps its historical per-board semantics."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

    # The other board's worker does NOT count against max_spawn.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


# ---------------------------------------------------------------------------
# concurrency_snapshot — shared counter for diagnostics' stranded_in_ready
#
# The dispatcher and kanban_diagnostics must never disagree on "is the board
# at capacity right now": concurrency_snapshot reuses the SAME counting
# helpers dispatch_once itself calls (count_running_tasks /
# count_running_tasks_other_boards / count_running_tasks_by_assignee), so
# there is exactly one implementation of "how many workers are running".
# ---------------------------------------------------------------------------


def test_concurrency_snapshot_reflects_real_running_counts(kanban_home, all_assignees_spawnable):
    """Total running + per-assignee running must match what dispatch_once's
    own cap enforcement would compute, across boards."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        other_running = kb.create_task(conn, title="already-running", assignee="alice")
        assert kb.claim_task(conn, other_running) is not None

    with kbc.connect() as conn:
        here_running = kb.create_task(conn, title="also-running", assignee="bob")
        assert kb.claim_task(conn, here_running) is not None
        snap = kbd.concurrency_snapshot(conn, kanban_cfg={"max_in_progress": 5, "max_in_progress_per_profile": 3})

    assert snap["max_in_progress"] == 5
    assert snap["max_in_progress_per_profile"] == 3
    assert snap["total_running"] == 2  # one on this board, one on "second"
    assert snap["running_by_assignee"] == {"alice": 1, "bob": 1}


def test_concurrency_snapshot_uses_memory_derived_default_when_unset(kanban_home, monkeypatch):
    """With no explicit kanban.max_in_progress, the snapshot must resolve the
    SAME memory-derived default dispatch_once uses — never hardcode 6."""
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 9)
    with kbc.connect() as conn:
        snap = kbd.concurrency_snapshot(conn, kanban_cfg={})
    assert snap["max_in_progress"] == 9


# ---------------------------------------------------------------------------
# 3. Review lane cannot be starved by a sustained ready backlog (P2)
# ---------------------------------------------------------------------------


def _park_in_review(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    _set_task_status(conn, tid, "review")
    return tid


def test_review_lane_gets_reserved_slot_under_ready_backlog(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        review_id = _park_in_review(conn, "review-me", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    spawned_ids = [s[0] for s in res.spawned]
    # Budget 2: one ready + the reserved review slot — never 2×ready.
    assert len(spawned_ids) == 2
    assert review_id in spawned_ids


def test_review_reservation_released_when_no_review_work(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # No review work → ready lane keeps the full budget.
    assert len(res.spawned) == 2


def test_nonspawnable_review_does_not_tax_ready_budget(
    kanban_home, monkeypatch,
):
    """Review tasks parked for humans (no real profile) release the slot."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    # Only 'alice' is a real profile; the review assignee is a human lane.
    monkeypatch.setattr(
        profmod, "profile_exists", lambda name: name == "alice"
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2"):
            kb.create_task(conn, title=title, assignee="alice")
        _park_in_review(conn, "human-review", "some-human")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Human-lane review is not spawnable → no reservation, ready gets both.
    assert len(res.spawned) == 2


def test_review_budget_still_bounded_by_shared_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation caps the ready lane; it grants review no extra slots."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-1", assignee="alice")
        for i in range(3):
            _park_in_review(conn, f"review-{i}", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Budget 2 total across both lanes, reservation notwithstanding.
    assert len(res.spawned) == 2
