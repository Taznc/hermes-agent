"""Fork-owned tests for ``hermes_fork.kanban.dispatch_concurrency``.

Extraction target: the ``kanban.*`` concurrency-cap resolution
(:class:`DispatchCaps` / :func:`resolve_dispatch_caps` /
:func:`clamp_requested_max_spawn`), host-wide running-task counting, and the
durable dispatch pause/resume circuit (start-budget cooldown + operator
pause), moved out of ``hermes_cli.kanban_db_dispatch`` behind the
``# >>> FORK ANCHOR: kanban-dispatch-concurrency <<<`` marker. These tests
pin the extracted module's own behavior contracts and the identity of its
late-bound ``_kb``/``_kbc``/``_kd`` origin references, not the full
dispatcher integration (covered by ``tests/hermes_cli/test_kanban_host_cap.py``,
``test_kanban_operator_pause.py`` and ``test_kanban_dispatch_start_budget.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import dispatch_concurrency as dc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Re-export identity: the facade attribute IS the extracted function, not a
# copy — proves the anchor import wires the fork module in rather than
# duplicating behavior that could drift.
# ---------------------------------------------------------------------------


def test_kanban_db_dispatch_reexports_the_extracted_caps_and_pause_api():
    assert kbd.DispatchCaps is dc.DispatchCaps
    assert kbd.resolve_dispatch_caps is dc.resolve_dispatch_caps
    assert kbd.clamp_requested_max_spawn is dc.clamp_requested_max_spawn
    assert kbd.pause_dispatch is dc.pause_dispatch
    assert kbd.resume_dispatch is dc.resume_dispatch
    assert kbd.read_dispatch_pause is dc.read_dispatch_pause
    assert kbd.concurrency_snapshot is dc.concurrency_snapshot
    assert kbd.OPERATOR_PAUSE_REASON == dc.OPERATOR_PAUSE_REASON


def test_extraction_late_bound_origins_resolve_to_the_real_modules():
    """The cycle-breaking ``_kb``/``_kbc``/``_kd`` module refs must point at the
    real, fully-initialized origin modules, not stand-ins or partial imports."""
    assert dc._kb is kb
    assert dc._kbc is kbc
    assert dc._kd is kbd


# ---------------------------------------------------------------------------
# DispatchCaps / resolve_dispatch_caps
# ---------------------------------------------------------------------------


def test_resolve_dispatch_caps_defaults_when_config_empty():
    caps = dc.resolve_dispatch_caps({})
    assert caps.max_spawn is None
    assert caps.max_in_progress_per_profile is None
    assert caps.default_assignee is None
    assert caps.max_review_rounds == dc.DEFAULT_MAX_REVIEW_ROUNDS
    assert caps.priority_reserved_slots == dc.DEFAULT_PRIORITY_RESERVED_SLOTS
    assert caps.priority_reserved_threshold == dc.DEFAULT_PRIORITY_RESERVED_THRESHOLD


def test_resolve_dispatch_caps_honors_explicit_config():
    caps = dc.resolve_dispatch_caps({
        "max_spawn": 4,
        "max_in_progress": 7,
        "max_in_progress_per_profile": 2,
        "default_assignee": " coder ",
        "max_review_rounds": 5,
        "priority_reserved_slots": 1,
        "priority_reserved_threshold": 2,
    })
    assert caps.max_spawn == 4
    assert caps.max_in_progress == 7
    assert caps.max_in_progress_per_profile == 2
    assert caps.default_assignee == "coder"
    assert caps.max_review_rounds == 5
    assert caps.priority_reserved_slots == 1
    assert caps.priority_reserved_threshold == 2


def test_clamp_requested_max_spawn_never_widens_beyond_host_cap():
    caps = dc.resolve_dispatch_caps({"max_in_progress": 3})
    assert dc.clamp_requested_max_spawn(99, caps) == 3
    assert dc.clamp_requested_max_spawn(1, caps) == 1
    assert dc.clamp_requested_max_spawn(None, caps) == 3


def test_clamp_requested_max_spawn_unbounded_when_no_host_cap():
    caps_uncapped = dc.DispatchCaps(
        max_in_progress=None, max_in_progress_per_profile=None,
        max_spawn=None, default_assignee=None,
    )
    assert dc.clamp_requested_max_spawn(99, caps_uncapped) == 99
    assert dc.clamp_requested_max_spawn(None, caps_uncapped) is None


# ---------------------------------------------------------------------------
# Running-task counting
# ---------------------------------------------------------------------------


def test_total_running_tasks_combines_this_board_and_other_boards(kanban_home, monkeypatch):
    conn = kbc.connect()
    monkeypatch.setattr(dc, "count_running_tasks_by_assignee_other_boards", lambda board=None: {})
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 5)
    assert dc.total_running_tasks(conn, board=None) == 5  # 0 local + 5 other


def test_count_running_tasks_by_assignee_merges_local_and_other_boards(kanban_home, monkeypatch):
    conn = kbc.connect()
    monkeypatch.setattr(
        dc, "count_running_tasks_by_assignee_other_boards",
        lambda board=None: {"coder": 2, "reviewer": 1},
    )
    task_id = kb.create_task(conn, title="t1", assignee="coder")
    kb.claim_task(conn, task_id, claimer="host:1")
    counts = dc.count_running_tasks_by_assignee(conn, board=None)
    assert counts["coder"] == 3  # 2 other-board + 1 local running
    assert counts["reviewer"] == 1


# ---------------------------------------------------------------------------
# Pause / resume circuit
# ---------------------------------------------------------------------------


def test_pause_dispatch_is_idempotent_and_preserves_first_record(kanban_home):
    first = dc.pause_dispatch(board=None, note="maintenance one")
    assert first["paused"] is True
    assert first["state"]["reason"] == dc.OPERATOR_PAUSE_REASON
    assert first["state"]["note"] == "maintenance one"

    second = dc.pause_dispatch(board=None, note="maintenance two")
    assert second["paused"] is True
    # Re-pausing must NOT clobber the first record's note.
    assert second["state"]["note"] == "maintenance one"


def test_resume_dispatch_clears_an_operator_pause(kanban_home):
    dc.pause_dispatch(board=None, note="drain")
    assert dc.read_dispatch_pause(board=None) is not None

    result = dc.resume_dispatch(board=None)
    assert result["resumed"] is True
    assert result["was_paused"] is True
    assert dc.read_dispatch_pause(board=None) is None


def test_resume_dispatch_on_a_clean_board_reports_not_previously_paused(kanban_home):
    result = dc.resume_dispatch(board=None)
    assert result["was_paused"] is False
    assert result["resumed"] is True


def test_dispatch_pause_message_names_the_resume_command_for_a_generic_fault():
    msg = dc.dispatch_pause_message(
        {"reason": "restart_safe_scope_unavailable", "fault_code": "X"}, board="myboard",
    )
    assert "manual intervention required" in msg
    assert "hermes kanban --board myboard dispatch --resume-circuit" in msg


def test_dispatch_pause_message_for_operator_pause_is_reassuring_not_alarming():
    msg = dc.dispatch_pause_message({"reason": dc.OPERATOR_PAUSE_REASON}, board=None)
    assert "paused for maintenance" in msg
    assert "already-running workers are unaffected" in msg


# ---------------------------------------------------------------------------
# High-priority slot demand
# ---------------------------------------------------------------------------


class _Row(dict):
    def __getitem__(self, key):
        return dict.get(self, key)


def test_high_priority_demand_counts_only_assigned_rows_at_or_above_threshold(monkeypatch):
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: name in ("coder", "reviewer"))
    rows = [
        _Row(priority=2, assignee="coder"),
        _Row(priority=1, assignee="reviewer"),
        _Row(priority=2, assignee=None),  # unassigned: never wants a slot
        _Row(priority=0, assignee="coder"),
    ]
    assert dc._high_priority_demand(rows, threshold=2) == 1
    assert dc._high_priority_demand(rows, threshold=1) == 2
