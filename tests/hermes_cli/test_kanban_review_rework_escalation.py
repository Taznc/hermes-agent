"""Escalate repeated review rework instead of looping one implementer."""

from __future__ import annotations

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd


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


def test_escalation_preserves_operator_set_model_override(all_assignees_spawnable):
    """An operator's explicit model pin must survive rework escalation — only a
    classifier-picked (route_source='mechanical') override may be cleared."""
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="hard rework", assignee="implementer",
            model_override="claude-sonnet-5", provider_override="anthropic",
        )
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
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "debugger"
        assert task.model_override == "claude-sonnet-5"
        assert task.provider_override == "anthropic"

        events = kb.list_events(conn, task_id)
        assigned = [e for e in events if e.kind == "assigned"][-1]
        assert assigned.payload.get("preserved_overrides") is True


def test_escalation_clears_classifier_picked_model_override(all_assignees_spawnable):
    """A create-time routing-classifier override (route_source='mechanical')
    is not an operator decision and may still be cleared on escalation, same
    as before this card's fix."""
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="hard rework", assignee="implementer",
            model_override="gpt-5.4-mini", provider_override="openai-codex",
            route_source="mechanical", route_name="mechanical",
        )
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
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "debugger"
        assert task.model_override is None
        assert task.provider_override is None

        events = kb.list_events(conn, task_id)
        assigned = [e for e in events if e.kind == "assigned"][-1]
        assert assigned.payload.get("preserved_overrides") is not True


def test_escalation_preserves_operator_set_via_set_model_override_cli(all_assignees_spawnable):
    """A model override set later via `kanban set-model` (kb.set_model_override)
    is unambiguously operator intent even if the task was create-time routed
    through the classifier — the LATEST override-setting event wins."""
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="hard rework", assignee="implementer",
            model_override="gpt-5.4-mini", provider_override="openai-codex",
            route_source="mechanical", route_name="mechanical",
        )
        assert kb.set_model_override(conn, task_id, "claude-sonnet-5", provider="anthropic")
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
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.model_override == "claude-sonnet-5"
        assert task.provider_override == "anthropic"


def test_third_changes_request_hits_review_round_cap_and_blocks(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="runaway rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "third and final"})
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_review_rounds=3,
        )

        assert result.spawned == []
        assert result.blocked_review_round_cap == [(task_id, 3)]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "review_round_cap"

        events = kb.list_events(conn, task_id)
        cap_event = [e for e in events if e.kind == "review_round_cap"][-1]
        assert cap_event.payload.get("changes_rounds") == 3
        assert cap_event.payload.get("max_review_rounds") == 3
        assert cap_event.payload.get("reason") == "third and final"


def test_second_changes_request_stays_under_the_cap_and_dispatches(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="normal rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_review_rounds=3,
        )

        assert result.blocked_review_round_cap == []
        assert [tid for tid, _who, _ws in result.spawned] == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"


def test_max_review_rounds_zero_disables_the_cap(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="unbounded rework", assignee="implementer")
        for i in range(5):
            kb._append_event(conn, task_id, "changes_requested", {"reason": f"round {i}"})
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_review_rounds=0,
        )

        assert result.blocked_review_round_cap == []
        assert [tid for tid, _who, _ws in result.spawned] == [task_id]


def test_manual_reassignment_bypasses_the_review_round_cap(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="operator-rescued rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "third"})
        conn.commit()
        assert kb.assign_task(conn, task_id, "specialist") is True

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_review_rounds=3,
        )

        assert result.blocked_review_round_cap == []
        assert [tid for tid, who, _ws in result.spawned] == [task_id]
        assert result.spawned[0][1] == "specialist"


def test_resolve_dispatch_caps_includes_max_review_rounds(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"max_review_rounds": 5}},
    )

    assert kbd.resolve_dispatch_caps().max_review_rounds == 5


def test_resolve_dispatch_caps_max_review_rounds_defaults_to_three(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})

    assert kbd.resolve_dispatch_caps().max_review_rounds == 3


def test_show_surfaces_review_round_cap_block(all_assignees_spawnable):
    """AC3: `hermes kanban show <id>` surfaces the review_round_cap block via
    the task's status and its review_round_cap event."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="runaway rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "root cause unclear"})
        conn.commit()

        kbd.dispatch_once(conn, spawn_fn=_spawn, max_review_rounds=3)

    output = kc.run_slash(f"show {task_id}")

    assert "status:    blocked" in output
    assert "review_round_cap" in output
    assert "root cause unclear" in output


def test_diagnostics_surfaces_review_round_cap_block(all_assignees_spawnable):
    """AC3: `hermes kanban diagnostics` surfaces the review_round_cap block kind,
    the round count, and the last `changes_requested` reason via a dedicated
    diagnostic rule (compute_task_diagnostics returned [] before this rule
    existed, even though the task was correctly blocked)."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="runaway rework", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "third and final"})
        conn.commit()

        result = kbd.dispatch_once(conn, spawn_fn=_spawn, max_review_rounds=3)
        assert result.blocked_review_round_cap == [(task_id, 3)]

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    diags = kd.compute_task_diagnostics(task, events, runs)

    matching = [d for d in diags if d.kind == "review_round_cap"]
    assert len(matching) == 1
    diag = matching[0]
    assert diag.data["changes_rounds"] == 3
    assert diag.data["max_review_rounds"] == 3
    assert diag.data["last_reason"] == "third and final"


def test_diagnostics_stays_empty_for_a_normal_blocked_task(all_assignees_spawnable):
    """Negative case: a task blocked for an unrelated reason must never surface
    the review_round_cap diagnostic (it keys off block_kind, not merely being
    blocked with SOME review_round_cap event lying around in old history)."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="ordinary block", assignee="implementer")
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "third"})
        conn.commit()
        kbd.dispatch_once(conn, spawn_fn=_spawn, max_review_rounds=3)

        # Unblock, then block again for an unrelated reason.
        assert kb.unblock_task(conn, task_id) is True
        assert kb.block_task(conn, task_id, reason="unrelated: waiting on credentials") is True

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    diags = kd.compute_task_diagnostics(task, events, runs)

    assert not any(d.kind == "review_round_cap" for d in diags)
