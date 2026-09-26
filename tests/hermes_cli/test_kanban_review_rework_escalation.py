"""Escalate repeated review rework instead of looping one implementer."""

from __future__ import annotations

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd
from hermes_cli.plugins import get_plugin_manager


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
            model_override="gpt-5.6-terra", provider_override="openai-codex",
            reasoning_effort="medium",
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
        assert task.model_override == "gpt-5.6-terra"
        assert task.provider_override == "openai-codex"

        events = kb.list_events(conn, task_id)
        assigned = [e for e in events if e.kind == "assigned"][-1]
        assert assigned.payload.get("preserved_overrides") is True


def test_forced_route_is_not_transferred_to_escalation_profile(all_assignees_spawnable):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="forced hard rework", assignee="implementer",
            model_override="gpt-5.6-terra", provider_override="openai-codex",
            reasoning_effort="high", policy_force=True,
            policy_force_reason="operator exception", policy_forced_by="operator",
        )
        kb._append_event(conn, task_id, "changes_requested", {"reason": "first"})
        kb._append_event(conn, task_id, "changes_requested", {"reason": "second"})
        conn.commit()

        result = kbd.dispatch_once(
            conn, spawn_fn=_spawn, review_rework_escalation_profile="debugger",
        )

        assert result.auto_escalated_rework == [
            (task_id, "implementer", "debugger", 2)
        ]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "debugger"
        assert task.model_override is None
        assert task.provider_override is None
        assert task.reasoning_effort is None
        assert task.policy_forced_by is None
        assert task.policy_force_reason is None
        assert task.policy_force_route is None


def test_escalation_clears_classifier_picked_model_override(all_assignees_spawnable):
    """A create-time routing-classifier override (route_source='mechanical')
    is not an operator decision and may still be cleared on escalation, same
    as before this card's fix."""
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="hard rework", assignee="implementer",
            model_override="gpt-5.6-terra", provider_override="openai-codex",
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
            model_override="gpt-5.6-terra", provider_override="openai-codex",
            reasoning_effort="medium",
            route_source="mechanical", route_name="mechanical",
        )
        assert kb.set_model_override(conn, task_id, "gpt-5.6-sol", provider="openai-codex")
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
        assert task.model_override == "gpt-5.6-sol"
        assert task.provider_override == "openai-codex"


def test_third_changes_request_hits_review_round_cap_and_blocks(all_assignees_spawnable):
    """No escalation profile configured: the cap is a plain hard stop."""
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


def test_cap_only_tick_is_classified_as_activity_not_idle(all_assignees_spawnable):
    """A tick whose ONLY transition is a review-round cap did real work: it
    blocked a card. ``_TICK_ACTIVITY_FIELDS`` drives the dispatch-tick hook's
    ``outcome``, so omitting ``blocked_review_round_cap`` reported that tick as
    ``idle`` to every observer — the board moved but telemetry said nothing
    happened."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    ticks: list[dict] = []
    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(
        lambda **kw: ticks.append(kw)
    )
    try:
        with kbc.connect() as conn:
            task_id = kb.create_task(conn, title="cap-only tick", assignee="implementer")
            for reason in ("first", "second", "third"):
                kb._append_event(conn, task_id, "changes_requested", {"reason": reason})
            conn.commit()

            result = kbd.dispatch_once(conn, spawn_fn=_spawn, max_review_rounds=3)
    finally:
        mgr._hooks = saved

    # The cap is the ONLY transition, so an "ok" outcome cannot come from
    # another counter being incidentally non-empty.
    assert result.blocked_review_round_cap == [(task_id, 3)]
    other_activity = {
        field: getattr(result, field)
        for field in kb._TICK_ACTIVITY_FIELDS
        if field != "blocked_review_round_cap" and getattr(result, field)
    }
    assert other_activity == {}

    assert "blocked_review_round_cap" in kb._TICK_ACTIVITY_FIELDS
    assert [kw["outcome"] for kw in ticks] == ["ok"]


# ---------------------------------------------------------------------------
# At the cap: one terminal escalated round BEFORE the hard block
# ---------------------------------------------------------------------------


def _card_at_cap(conn, *, assignee: str, rounds: int = 2) -> str:
    task_id = kb.create_task(conn, title="at the cap", assignee=assignee)
    for i in range(rounds):
        kb._append_event(conn, task_id, "changes_requested", {"reason": f"round {i + 1}"})
    conn.commit()
    return task_id


def test_at_cap_escalates_to_profile_for_one_terminal_round_instead_of_blocking(
    all_assignees_spawnable,
):
    """A fully AI-operated board must not need a human at the cap while a
    specialist is configured: the card goes to the escalation profile for ONE
    more round, stays dispatchable, and carries a durable review_cap_escalated
    event — it is NOT blocked."""
    with kbc.connect() as conn:
        task_id = _card_at_cap(conn, assignee="implementer", rounds=2)

        result = kbd.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_review_rounds=2,
            review_rework_escalation_profile="debugger",
        )

        assert result.blocked_review_round_cap == []
        assert result.escalated_review_cap == [(task_id, "implementer", "debugger", 2)]
        assert result.auto_escalated_rework == []
        assert [(tid, who) for tid, who, _ws in result.spawned] == [(task_id, "debugger")]

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.block_kind != "review_round_cap"
        assert task.assignee == "debugger"

        events = kb.list_events(conn, task_id)
        assert not any(e.kind == "review_round_cap" for e in events)
        escalated = [e for e in events if e.kind == "review_cap_escalated"]
        assert len(escalated) == 1
        assert escalated[0].payload == {
            "changes_rounds": 2,
            "max_review_rounds": 2,
            "escalation_profile": "debugger",
            "previous_assignee": "implementer",
        }

        # The escalated worker can see it is on the terminal pass.
        packet = kb.build_worker_task_packet(conn, task_id).to_dict()
        assert packet["review"]["terminal_rework"] is True


def test_at_cap_blocks_when_the_escalated_round_also_requests_changes(
    all_assignees_spawnable,
):
    """The escalation profile already owns the card at the cap, so its one
    terminal round came back changes_requested: now — and only now — the
    hard stop fires."""
    with kbc.connect() as conn:
        task_id = _card_at_cap(conn, assignee="implementer", rounds=2)
        first = kbd.dispatch_once(
            conn, spawn_fn=_spawn, max_review_rounds=2,
            review_rework_escalation_profile="debugger",
        )
        assert first.escalated_review_cap == [(task_id, "implementer", "debugger", 2)]

        # The specialist's round ends in yet another changes request.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL WHERE id = ?",
                (task_id,),
            )
            kb._append_event(conn, task_id, "changes_requested", {"reason": "still wrong"})

        second = kbd.dispatch_once(
            conn, spawn_fn=_spawn, max_review_rounds=2,
            review_rework_escalation_profile="debugger",
        )

        assert second.escalated_review_cap == []
        assert second.spawned == []
        assert second.blocked_review_round_cap == [(task_id, 3)]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "review_round_cap"
        assert task.assignee == "debugger"
        cap_event = [e for e in kb.list_events(conn, task_id) if e.kind == "review_round_cap"][-1]
        assert cap_event.payload.get("reason") == "still wrong"
        # Exactly one escalated round was granted — the block did not re-escalate.
        assert sum(
            1 for e in kb.list_events(conn, task_id) if e.kind == "review_cap_escalated"
        ) == 1


def test_at_cap_without_escalation_profile_blocks_immediately(all_assignees_spawnable):
    """Existing behaviour preserved: no specialist configured means the cap is
    still a hard block on the implementer, with no escalation event."""
    with kbc.connect() as conn:
        task_id = _card_at_cap(conn, assignee="implementer", rounds=2)

        result = kbd.dispatch_once(
            conn, spawn_fn=_spawn, max_review_rounds=2,
            review_rework_escalation_profile="",
        )

        assert result.escalated_review_cap == []
        assert result.spawned == []
        assert result.blocked_review_round_cap == [(task_id, 2)]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "review_round_cap"
        assert task.assignee == "implementer"
        assert not any(
            e.kind == "review_cap_escalated" for e in kb.list_events(conn, task_id)
        )
        assert kb.build_worker_task_packet(conn, task_id).to_dict()["review"][
            "terminal_rework"
        ] is False


def test_manual_reassignment_at_cap_overrides_escalation_too(all_assignees_spawnable):
    """An operator's explicit routing after the last changes request wins over
    the cap-time escalation exactly as it wins over the block."""
    with kbc.connect() as conn:
        task_id = _card_at_cap(conn, assignee="implementer", rounds=2)
        assert kb.assign_task(conn, task_id, "specialist") is True

        result = kbd.dispatch_once(
            conn, spawn_fn=_spawn, max_review_rounds=2,
            review_rework_escalation_profile="debugger",
        )

        assert result.escalated_review_cap == []
        assert result.blocked_review_round_cap == []
        assert [(tid, who) for tid, who, _ws in result.spawned] == [(task_id, "specialist")]


def test_terminal_rework_flag_resets_after_completion(all_assignees_spawnable):
    """Same work-epoch rule as the round counter: a card that was escalated at
    the cap, completed, and later reopened is not still 'terminal'."""
    with kbc.connect() as conn:
        task_id = _card_at_cap(conn, assignee="implementer", rounds=2)
        kbd.dispatch_once(
            conn, spawn_fn=_spawn, max_review_rounds=2,
            review_rework_escalation_profile="debugger",
        )
        assert kb.build_worker_task_packet(conn, task_id).to_dict()["review"][
            "terminal_rework"
        ] is True
        assert kb.complete_task(conn, task_id, summary="done at last") is True

        assert kb.build_worker_task_packet(conn, task_id).to_dict()["review"][
            "terminal_rework"
        ] is False
