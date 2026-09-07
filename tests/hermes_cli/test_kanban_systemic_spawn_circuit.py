"""Board-wide circuit for a proven restart-safe scope prerequisite outage."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tools import process_registry


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _to_review(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))


def test_scope_prerequisite_fault_pauses_board_without_burning_cards(
    kanban_home, all_assignees_spawnable,
):
    """The first typed scope failure defers every ready/review card untouched."""
    board = "shared-scope-outage"
    spawn_calls: list[str] = []

    def unavailable(task, _workspace, board=None):
        spawn_calls.append(task.id)
        raise process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")

    with kbc.connect(board=board) as conn:
        ready_first = kb.create_task(conn, title="first", assignee="worker")
        ready_second = kb.create_task(conn, title="second", assignee="worker")
        review = kb.create_task(conn, title="review", assignee="reviewer")
        _to_review(conn, review)
        conn.commit()

        result = kbd.dispatch_once(conn, board=board, spawn_fn=unavailable)

        assert spawn_calls == [ready_first]
        assert result.dispatch_paused is not None
        assert result.dispatch_paused["reason"] == "restart_safe_scope_unavailable"
        assert result.dispatch_paused["fault_code"] == "systemd_user_scope_unavailable"
        assert result.dispatch_paused["trigger_task_id"] == ready_first
        for task_id, expected_status in (
            (ready_first, "ready"),
            (ready_second, "ready"),
            (review, "review"),
        ):
            task = kb.get_task(conn, task_id)
            assert task is not None
            assert task.status == expected_status
            assert task.consecutive_failures == 0
            assert task.last_failure_error is None
        events = kb.list_events(conn, ready_first)
        tripped = [event for event in events if event.kind == "dispatch_circuit_tripped"]
        assert len(tripped) == 1
        assert tripped[0].payload["fault_code"] == "systemd_user_scope_unavailable"

    # A new dispatcher connection sees the durable board pause and does not call
    # a repaired/otherwise-valid spawner until an operator explicitly resumes.
    with kbc.connect(board=board) as conn:
        paused = kbd.dispatch_once(conn, board=board, spawn_fn=lambda *_a, **_k: pytest.fail("must not spawn"))
        assert paused.dispatch_paused is not None
        assert paused.dispatch_paused["trigger_task_id"] == ready_first

    cleared = kbd.resume_dispatch(board)
    assert cleared["was_paused"] is True
    with kbc.connect(board=board) as conn:
        resumed = kbd.dispatch_once(conn, board=board, spawn_fn=lambda *_a, **_k: 4242, max_spawn=1)
    resumed_ids = [task_id for task_id, _assignee, _workspace in resumed.spawned]
    # The existing review-reservation policy may give the one available slot to
    # review; recovery proves that an explicit resume permits exactly one safe
    # probe rather than silently re-arming while the circuit remains tripped.
    assert len(resumed_ids) == 1
    assert resumed_ids[0] in {ready_first, ready_second, review}


def test_scope_classifier_uses_the_typed_boundary_not_error_text():
    typed = process_registry.RestartSafeScopeUnavailable("scope is unavailable")

    assert kbd._is_shared_launcher_prerequisite_fault(typed) is True
    assert kbd._is_shared_launcher_prerequisite_fault(
        RuntimeError("systemd-run --user --scope is unavailable")
    ) is False


def test_unrelated_spawn_failure_keeps_per_task_failure_semantics(
    kanban_home, all_assignees_spawnable,
):
    """Only the typed launch-prerequisite boundary trips the board circuit."""
    board = "ordinary-failure"
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="credentials", assignee="worker")

        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("credential rejected")),
        )

        task = kb.get_task(conn, task_id)
        assert result.dispatch_paused is None
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert task.last_failure_error == "credential rejected"


def test_scope_prerequisite_fault_from_review_restores_review_neutrally(
    kanban_home, all_assignees_spawnable,
):
    """A review-lane outage must preserve the review handoff and its budget."""
    board = "review-scope-outage"

    def unavailable(*_args, **_kwargs):
        raise process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")

    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        _to_review(conn, task_id)
        conn.commit()

        result = kbd.dispatch_once(conn, board=board, spawn_fn=unavailable)

        task = kb.get_task(conn, task_id)
        assert result.dispatch_paused is not None
        assert task is not None
        assert task.status == "review"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
        outcome = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()[0]
        assert outcome == "spawn_deferred"


def test_contended_typed_outage_makes_only_one_spawn_attempt(
    kanban_home, all_assignees_spawnable,
):
    """A second dispatcher must not enter the same typed outage while one tick owns the board."""
    board = "contended-scope-outage"
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    spawn_calls: list[str] = []

    def unavailable(task, _workspace, board=None):
        spawn_calls.append(task.id)
        entered.set()
        assert release.wait(timeout=5)
        raise process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")

    with kbc.connect(board=board) as conn:
        kb.create_task(conn, title="first", assignee="worker")
        kb.create_task(conn, title="second", assignee="worker")
        conn.commit()

    first_result: list[kb.DispatchResult] = []

    def first_tick():
        with kbc.connect(board=board) as conn:
            first_result.append(kbd.dispatch_once(conn, board=board, spawn_fn=unavailable))
        finished.set()

    thread = threading.Thread(target=first_tick)
    thread.start()
    assert entered.wait(timeout=5)
    with kbc.connect(board=board) as conn:
        second = kbd.dispatch_once(conn, board=board, spawn_fn=unavailable)
    release.set()
    assert finished.wait(timeout=5)
    thread.join(timeout=5)

    assert second.skipped_locked is True
    assert len(spawn_calls) == 1
    assert first_result[0].dispatch_paused is not None


def test_durable_pause_stops_tick_even_when_trigger_claim_was_reconciled(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Losing the task-row CAS after persisting a pause must not start siblings."""
    board = "cas-lost-scope-outage"
    spawn_calls: list[str] = []
    original_write = kbd._write_dispatch_pause

    def pause_then_reconcile(board_arg, reason, **details):
        state = original_write(board_arg, reason, **details)
        with kbc.connect(board=board) as other:
            other.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL "
                "WHERE id = ?",
                (details["trigger_task_id"],),
            )
            other.commit()
        return state

    def unavailable(task, _workspace, board=None):
        spawn_calls.append(task.id)
        raise process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")

    monkeypatch.setattr(kbd, "_write_dispatch_pause", pause_then_reconcile)
    with kbc.connect(board=board) as conn:
        kb.create_task(conn, title="first", assignee="worker")
        kb.create_task(conn, title="second", assignee="worker")
        conn.commit()
        result = kbd.dispatch_once(conn, board=board, spawn_fn=unavailable)

    assert result.dispatch_paused is not None
    assert spawn_calls == [spawn_calls[0]]


def test_pause_persistence_failure_releases_trigger_without_charging_it(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A pause-write error must not strand a claimed task or consume its retry budget."""
    board = "pause-write-failure"
    monkeypatch.setattr(
        kbd, "_write_dispatch_pause", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="first", assignee="worker")
        conn.commit()
        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")
            ),
        )
        task = kb.get_task(conn, task_id)
        outcome = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()[0]

    assert result.dispatch_paused is not None
    assert result.dispatch_paused["reason"] == "pause_persistence_failed"
    assert task is not None
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert outcome == "spawn_deferred"


def test_resume_refuses_to_delete_pause_while_a_dispatch_tick_holds_the_board_lock(
    kanban_home,
):
    """Recovery cannot unlink a newer pause written by an in-flight tick."""
    board = "resume-lock-race"
    state = kbd._write_dispatch_pause(board, "test_pause", recovery="explicit retry")
    db_path = kb.kanban_db_path(board=board)

    with kbc._dispatch_tick_lock(db_path) as held:
        assert held is True
        resumed = kbd.resume_dispatch(board)

    assert resumed == {
        "was_paused": True,
        "resumed": False,
        "reason": "dispatch_in_progress",
    }
    assert kbd.read_dispatch_pause(board) == state


def test_spawn_deferred_is_neutral_to_the_protocol_violation_streak(kanban_home):
    """A host outage between protocol failures must not replenish their bounded retry budget."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="mixed", assignee="worker")
        now = int(time.time())
        for index, outcome in enumerate(("crashed", "crashed", "spawn_deferred")):
            metadata = {"protocol_violation": True} if outcome == "crashed" else {}
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, metadata, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, "worker", outcome, outcome, json.dumps(metadata), now + index, now + index),
            )
        conn.commit()

        assert kbd._protocol_violation_streak(conn, task_id) == 2


def test_old_scope_error_text_still_exhausts_each_task_budget_without_the_typed_boundary(
    kanban_home, all_assignees_spawnable,
):
    """Faithful pre-fix behavior: the actual old RuntimeError charges every card."""
    board = "old-runtime-error"
    spawn_calls: list[str] = []
    old_error = (
        "cannot create restart-safe systemd scope for gateway child: "
        "systemd-run --user --scope is unavailable"
    )

    def old_unavailable(task, _workspace, board=None):
        spawn_calls.append(task.id)
        raise RuntimeError(old_error)

    with kbc.connect(board=board) as conn:
        task_ids = [kb.create_task(conn, title=f"task-{i}", assignee="worker") for i in range(3)]
        conn.commit()
        result = kbd.dispatch_once(conn, board=board, spawn_fn=old_unavailable)

        assert result.dispatch_paused is None
        assert spawn_calls == task_ids
        for task_id in task_ids:
            task = kb.get_task(conn, task_id)
            assert task is not None
            assert task.consecutive_failures == 1
            assert task.last_failure_error == old_error
