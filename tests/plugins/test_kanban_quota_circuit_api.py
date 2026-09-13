"""Dashboard diagnostics and manual control for host quota circuits."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_quota_circuit as kqc


def _router():
    repo = Path(__file__).resolve().parents[2]
    path = repo / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_quota_dashboard_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_lists_sanitized_circuit_and_clears_by_handle(client, monkeypatch):
    monkeypatch.setattr(kqc.time, "time", lambda: 10_000)
    kqc.register_quota_circuit(
        "private-account-name",
        retry_after=300,
        board="default",
        task_id="t_source",
        reason="billing",
        max_seconds=3600,
    )

    response = client.get("/api/plugins/kanban/quota-circuits")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is True
    assert len(payload["circuits"]) == 1
    circuit = payload["circuits"][0]
    assert circuit["group"].startswith("budget-")
    assert "private-account-name" not in response.text

    cleared = client.delete(f"/api/plugins/kanban/quota-circuits/{circuit['group']}")
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": True, "group": circuit["group"]}
    assert client.get("/api/plugins/kanban/quota-circuits").json()["active"] is False


def test_dashboard_clear_unknown_handle_is_404(client):
    response = client.delete("/api/plugins/kanban/quota-circuits/budget-does-not-exist")
    assert response.status_code == 404


def test_dashboard_reports_recovering_state_after_probe(client, monkeypatch):
    monkeypatch.setattr(kqc.time, "time", lambda: 10_000)
    kqc.register_quota_circuit(
        "private-account-name", retry_after=10, board="default", task_id="t_source",
        reason="rate_limit", max_seconds=3600,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 10_010)
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: {
        "private-account-name": {"providers": ["openai-codex"], "profiles": ["implementer"]},
    })
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect(board="default") as conn:
        task_id = kb.create_task(
            conn, title="probe", assignee="implementer",
            model_override="m", provider_override="openai-codex",
        )
        assert kqc.task_quota_guard(conn, task_id, board="default", consume_probe=True) is None
    circuit = client.get("/api/plugins/kanban/quota-circuits").json()["circuits"][0]
    assert circuit["state"] == "recovering"
    assert circuit["next_eligible_at"] == 10_010 + kqc._resume_spread_seconds()
    assert "private-account-name" not in repr(circuit)
