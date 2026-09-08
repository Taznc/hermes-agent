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
import time
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


def _set_kanban_config(monkeypatch, kanban: dict) -> None:
    """Drive the caps through the real config loader.

    Entry points resolve caps via ``resolve_dispatch_caps``, which reads
    ``hermes_cli.config.load_config``. Patching config (what the operator
    writes) rather than an internal reader keeps these tests contracts about
    configured behaviour instead of about the current call chain.
    """
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": dict(kanban)})


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
    _set_kanban_config(monkeypatch, {})
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3


def test_run_daemon_uses_nondefault_board_for_connection_and_dispatch(
    kanban_home, monkeypatch,
):
    board = "secondary"
    kb.init_db(board=board)
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured["board"] = kwargs.get("board")
        captured["db_path"] = Path(
            conn.execute("PRAGMA database_list").fetchone()[2]
        ).resolve()
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(
        interval=0.01,
        board=board,
        stop_event=stop,
        on_tick=on_tick,
    )

    assert captured["board"] == board
    assert captured["db_path"] == kb.kanban_db_path(board).resolve()


def test_run_daemon_explicit_config_wins(kanban_home, monkeypatch):
    """Explicit ``kanban.max_in_progress`` beats the memory-derived default.

    Driven through real config rather than by patching the internal reader:
    the contract is "what the operator configured is what dispatch_once gets",
    which must hold regardless of which helper the daemon resolves it with.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _set_kanban_config(monkeypatch, {"max_in_progress": 7})
    monkeypatch.setattr(
        kbd, "derive_default_max_in_progress",
        lambda sample=None: pytest.fail("derived default must not be consulted"),
    )

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 7


def test_run_daemon_honours_per_profile_cap(kanban_home, monkeypatch):
    """The daemon must forward ``max_in_progress_per_profile`` too.

    ``dispatch_once`` treats an omitted cap as *unlimited*, so a daemon that
    resolves only the global cap hands one profile its whole backlog while the
    gateway tick, ``hermes kanban dispatch`` and the dashboard nudge all hold
    it to the configured per-profile limit. The caps bound the host, not an
    entry point, so every entry point must resolve the same set.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _set_kanban_config(
        monkeypatch, {"max_in_progress": 9, "max_in_progress_per_profile": 2})

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 9
    assert captured.get("max_in_progress_per_profile") == 2


def test_run_daemon_per_profile_cap_actually_limits_spawns(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """End-to-end: with a per-profile cap of 1, a one-profile backlog of three
    ready tasks leaves exactly one running after a tick.

    Asserts the observable outcome (how many workers exist) rather than the
    arguments passed, so it still holds if the plumbing is reshaped.
    """
    spawns: list = []
    monkeypatch.setattr(kbd, "_default_spawn", _fake_spawn_factory(spawns))
    _set_kanban_config(
        monkeypatch, {"max_in_progress": 10, "max_in_progress_per_profile": 1})

    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"t{i}", assignee="one-profile")
        conn.execute("UPDATE tasks SET status = 'ready'")
        conn.commit()

    stop = threading.Event()
    kbd.run_daemon(interval=0.01, stop_event=stop,
                   on_tick=lambda res: stop.set())

    with kbc.connect() as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]

    assert running == 1, (
        f"per-profile cap of 1 must leave 1 worker running, got {running}"
    )


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


# ---------------------------------------------------------------------------
# 4. High-priority slot reservation (kanban.priority_reserved_slots)
# ---------------------------------------------------------------------------
#
# Priority previously had no scheduling power at all: `_lane_rows` sorts
# `priority DESC, created_at ASC`, and that ordering was the ONLY place priority
# mattered. Every gate deciding whether a worker spawns at all ignored it, so a
# Critical card behind a saturated pool waited exactly as long as a Normal one.
#
# The reservation is the same mechanism the review lane already uses (hold a slot
# back so a sustained backlog cannot starve a lane), keyed on priority instead of
# lane. It grants EARLIER ACCESS to a slot and never preempts a running worker.


def _capped_config(monkeypatch, **kanban):
    """Config with review dispatch on, plus whatever the test sets."""
    import hermes_cli.config as cfgmod
    cfg = {"review_dispatch": True, **kanban}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": dict(cfg)})


def _park_running(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    """A task genuinely occupying a worker slot, with claim bookkeeping intact.

    Setting ``status='running'`` alone is not enough: the reclaim phase runs first
    and ``reconcile_orphaned_running`` requeues any running row with a NULL
    ``claim_lock``/``claim_expires`` as a zombie, which would silently free the
    very slot the test is trying to occupy. A foreign-host lock with a live expiry
    is also invisible to ``detect_crashed_workers`` (this host's PIDs only) and to
    ``release_stale_claims`` (not yet expired).
    """
    tid = kb.create_task(conn, title=title, assignee=assignee)
    conn.execute(
        "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ? "
        "WHERE id = ?",
        ("otherhost:12345", int(time.time()) + 3600, tid),
    )
    return tid


def test_critical_card_spawns_against_a_saturated_normal_queue(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """AC6 (first direction), stated literally: reserved=1, a queue of normal
    cards, and a newly-filed Critical card gets a worker on the next tick."""
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"normal-{i}", assignee="alice")
        critical = kb.create_task(
            conn, title="critical", assignee="alice", priority=2,
        )
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    assert critical in [s[0] for s in res.spawned]


def test_reservation_holds_a_slot_for_a_critical_card_blocked_by_per_profile_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The DISCRIMINATING case: a reservation that only reorders is worthless,
    because the ready lane is already sorted priority DESC.

    A Critical card whose assignee is at ``max_in_progress_per_profile`` cannot
    spawn this tick. Without a reservation, normal work immediately consumes the
    whole budget and re-saturates the pool, so the Critical card is no closer to
    a slot when the cap clears. With ``priority_reserved_slots=1`` its demand
    holds one slot back.

    AC3 in the same assertion: the capped Critical card still records
    ``skipped_per_profile_capped`` — the reservation composes with the
    per-profile cap and never overrides it into a spawn.
    """
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        # 'busy' already has one worker running -> at a per-profile cap of 1.
        _park_running(conn, "running", "busy")
        critical = kb.create_task(
            conn, title="critical-blocked", assignee="busy", priority=2,
        )
        # Distinct assignees: the per-profile cap must not be what limits normal
        # work, or this test would pass for the wrong reason (see the control).
        for i in range(4):
            kb.create_task(conn, title=f"normal-{i}", assignee=f"worker{i}")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=3, max_in_progress_per_profile=1,
        )

    # One slot is consumed by the already-running worker; 2 remain. The Critical
    # card holds one, so only ONE normal card spawns.
    assert len(res.spawned) == 1
    assert res.priority_slots_reserved == 1
    # Nothing spawned into the held slot: that is the cost, reported not hidden.
    assert res.priority_slots_unused == 1
    # AC3: the per-profile cap still binds and is still recorded as such.
    assert critical in [c[0] for c in res.skipped_per_profile_capped]
    assert critical not in [s[0] for s in res.spawned]
    # Every normal card held back by the reservation says so, not just the first.
    assert len(res.deferred_priority_reserved) == 3


def test_same_board_spawns_one_more_normal_card_when_reservation_is_off(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Control for the test above, and the proof it is not vacuous: the identical
    board at ``priority_reserved_slots=0`` (the default) hands the slot to normal
    work instead of holding it."""
    _capped_config(monkeypatch, priority_reserved_slots=0)

    spawns: list = []
    with kbc.connect() as conn:
        _park_running(conn, "running", "busy")
        kb.create_task(conn, title="critical-blocked", assignee="busy", priority=2)
        for i in range(4):
            kb.create_task(conn, title=f"normal-{i}", assignee=f"worker{i}")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=3, max_in_progress_per_profile=1,
        )

    assert len(res.spawned) == 2          # vs 1 with the reservation on
    assert res.priority_slots_reserved == 0
    assert res.priority_slots_unused == 0
    assert res.deferred_priority_reserved == []


def test_unused_reserved_slots_go_to_normal_work_in_the_same_tick(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """AC2's never-idle-capacity rule: a reservation with NO high-priority card
    waiting reserves nothing, and normal work gets the whole budget in this tick
    — not on some later one."""
    _capped_config(monkeypatch, priority_reserved_slots=2, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"normal-{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    assert len(res.spawned) == 3
    assert res.priority_slots_reserved == 0
    assert res.deferred_priority_reserved == []


def test_reservation_releases_partially_when_demand_is_smaller_than_the_setting(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Reserved is min(configured, budget, demand): 3 configured but only one
    high-priority card wanting a slot holds ONE slot, not three."""
    _capped_config(monkeypatch, priority_reserved_slots=3, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        _park_running(conn, "running", "busy")
        kb.create_task(conn, title="critical-blocked", assignee="busy", priority=2)
        for i in range(5):
            kb.create_task(conn, title=f"normal-{i}", assignee=f"worker{i}")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=4, max_in_progress_per_profile=1,
        )

    # Budget 3 after the running worker; demand is 1, so 1 held, 2 to normal work.
    assert res.priority_slots_reserved == 1
    assert len(res.spawned) == 2


def test_unassigned_high_priority_card_never_holds_a_slot(
    kanban_home, monkeypatch,
):
    """A Critical card nothing can spawn — no assignee, or a control-plane lane a
    terminal pulls via claim_task — must not hold a worker slot hostage: no
    worker would ever land in it, so the slot would idle forever."""
    import hermes_cli.profiles as profmod
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=1)
    monkeypatch.setattr(profmod, "profile_exists", lambda name: name == "alice")

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="critical-terminal-lane", assignee="orion-cc", priority=2)
        for i in range(3):
            kb.create_task(conn, title=f"normal-{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    assert res.priority_slots_reserved == 0
    assert len(res.spawned) == 2


def test_reservation_never_exceeds_the_tick_budget(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A reservation larger than the budget clamps to it instead of driving the
    normal allowance negative — and it still cannot spawn more than the host cap
    allows. The reservation only ever narrows what normal work may take."""
    _capped_config(monkeypatch, priority_reserved_slots=10, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"critical-{i}", assignee="alice", priority=2)
        for i in range(3):
            kb.create_task(conn, title=f"normal-{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    assert res.priority_slots_reserved == 2      # clamped to the budget
    assert len(res.spawned) == 2                 # host cap still binds
    assert all(s[0] for s in res.spawned)


def test_high_priority_review_card_can_take_the_reserved_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A Critical card in the REVIEW lane is real demand on the same shared
    budget, and a review spawn into a held slot counts as the reservation
    working rather than as capacity wasted."""
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        for i in range(4):
            kb.create_task(conn, title=f"normal-{i}", assignee="alice")
        review_id = kb.create_task(
            conn, title="critical-review", assignee="reviewer", priority=2,
        )
        _set_task_status(conn, review_id, "review")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    assert review_id in [s[0] for s in res.spawned]
    assert res.priority_slots_reserved == 1
    # The critical review card spawned into it, so nothing was held idle.
    assert res.priority_slots_unused == 0


def test_threshold_decides_which_cards_draw_on_the_reservation(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """``priority_reserved_threshold=2`` means only Critical draws on the
    reservation; a High (1) card is normal work for this purpose."""
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=2)

    spawns: list = []
    with kbc.connect() as conn:
        _park_running(conn, "running", "busy")
        # Priority 1 is BELOW the threshold of 2 -> generates no demand.
        kb.create_task(conn, title="high-blocked", assignee="busy", priority=1)
        for i in range(3):
            kb.create_task(conn, title=f"normal-{i}", assignee=f"worker{i}")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=3, max_in_progress_per_profile=1,
        )

    assert res.priority_slots_reserved == 0
    assert len(res.spawned) == 2


def test_default_config_leaves_dispatch_order_and_counts_unchanged(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """AC6 (second direction): with the feature at its shipped default the tick
    is identical to the pre-reservation behaviour — same spawn count, same
    priority-DESC order, and the new result fields inert."""
    _capped_config(monkeypatch)   # no priority_reserved_* keys at all

    spawns: list = []
    with kbc.connect() as conn:
        low = kb.create_task(conn, title="low", assignee="alice", priority=-1)
        normal = kb.create_task(conn, title="normal", assignee="alice")
        critical = kb.create_task(conn, title="critical", assignee="alice", priority=2)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    assert [s[0] for s in res.spawned] == [critical, normal, low]
    assert res.priority_slots_reserved == 0
    assert res.priority_slots_unused == 0
    assert res.deferred_priority_reserved == []


def test_settings_resolve_from_live_config_without_being_passed_in(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """AC4: an entry point that does not thread the settings still gets them,
    resolved from live config on the tick itself — the same fallback
    ``max_review_rounds`` uses, so retuning never needs a gateway restart."""
    _capped_config(monkeypatch, priority_reserved_slots=1, priority_reserved_threshold=1)

    spawns: list = []
    with kbc.connect() as conn:
        _park_running(conn, "running", "busy")
        kb.create_task(conn, title="critical-blocked", assignee="busy", priority=2)
        for i in range(4):
            kb.create_task(conn, title=f"normal-{i}", assignee=f"worker{i}")
        # No priority_reserved_* kwargs: dispatch_once must read them itself.
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=3, max_in_progress_per_profile=1,
        )

    assert res.priority_slots_reserved == 1
    assert len(res.spawned) == 1
