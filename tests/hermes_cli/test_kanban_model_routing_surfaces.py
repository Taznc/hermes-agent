"""Kanban model-routing integration tests across create surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from argparse import Namespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban as kanban_cli
from hermes_cli.kanban_model_routing import KanbanModelRouteDecision
from tools import kanban_tools
from plugins.kanban.dashboard import plugin_api


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def routing_decision():
    return KanbanModelRouteDecision(
        route_source="mechanical",
        route_name="mechanical",
        model_override="gpt-5.4-mini",
        provider_override="openai-codex",
        reasoning_effort="medium",
    )


@pytest.fixture
def router_client(kanban_home):
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _latest_task_by_title(title: str):
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE title = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (title,),
        ).fetchone()
        assert row is not None
        return kb.Task.from_row(row)


def test_create_paths_share_routing_and_show_provenance(
    kanban_home, routing_decision, monkeypatch, router_client, capsys
):
    calls: list[dict] = []

    def _fake_resolver(**kwargs):
        calls.append(kwargs)
        return routing_decision

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _fake_resolver)

    cli_args = Namespace(
        workspace=None,
        branch=None,
        max_runtime=None,
        max_retries=None,
        title="CLI task",
        body="compact docs tweak",
        assignee="claudeprimary",
        created_by="tester",
        tenant=None,
        priority=0,
        parent=[],
        triage=False,
        idempotency_key=None,
        skills=[],
        model_override=None,
        provider_override=None,
        reasoning_effort=None,
        goal_mode=False,
        goal_max_turns=None,
        initial_status="running",
        json=False,
    )
    assert kanban_cli._cmd_create(cli_args) == 0
    cli_task = _latest_task_by_title("CLI task")
    assert cli_task.model_override == "gpt-5.4-mini"
    assert cli_task.provider_override == "openai-codex"
    assert cli_task.reasoning_effort == "medium"
    assert cli_task.route_source == "mechanical"
    assert cli_task.route_name == "mechanical"

    tool_response = kanban_tools._handle_create({
        "title": "Tool task",
        "body": "compact docs tweak",
        "assignee": "claudeprimary",
        "model": None,
        "provider": None,
        "reasoning_effort": None,
    })
    tool_payload = json.loads(tool_response)
    assert tool_payload["ok"] is True
    tool_task = _latest_task_by_title("Tool task")
    assert tool_task.route_source == "mechanical"
    assert tool_task.route_name == "mechanical"
    assert tool_task.reasoning_effort == "medium"

    response = router_client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Dashboard task",
            "body": "compact docs tweak",
            "assignee": "claudeprimary",
        },
    )
    assert response.status_code == 200
    dashboard_task = response.json()["task"]
    assert dashboard_task["route_source"] == "mechanical"
    assert dashboard_task["route_name"] == "mechanical"
    assert dashboard_task["reasoning_effort"] == "medium"

    assert len(calls) == 3
    for call in calls:
        assert set(call) == {
            "title",
            "body",
            "explicit_model",
            "explicit_provider",
            "explicit_reasoning_effort",
        }
        assert call["body"] == "compact docs tweak"

    capsys.readouterr()
    show_args = Namespace(task_id=cli_task.id, json=True, filter_runs=None)
    assert kanban_cli._cmd_show(show_args) == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["task"]["route_source"] == "mechanical"
    assert show_payload["task"]["route_name"] == "mechanical"
    assert show_payload["task"]["reasoning_effort"] == "medium"


def test_route_provenance_survives_review_roundtrip(kanban_home, routing_decision):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Round-trip provenance",
            body="keep this route stable",
            assignee="claudeprimary",
            route_source=routing_decision.route_source,
            route_name=routing_decision.route_name,
            model_override=routing_decision.model_override,
            provider_override=routing_decision.provider_override,
            reasoning_effort=routing_decision.reasoning_effort,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.route_source == "mechanical"
        kb.claim_task(conn, task_id)
        task = kb.get_task(conn, task_id)
        assert task is not None
        run_id = task.current_run_id
        assert run_id is not None
        assert kb.request_review(conn, task_id, summary="ready", expected_run_id=run_id, reviewer="reviewer")
        reviewed = kb.get_task(conn, task_id)
        assert reviewed is not None
        assert reviewed.route_source == "mechanical"
        assert reviewed.route_name == "mechanical"


def test_routing_classifier_runs_only_on_create_across_lifecycle(
    kanban_home, routing_decision, monkeypatch
):
    calls: list[dict] = []

    def _fake_resolver(**kwargs):
        calls.append(kwargs)
        return routing_decision

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _fake_resolver)

    cli_args = Namespace(
        workspace=None,
        branch=None,
        max_runtime=None,
        max_retries=None,
        title="Lifecycle routing",
        body="compact docs tweak",
        assignee="claudeprimary",
        created_by="tester",
        tenant=None,
        priority=0,
        parent=[],
        triage=False,
        idempotency_key=None,
        skills=[],
        model_override=None,
        provider_override=None,
        reasoning_effort=None,
        goal_mode=False,
        goal_max_turns=None,
        initial_status="running",
        json=False,
    )
    assert kanban_cli._cmd_create(cli_args) == 0
    task = _latest_task_by_title("Lifecycle routing")
    assert len(calls) == 1
    assert task.route_source == "mechanical"
    assert task.reasoning_effort == "medium"

    with kbc.connect() as conn:
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None

        conn.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (task.id,))
        assert kb.release_stale_claims(conn) == 1
        assert len(calls) == 1

        retried = kb.claim_task(conn, task.id)
        assert retried is not None
        assert kb.request_review(
            conn,
            task.id,
            summary="ready for review",
            reviewer="reviewer",
            expected_run_id=retried.current_run_id,
        ) is True
        assert len(calls) == 1

        reviewed = kb.claim_review_task(conn, task.id)
        assert reviewed is not None
        ok, implementer = kb.request_changes(
            conn, task.id, reason="needs another pass", expected_run_id=reviewed.current_run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"
        assert len(calls) == 1

        assert kb.block_task(conn, task.id, reason="pause") is True
        assert kb.unblock_task(conn, task.id) is True
        assert len(calls) == 1

        retried_again = kb.claim_task(conn, task.id)
        assert retried_again is not None
        assert kb.request_review(
            conn,
            task.id,
            summary="re-review",
            reviewer="reviewer",
            expected_run_id=retried_again.current_run_id,
        ) is True
        assert len(calls) == 1


def test_idempotent_replays_skip_routing_across_create_surfaces(
    kanban_home, monkeypatch, router_client
):
    """An existing idempotency key must return without spending a classifier call."""
    with kbc.connect() as conn:
        cli_id = kb.create_task(conn, title="CLI existing", assignee="claudeprimary", idempotency_key="cli-key")
        tool_id = kb.create_task(conn, title="Tool existing", assignee="claudeprimary", idempotency_key="tool-key")
        dashboard_id = kb.create_task(
            conn, title="Dashboard existing", assignee="claudeprimary", idempotency_key="dashboard-key"
        )

    def _unexpected_resolver(**_kwargs):
        raise AssertionError("idempotent replay must not classify")

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _unexpected_resolver)
    cli_args = Namespace(
        workspace=None, branch=None, max_runtime=None, max_retries=None,
        title="CLI replay", body="unchanged", assignee="claudeprimary", created_by="tester",
        tenant=None, priority=0, parent=[], triage=False, idempotency_key="cli-key", skills=[],
        model_override=None, provider_override=None, reasoning_effort=None, goal_mode=False,
        goal_max_turns=None, initial_status="running", json=False,
    )
    assert kanban_cli._cmd_create(cli_args) == 0
    assert _latest_task_by_title("CLI existing").id == cli_id

    tool_payload = json.loads(kanban_tools._handle_create({
        "title": "Tool replay", "body": "unchanged", "assignee": "claudeprimary",
        "idempotency_key": "tool-key",
    }))
    assert tool_payload["ok"] is True
    assert tool_payload["task_id"] == tool_id

    response = router_client.post("/api/plugins/kanban/tasks", json={
        "title": "Dashboard replay", "body": "unchanged", "assignee": "claudeprimary",
        "idempotency_key": "dashboard-key",
    })
    assert response.status_code == 200
    assert response.json()["task"]["id"] == dashboard_id


def test_dashboard_idempotent_replay_validates_explicit_override_before_skipping_routing(
    kanban_home, monkeypatch, router_client
):
    """Invalid override pairs stay invalid even when a replay finds an existing task."""
    created = router_client.post("/api/plugins/kanban/tasks", json={
        "title": "Dashboard existing", "assignee": "claudeprimary", "idempotency_key": "dashboard-key",
    })
    assert created.status_code == 200

    def _unexpected_resolver(**_kwargs):
        raise AssertionError("idempotent replay must not classify")

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _unexpected_resolver)
    replay = router_client.post("/api/plugins/kanban/tasks", json={
        "title": "Dashboard replay", "assignee": "claudeprimary", "idempotency_key": "dashboard-key",
        "provider_override": "openai-codex",
    })

    assert replay.status_code == 400
    assert replay.json()["detail"] == "provider_override requires a model_override"


def test_idempotent_replays_validate_reasoning_effort_before_skipping_routing(
    kanban_home, monkeypatch, router_client
):
    """Malformed explicit reasoning stays rejected without consuming classifier work."""
    with kbc.connect() as conn:
        kb.create_task(conn, title="CLI existing", assignee="claudeprimary", idempotency_key="cli-key")
        kb.create_task(conn, title="Tool existing", assignee="claudeprimary", idempotency_key="tool-key")
        kb.create_task(
            conn, title="Dashboard existing", assignee="claudeprimary", idempotency_key="dashboard-key"
        )

    def _unexpected_resolver(**_kwargs):
        raise AssertionError("invalid idempotent replay must not classify")

    monkeypatch.setattr("hermes_cli.kanban_model_routing.resolve_kanban_model_route", _unexpected_resolver)
    cli_args = Namespace(
        workspace=None, branch=None, max_runtime=None, max_retries=None,
        title="CLI replay", body="unchanged", assignee="claudeprimary", created_by="tester",
        tenant=None, priority=0, parent=[], triage=False, idempotency_key="cli-key", skills=[],
        model_override=None, provider_override=None, reasoning_effort="not-a-level", goal_mode=False,
        goal_max_turns=None, initial_status="running", json=False,
    )
    assert kanban_cli._cmd_create(cli_args) == 2

    tool_payload = json.loads(kanban_tools._handle_create({
        "title": "Tool replay", "body": "unchanged", "assignee": "claudeprimary",
        "idempotency_key": "tool-key", "reasoning_effort": "not-a-level",
    }))
    assert "reasoning_effort must be one of" in tool_payload["error"]

    replay = router_client.post("/api/plugins/kanban/tasks", json={
        "title": "Dashboard replay", "body": "unchanged", "assignee": "claudeprimary",
        "idempotency_key": "dashboard-key", "reasoning_effort": "not-a-level",
    })
    assert replay.status_code == 400
    assert "reasoning_effort must be one of" in replay.json()["detail"]
