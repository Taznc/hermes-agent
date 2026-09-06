"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


# ---------------------------------------------------------------------------
# stranded_in_ready — concurrency-aware (board/profile at capacity is queued,
# not stranded; the operator has no action to take, so the rule must not fire)
# ---------------------------------------------------------------------------


def test_stranded_in_ready_suppressed_when_board_at_global_cap():
    """A board saturated at kanban.max_in_progress is healthy and busy, not
    stranded: this is the exact false-positive from the bug report (t_ff7c7888
    sat in ready with 6/6 workers alive and got flagged as if the dispatcher
    were down). No operator action exists, so the diagnostic must not fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 65 * 60)]  # well past the 30 min threshold
    concurrency = {
        "max_in_progress": 6, "max_in_progress_per_profile": None,
        "total_running": 6, "running_by_assignee": {},
    }
    diags = kd.compute_task_diagnostics(task, events, [], now=now, concurrency=concurrency)
    assert not [d for d in diags if d.kind == "stranded_in_ready"]


def test_stranded_in_ready_still_fires_when_a_slot_is_free():
    """Same age, but the host has headroom: a genuinely unclaimed task past
    the threshold with capacity to run it IS worth flagging — the rule must
    keep working when the board isn't at cap."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 65 * 60)]
    concurrency = {
        "max_in_progress": 6, "max_in_progress_per_profile": None,
        "total_running": 3, "running_by_assignee": {},
    }
    diags = kd.compute_task_diagnostics(task, events, [], now=now, concurrency=concurrency)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_suppressed_when_assignees_per_profile_cap_saturated():
    """A per-profile cap can saturate for one assignee while the global cap
    still has headroom (other profiles are the ones filling it). The card
    for the saturated assignee must not fire even though a global slot is
    technically free."""
    now = 100_000
    task = _task(status="ready", assignee="alice", claim_lock=None)
    events = [_event("created", ts=now - 65 * 60)]
    concurrency = {
        "max_in_progress": 10, "max_in_progress_per_profile": 4,
        "total_running": 5,  # global headroom: 5 < 10
        "running_by_assignee": {"alice": 4, "bob": 1},  # alice alone is at her cap
    }
    diags = kd.compute_task_diagnostics(task, events, [], now=now, concurrency=concurrency)
    assert not [d for d in diags if d.kind == "stranded_in_ready"]


def test_stranded_in_ready_fires_for_a_different_assignee_under_the_cap():
    """The per-profile cap suppression is scoped to the saturated assignee
    only — a sibling card assigned to a profile with headroom must still be
    flaggable."""
    now = 100_000
    task = _task(status="ready", assignee="bob", claim_lock=None)
    events = [_event("created", ts=now - 65 * 60)]
    concurrency = {
        "max_in_progress": 10, "max_in_progress_per_profile": 4,
        "total_running": 5,
        "running_by_assignee": {"alice": 4, "bob": 1},
    }
    diags = kd.compute_task_diagnostics(task, events, [], now=now, concurrency=concurrency)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_ignores_missing_concurrency_snapshot():
    """No concurrency context (e.g. a low-level caller with no live DB
    connection) preserves the old age-only behavior rather than silently
    suppressing everything."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 65 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


# ---------------------------------------------------------------------------
# repeated_failures rule — threshold must track the breaker's effective limit
#
# _record_task_failure (kanban_db_dispatch.py) resolves its trip threshold as
# task.max_retries if set, else the dispatcher's failure_limit. The diagnostic
# must resolve the SAME threshold (kanban_db_dispatch.effective_failure_limit)
# so a task the breaker already blocked can never surface zero diagnostics.
# ---------------------------------------------------------------------------


def test_repeated_failures_fires_when_task_max_retries_below_global_limit():
    """Regression for the proven defect: a task with its own max_retries set
    BELOW the global failure_limit trips the breaker at that lower count.
    The diagnostic must still fire — this is the exact shape of live task
    t_44ca59a3 (max_retries=1, consecutive_failures=1, global limit higher).
    Fails on the unfixed source, which derives its threshold from
    cfg["failure_threshold"]/failure_limit alone and never looks at
    task.max_retries, so failures(1) < threshold(3) suppresses the rule.
    """
    now = int(time.time())
    task = _task(status="blocked", consecutive_failures=1, max_retries=1,
                 last_failure_error="pid 704578 not alive")
    diags = kd.compute_task_diagnostics(
        task, [], [], now=now, config={"failure_limit": 3},
    )
    fires = [d for d in diags if d.kind == "repeated_failures"]
    assert len(fires) == 1, (
        "repeated_failures must fire once a task's own max_retries has "
        "tripped the breaker, even though consecutive_failures (1) is below "
        "the global failure_limit (3)"
    )
    assert fires[0].data["consecutive_failures"] == 1
    assert fires[0].data["failure_threshold"] == 1
    assert fires[0].data["limit_source"] == "task"


def test_repeated_failures_control_global_limit_path_unchanged():
    """Control: a task with NO per-task max_retries override still gates on
    the global failure_limit exactly as before — one failure short of the
    limit produces nothing, reaching it produces the diagnostic."""
    now = int(time.time())
    short = _task(status="blocked", consecutive_failures=1, max_retries=None)
    diags_short = kd.compute_task_diagnostics(
        short, [], [], now=now, config={"failure_limit": 2},
    )
    assert not [d for d in diags_short if d.kind == "repeated_failures"]

    at_limit = _task(status="blocked", consecutive_failures=2, max_retries=None)
    diags_at_limit = kd.compute_task_diagnostics(
        at_limit, [], [], now=now, config={"failure_limit": 2},
    )
    fires = [d for d in diags_at_limit if d.kind == "repeated_failures"]
    assert len(fires) == 1
    assert fires[0].data["failure_threshold"] == 2
    assert fires[0].data["limit_source"] == "dispatcher"


def test_repeated_failures_threshold_matches_breaker_effective_limit():
    """Invariant, not a frozen literal: for any (task max_retries, dispatcher
    failure_limit) pair, the diagnostic's resolved threshold and limit_source
    equal kanban_db_dispatch.effective_failure_limit's own resolution — the
    two paths cannot disagree because the diagnostic calls the same function.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    cases = [
        (None, 2), (None, 5), (1, 2), (1, 5), (3, 2), (7, 1), (2, 2),
    ]
    for task_max_retries, dispatcher_limit in cases:
        task = _task(status="blocked", max_retries=task_max_retries)
        threshold, limit_source, _display = kd._effective_repeated_failures_threshold(
            task, {**kd.DEFAULT_CONFIG, "failure_limit": dispatcher_limit,
                   "failure_threshold": dispatcher_limit},
        )
        expected_limit, expected_source = kbd.effective_failure_limit(
            task_max_retries, dispatcher_limit,
        )
        assert (threshold, limit_source) == (expected_limit, expected_source), (
            task_max_retries, dispatcher_limit,
        )




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
