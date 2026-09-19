from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_router():
    plugin_file = (
        Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "kanban_review_contract_api_test", plugin_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def review_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="API review", assignee="builder")
        implementation = kb.claim_task(conn, task_id, claimer="builder:1")
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        assert kb.claim_review_task(conn, task_id, claimer="reviewer:1") is not None
    app = FastAPI()
    app.include_router(_load_router(), prefix="/api/plugins/kanban")
    return TestClient(app), task_id


def test_dashboard_request_changes_requires_blockers(review_api):
    client, task_id = review_api

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/request-changes",
        json={"reason": "prose only"},
    )

    assert response.status_code == 422


def test_dashboard_request_changes_persists_structured_verdict(review_api):
    client, task_id = review_api

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/request-changes",
        json={
            "reason": "Fix both failures.",
            "blockers": [
                {"basis": "original_ac", "reference": "AC1: reject traversal"},
                {"basis": "landing_gate", "reference": "ruff: changed files"},
            ],
            "followups": ["Consider progress output."],
        },
    )

    assert response.status_code == 200
    assert response.json()["implementer"] == "builder"
    with kbc.connect() as conn:
        event = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "changes_requested"
        ][-1]
    assert [item["reference"] for item in event.payload["blockers"]] == [
        "AC1: reject traversal",
        "ruff: changed files",
    ]
    assert event.payload["followups"] == ["Consider progress output."]
