"""Escalate repeated review rework instead of looping one implementer."""

from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _spawn(_task, _workspace, board=None):
    return 54321


def test_resolve_dispatch_caps_includes_rework_escalation_profile(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"review_rework_escalation_profile": " debugger "}},
    )

    assert kbd.resolve_dispatch_caps().review_rework_escalation_profile == "debugger"


def test_second_changes_request_routes_rework_to_escalation_profile(
    all_assignees_spawnable,
):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="hard rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            review_rework_escalation_profile="debugger",
        )

        assert result.auto_escalated_rework == [
            (task_id, "implementer", "debugger", 2)
        ]
        assert result.spawned[0][1] == "debugger"
        assert kb.get_task(conn, task_id).assignee == "debugger"


def test_first_changes_request_stays_with_implementer(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="normal rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            review_rework_escalation_profile="debugger",
        )

        assert result.auto_escalated_rework == []
        assert result.spawned[0][1] == "implementer"


def test_manual_assignment_after_second_request_overrides_auto_escalation(
    all_assignees_spawnable,
):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="operator-routed rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        conn.commit()
        assert kb.assign_task(conn, task_id, "specialist") is True

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            review_rework_escalation_profile="debugger",
        )

        assert result.auto_escalated_rework == []
        assert result.spawned[0][1] == "specialist"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "specialist"


def test_completed_reopened_card_does_not_reuse_historical_rework_rounds(
    all_assignees_spawnable,
):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="new work epoch", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        conn.commit()
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, summary="old work complete") is True
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
            kb._append_event(conn, task_id, "status", {"status": "ready"})

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            review_rework_escalation_profile="debugger",
        )

        assert result.auto_escalated_rework == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "implementer"
